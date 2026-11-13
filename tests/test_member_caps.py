"""Member-area load safety (approved limits):

* every reader of ONE member's health conditions stops at ``CONDITIONS_READ_MAX`` rows in SQL, chosen safety-relevant
  first (tagged conditions, then injuries, conditions, allergies, medications): health ``condition_tags``, plan advice,
  profiles, owner intake (the embedding and the owner's health edit), ``coach.conditions_of`` and the live coach's
  ``load_member`` - a padded list can neither make a page heavier nor push a safety tag out;
* the scale scan asks the AI guard once before any AI work: a refused OCR vision step is the click's only refusal (one
  strike, the friendly message) - it never goes on to the vision model, and nor does an OCR vision step that failed.
"""

from __future__ import annotations

import html
from typing import Any

import pytest
from sqlalchemy import event, insert, select

from app.ai import health as health_ai
from app.ai.errors import AIRejected
from app.ai.guard import guard
from app.ai.owner import AIOwner
from app.config import get_settings
from app.models import CustomerProfile, HealthCondition, User
from app.models.enums import ConditionKind
from app.services import health as hs
from tests.ask_ai_helpers import steady_minute  # noqa: F401 - a fixture
from tests.conftest import login_as, post_form
from tests.factories import make_customer, make_owner, png_bytes

CAP = 6


class Reads:
    """Statements on health_conditions and the HealthCondition rows the ORM materialises while the block runs."""

    def __init__(self) -> None:
        self.sql: list[str] = []
        self.rows = 0

    def _stmt(self, conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if "health_conditions" in statement:
            self.sql.append(" ".join(statement.split()))

    def _load(self, target: Any, context: Any) -> None:
        self.rows += 1

    def __enter__(self) -> "Reads":
        from app import db as dbmod

        event.listen(dbmod.get_engine().sync_engine, "before_cursor_execute", self._stmt)
        event.listen(HealthCondition, "load", self._load)
        return self

    def __exit__(self, *exc: object) -> None:
        from app import db as dbmod

        event.remove(dbmod.get_engine().sync_engine, "before_cursor_execute", self._stmt)
        event.remove(HealthCondition, "load", self._load)

    def selects(self) -> list[str]:
        """The reads (a data-modifying ``WITH ... DELETE ... RETURNING`` is the edit's delete, not a read)."""
        return [s for s in self.sql if s.startswith(("SELECT", "WITH")) and "DELETE FROM" not in s]


@pytest.fixture
def small_cap(monkeypatch):
    monkeypatch.setattr(hs, "CONDITIONS_READ_MAX", CAP)
    return CAP


async def padded_member(db, **kwargs: Any) -> User:
    """10 untagged allergies first (the oldest rows, alphabetically first too) - then the two that matter for safety."""
    user = await make_customer(db, **kwargs)
    await db.execute(insert(HealthCondition), [
        {"customer_id": user.id, "kind": ConditionKind.ALLERGY, "name": f"Aardvark fur {i}", "tag": None} for i in range(10)
    ] + [
        {"customer_id": user.id, "kind": ConditionKind.CONDITION, "name": "Asthma since childhood", "tag": "asthma"},
        {"customer_id": user.id, "kind": ConditionKind.INJURY, "name": "Torn ACL", "tag": "knee_injury"},
    ])
    await db.commit()
    return user


def _bounded(reads: Reads) -> None:
    assert reads.selects(), "no conditions read at all"
    assert all("LIMIT" in s for s in reads.selects()), reads.selects()


# ============================================================================================ condition readers
async def test_condition_tags_reads_capped_tagged_rows(db, small_cap):
    user = await padded_member(db)
    with Reads() as reads:
        assert await hs.condition_tags(db, user.id) == ["asthma", "knee_injury"]
    _bounded(reads)


async def test_plan_advice_reads_the_capped_distinct_tags(db, small_cap):
    from app.services import plan_advice

    user = await padded_member(db)
    member = (await db.execute(select(User).where(User.id == user.id))).scalar_one()
    with Reads() as reads:
        ctx = await plan_advice.build_context(db, member, None)
    _bounded(reads)
    assert ctx.tags == ["asthma", "knee_injury"]
    assert "health condition tags: asthma, knee_injury" in plan_advice.build_prompt(ctx, "Which plan?")


async def test_profile_page_shows_at_most_the_cap_safety_relevant_first(db, small_cap):
    from app.services import profiles

    user = await padded_member(db)
    member = await db.get(User, user.id)
    with Reads() as reads:
        data = await profiles.load_profile(db, member, user.id)
    _bounded(reads)
    assert reads.rows == CAP and len(data.conditions) == CAP
    names = [c.name for c in data.conditions]
    assert "Asthma since childhood" in names and "Torn ACL" in names  # never pushed out by the padding
    assert [(c.kind.value, c.name) for c in data.conditions] == sorted((c.kind.value, c.name) for c in data.conditions)


async def test_coach_conditions_are_capped_and_keep_every_safety_tag(db, small_cap):
    from app.services import coach

    user = await padded_member(db)
    with Reads() as reads:
        rows = await coach.conditions_of(db, user.id)
    _bounded(reads)
    assert reads.rows == CAP and len(rows) == CAP
    assert coach.tags_of(rows, 30) == ["asthma", "knee_injury"]  # the plan filter still sees both
    assert [(c.kind.value, c.name) for c in rows] == sorted((c.kind.value, c.name) for c in rows)  # the page's order


async def test_live_coach_loads_a_capped_member_with_every_safety_tag(db, small_cap):
    from app.services import live_coach

    user = await padded_member(db)
    member_user = await db.get(User, user.id)
    with Reads() as reads:
        member = await live_coach.load_member(db, member_user)
    _bounded(reads)
    assert len(member.condition_names) == CAP and {"asthma", "knee_injury"} <= member.tags


async def test_the_intake_embedding_reads_a_capped_list(db, small_cap, monkeypatch):
    from app.services import intake

    user = await padded_member(db)
    seen: list[str] = []
    real = hs.conditions_text

    def text(conditions: Any) -> str:
        out = real(conditions)
        seen.append(out)
        return out

    monkeypatch.setattr(hs, "conditions_text", text)
    with Reads() as reads:
        await intake._store_embedding(db, user.id)
    _bounded(reads)
    assert reads.rows == CAP and "Asthma since childhood" in seen[0] and "Torn ACL" in seen[0]
    await db.commit()
    assert (await db.get(CustomerProfile, user.id)).health_embedding is not None


async def test_one_tag_over_and_over_never_pushes_another_tag_out(db, small_cap, monkeypatch):
    """Ten old rows of one safety tag (injuries: the kind read first) before a different tag: every capped reader still
    sees both - one row of every distinct tag comes first, and the tag list itself is read DISTINCT."""
    from app.services import coach, intake, live_coach, profiles

    user = await make_customer(db)
    await db.execute(insert(HealthCondition), [
        {"customer_id": user.id, "kind": ConditionKind.INJURY, "name": f"Knee sprain {i}", "tag": "knee_injury"}
        for i in range(10)
    ] + [{"customer_id": user.id, "kind": ConditionKind.CONDITION, "name": "Asthma since childhood", "tag": "asthma"}])
    await db.commit()
    member = await db.get(User, user.id)
    seen: list[str] = []
    real = hs.conditions_text
    monkeypatch.setattr(hs, "conditions_text", lambda conditions: seen.append(real(conditions)) or seen[-1])
    with Reads() as reads:
        tags = await hs.condition_tags(db, user.id)
        coach_rows = await coach.conditions_of(db, user.id)
        live = await live_coach.load_member(db, member)
        profile = await profiles.load_profile(db, member, user.id)
        await intake._store_embedding(db, user.id)
    _bounded(reads)
    assert tags == ["asthma", "knee_injury"]
    assert len(coach_rows) == CAP and coach.tags_of(coach_rows, 30) == ["asthma", "knee_injury"]
    assert len(live.condition_names) == CAP and {"asthma", "knee_injury"} <= live.tags
    assert len(profile.conditions) == CAP and "Asthma since childhood" in [c.name for c in profile.conditions]
    assert "Asthma since childhood" in seen[0] and "Knee sprain 0" in seen[0]
    await db.rollback()


async def test_the_owners_health_edit_never_loads_the_whole_list(db, small_cap):
    """The edit compares the submitted lists with the entries the form showed (its snapshot - read as plain columns, with
    a LIMIT) and removes the ones the owner deleted in one statement - whatever the member's list holds (rows from
    before the caps included)."""
    from app.services import intake

    owner = await make_owner(db)
    user = await make_customer(db)
    await db.execute(insert(HealthCondition), [
        {"customer_id": user.id, "kind": ConditionKind.CONDITION, "name": f"Cond {i:02d}", "tag": None} for i in range(30)
    ] + [
        {"customer_id": user.id, "kind": ConditionKind.ALLERGY, "name": "Dust", "tag": "respiratory_allergy"},
        {"customer_id": user.id, "kind": ConditionKind.ALLERGY, "name": "Latex", "tag": "skin_allergy"},
        {"customer_id": user.id, "kind": ConditionKind.MEDICATION, "name": "Salbutamol", "tag": None},
    ])
    await db.commit()
    shown = (await db.execute(select(HealthCondition).where(HealthCondition.customer_id == user.id))).scalars().all()
    seen = intake.health_snapshot(user.id, shown)  # the owner's form: every entry shown
    customer = await db.get(User, user.id)
    profile = await hs.ensure_profile(db, customer)
    data = intake.HealthData(lists={"conditions": ["Cond 05", "Brand new"], "allergies": ["dust"],
                                    "medications": ["Salbutamol"], "injuries": []}, seen_token=seen)
    with Reads() as reads:
        changed = await intake.update_health(db, owner=owner, customer=customer, profile=profile, data=data)
    await db.commit()
    assert changed == ["conditions", "allergies"]
    assert reads.rows <= CAP  # only the capped embedding read loads rows: the diff loads none
    _bounded(reads)
    deletes = [s for s in reads.sql if "DELETE FROM health_conditions" in s]
    assert len(deletes) == 1
    rows = (await db.execute(select(HealthCondition.kind, HealthCondition.name)
                             .where(HealthCondition.customer_id == user.id))).all()
    assert sorted((k.value, n) for k, n in rows) == [("allergy", "Dust"), ("condition", "Brand new"),
                                                     ("condition", "Cond 05"), ("medication", "Salbutamol")]
    again = await intake.update_health(db, owner=owner, customer=customer, profile=profile, data=data)
    assert again == []  # the same lists again: nothing changes, nothing is notified


# ============================================================================================ scale scan
TAB = "tab-scan-caps-0001"


@pytest.fixture
def guard_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_guard_enabled", True)
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 1.0)


def _real_ocr(monkeypatch, *, local: str = "WEIGHT 72.4 kg") -> list[str]:
    """The REAL multi-engine OCR (run_ocr, its 'vision_llm' step through the AI client) with one fake local engine."""
    from app.services import ocr

    calls: list[str] = []

    def tesseract(img: bytes) -> tuple[str, float]:
        calls.append("tesseract")
        return local, 0.9

    monkeypatch.setattr(get_settings(), "ocr_engines", ["tesseract", "vision_llm"])
    monkeypatch.setattr(ocr, "_local_status", lambda: {n: (None if n == "tesseract" else "not installed")
                                                       for n in ocr.LOCAL_ENGINES})
    monkeypatch.setitem(ocr._SYNC_ENGINES, "tesseract", tesseract)
    return calls


async def _scan(client):
    return await post_form(client, "/me/health/scan", {"page_id": TAB},
                           files={"photo": ("scale.png", png_bytes((200, 120)), "image/png")})


async def test_a_refused_ocr_vision_step_is_the_clicks_only_refusal(client, db, monkeypatch, fake_ai, guard_on,
                                                                   steady_minute):
    local = _real_ocr(monkeypatch)
    provider = fake_ai(responses=["72.4 kg"] * 4)
    user = await make_customer(db)  # no AI plan: 3 'vision' units a minute
    owner = AIOwner(user_id=user.id, role="customer", ip="198.51.100.20")
    for _ in range(3):  # this minute's AI photo checks are used up
        assert await guard.admit(owner, "ocr") is not None
    before = (await guard.snapshot())["refused"]["quota"]
    await login_as(client, user)
    r = await _scan(client)
    text = html.unescape(r.text)
    assert r.status_code == 200 and provider.prompts == []  # no AI compute at all
    assert "AI vision check skipped." in text and "You've used your AI photo checks for this minute." in text
    assert 'data-scan-ai-refusal="quota"' in text and 'value="72.4"' in text  # the local engine's reading still fills in
    assert local == ["tesseract"]
    assert (await guard.snapshot())["refused"]["quota"] - before == 1  # ONE refusal for the click ...
    assert (await guard.status(user.id))["strikes"] == 1  # ... and one strike - never a second for the vision model


async def test_a_failed_ocr_vision_step_never_goes_on_to_the_vision_model(client, db, monkeypatch, fake_ai, guard_on):
    """Admitted at the click, but the OCR vision step itself failed (refused in a race, busy, every provider down):
    no second AI vision call in the same click."""
    from app.services import ocr

    _real_ocr(monkeypatch)
    fake_ai(responses=["{}"])

    async def refused(img: bytes, purpose: str) -> Any:
        raise AIRejected("quota", "You've used your AI photo checks for this minute.", retry_after=30)

    vision_model: list[bytes] = []

    async def read(image: bytes) -> Any:
        vision_model.append(image)
        return health_ai.MeasurementReading(weight=72.4, weight_unit="kg"), "gateway"

    monkeypatch.setattr(ocr, "_vision_llm", refused)
    monkeypatch.setattr(health_ai, "read_measurement_photo", read)
    user = await make_customer(db)
    await login_as(client, user)
    r = await _scan(client)
    assert r.status_code == 200 and 'value="72.4"' in r.text  # the local engine's reading
    assert vision_model == []


async def test_an_ocr_vision_step_that_found_no_text_still_asks_the_vision_model(client, db, monkeypatch, fake_ai,
                                                                                 guard_on):
    from app.services import ocr

    _real_ocr(monkeypatch, local="no numbers here")
    fake_ai(responses=["{}"])

    async def blank(img: bytes, purpose: str) -> tuple[str, None]:
        return "", None

    vision_model: list[bytes] = []

    async def read(image: bytes) -> Any:
        vision_model.append(image)
        return health_ai.MeasurementReading(weight=81.2, weight_unit="kg"), "gateway"

    monkeypatch.setattr(ocr, "_vision_llm", blank)
    monkeypatch.setattr(health_ai, "read_measurement_photo", read)
    user = await make_customer(db)
    await login_as(client, user)
    r = await _scan(client)
    assert r.status_code == 200 and len(vision_model) == 1 and 'value="81.2"' in r.text
