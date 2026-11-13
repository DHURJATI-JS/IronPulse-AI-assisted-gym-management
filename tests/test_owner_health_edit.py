"""The gym owner's health editor never corrupts or loses what the member told the app (F34-F36).

* F34: saving the form (even with only the emergency contact changed) split entries with a comma, "and", "&" or ";"
  inside, re-spelled them and deleted every entry's safety note - the entries round-tripped through a comma-separated
  textarea and the submitted list was taken as the complete truth.
* F35: an entry the member (or the AI's "Remember this") added while the owner's page was open was deleted by the
  owner's stale save. Saves are applied as explicit changes against the entries the form showed (its signed
  ``health_seen`` snapshot); a form without one never removes the member's own entries.
* F36: a member with more than 20 entries of one kind made every owner save fail ("List at most 20 entries."), the
  guardian and emergency contact included. The 20 per list now limits what one save ADDS; the member's total stays 50.
"""

from __future__ import annotations

import html
import re
from typing import Any

from sqlalchemy import select

from app.models import CustomerProfile, HealthCondition, Notification
from app.models.enums import ConditionKind
from app.services import intake
from tests.conftest import login_as, make_client, post_form
from tests.factories import make_customer, make_subscription
from tests.test_owner_customers import owner_with_gym

C, A, M, I = ConditionKind.CONDITION, ConditionKind.ALLERGY, ConditionKind.MEDICATION, ConditionKind.INJURY
MEMBER_ENTRIES = [  # (kind, name, note) - each name has a list separator or a misspelling inside
    (C, "Anxiety and depression", "Panic attacks during HIIT - stop, sit down, slow breathing"),
    (M, "Salbutamol inhaler, 2 puffs before exercise", "Keep it at the front desk"),
    (I, "Knee & ankle sprain (left)", "No jumping or lunges until physio clears it"),
    (A, "Peanuts; tree nuts", "Anaphylaxis - EpiPen in my bag"),
    (I, "Old ACL tear, right knee", None),
    (C, "Hypertention", "Measured 150/95 last month"),
    (C, "Rotator cuff tear + bursitis", "No overhead pressing"),
    (C, "Pregnancy - second trimester, cleared by her doctor for light exercise", None),
]
_FORM = re.compile(r'<form method="post" action="/owner/customers/\d+/health".*?</form>', re.S)
_TEXTAREA = re.compile(r'<textarea[^>]*\bname="([a-z_]+)"[^>]*>(.*?)</textarea>', re.S)
_INPUT = re.compile(r'<input[^>]*\bname="([a-z_]+)"[^>]*\bvalue="([^"]*)"', re.S)


async def owner_form(client, member_id: int) -> dict[str, str]:
    """The health form exactly as the owner's browser would submit it (the page's own pre-filled values)."""
    page = await client.get(f"/owner/customers/{member_id}")
    assert page.status_code == 200
    form = _FORM.search(page.text).group(0)
    values = {name: html.unescape(value) for name, value in _TEXTAREA.findall(form)}
    values.update({name: html.unescape(value) for name, value in _INPUT.findall(form)
                   if name not in ("csrf_token", "submit_id")})
    return values


async def entries(db, member_id: int) -> set[tuple[str, str, str | None, str]]:
    rows = (await db.execute(select(HealthCondition).where(HealthCondition.customer_id == member_id)
                             .execution_options(populate_existing=True))).scalars().all()
    return {(c.kind.value, c.name, c.notes, c.source) for c in rows}


async def member_of_gym(db, **kwargs: Any):
    owner, gym, plans = await owner_with_gym(db)
    member = await make_customer(db, **kwargs)
    await make_subscription(db, member, plans[0])
    return owner, member


async def add_entries(db, member_id: int, items) -> None:
    db.add_all([HealthCondition(customer_id=member_id, kind=kind, name=name, notes=note, source="self",
                                tag=intake.condition_tag(name, kind)) for kind, name, note in items])
    await db.commit()


# ============================================================================================ F34
async def test_saving_the_form_keeps_every_entry_and_note_exactly_as_the_member_saved_it(client, db):
    owner, member = await member_of_gym(db, full_name="Rohan Verma")
    await add_entries(db, member.id, MEMBER_ENTRIES)
    before = await entries(db, member.id)
    await login_as(client, owner)

    form = await owner_form(client, member.id)  # the page as it renders today (entries joined with ", ")
    form.update(emergency_contact_name="Asha Verma", emergency_contact_phone="+91 98450 11111")
    r = await post_form(client, f"/owner/customers/{member.id}/health", form)
    assert r.status_code == 303, r.text
    assert await entries(db, member.id) == before  # nothing split, renamed, re-spelled or stripped of its note
    profile = await db.get(CustomerProfile, member.id, populate_existing=True)
    assert profile.emergency_contact_name == "Asha Verma"
    note = (await db.execute(select(Notification).where(Notification.user_id == member.id))).scalar_one()
    assert "updated your emergency contact." in note.body and "conditions" not in note.body

    # the snapshot form (one entry per line + health_seen) round-trips just the same
    snapshot = intake.health_form_values(member.id, (await db.execute(
        select(HealthCondition).where(HealthCondition.customer_id == member.id))).scalars().all(), profile)
    form.update({k: v for k, v in snapshot.items() if k in intake.LIST_FIELDS or k == "health_seen"})
    form.update(emergency_contact_phone="+91 98450 22222")
    assert (await post_form(client, f"/owner/customers/{member.id}/health", form)).status_code == 303
    assert await entries(db, member.id) == before


async def test_new_text_typed_next_to_existing_entries_only_adds_entries(client, db):
    owner, member = await member_of_gym(db)
    await add_entries(db, member.id, MEMBER_ENTRIES[:2])
    await login_as(client, owner)
    form = await owner_form(client, member.id)
    form["conditions"] += ", migrane, Asthma"
    assert (await post_form(client, f"/owner/customers/{member.id}/health", form)).status_code == 303
    rows = await entries(db, member.id)
    assert ("condition", "Anxiety and depression", MEMBER_ENTRIES[0][2], "self") in rows
    assert {("condition", "Migraine", None, "owner"), ("condition", "Asthma", None, "owner")} <= rows
    assert len(rows) == 4


# ============================================================================================ F35
async def test_an_entry_added_while_the_owners_page_was_open_survives_the_owners_save(client, db):
    owner, member = await member_of_gym(db)
    await add_entries(db, member.id, [(C, "Asthma", None)])
    await login_as(client, owner)
    stale = await owner_form(client, member.id)  # the owner opens the member's page ...
    shown_at_load = (await db.execute(
        select(HealthCondition).where(HealthCondition.customer_id == member.id))).scalars().all()

    async with make_client() as mc:  # ... the member adds an allergy with a safety note ...
        await login_as(mc, member)
        r = await post_form(mc, "/me/health/profile/conditions",
                            {"kind": "allergy", "name": "Latex", "notes": "Hives within minutes"})
        assert r.status_code == 303
    latex = ("allergy", "Latex", "Hives within minutes", "self")
    assert latex in await entries(db, member.id)

    # ... and the owner saves the stale form, changing only the emergency contact: today's form (no snapshot) ...
    stale.update(emergency_contact_name="Asha Verma", emergency_contact_phone="+91 98450 11111")
    assert (await post_form(client, f"/owner/customers/{member.id}/health", stale)).status_code == 303
    assert latex in await entries(db, member.id)
    # ... and a form carrying its snapshot (the entries it showed)
    stale.update(health_seen=intake.health_snapshot(member.id, shown_at_load), emergency_contact_phone="+91 98450 22222")
    assert (await post_form(client, f"/owner/customers/{member.id}/health", stale)).status_code == 303
    assert latex in await entries(db, member.id)
    assert ("condition", "Asthma", None, "self") in await entries(db, member.id)


async def test_owner_removals_follow_what_the_form_showed(client, db):
    """With the snapshot the owner removes exactly the entries they deleted from the box and never re-adds one the
    member removed meanwhile; without it, the gym's own entries can still be removed but never the member's."""
    owner, member = await member_of_gym(db)
    await add_entries(db, member.id, [(C, "Asthma", "Inhaler in locker 4"), (C, "Migraine", None)])
    db.add(HealthCondition(customer_id=member.id, kind=A, name="Dust", source="owner"))
    await db.commit()
    await login_as(client, owner)
    form = await owner_form(client, member.id)

    # the page now carries the snapshot (owner/customers/detail.html renders health_seen): a form without it is an
    # older page or a script
    no_snapshot = dict({k: v for k, v in form.items() if k != "health_seen"}, conditions="", allergies="")
    assert (await post_form(client, f"/owner/customers/{member.id}/health", no_snapshot)).status_code == 303
    assert {(k, n) for k, n, _o, _s in await entries(db, member.id)} == {("condition", "Asthma"),
                                                                        ("condition", "Migraine")}

    shown = (await db.execute(select(HealthCondition).where(HealthCondition.customer_id == member.id))).scalars().all()
    with_snapshot = dict(form, conditions="Asthma\nMigraine", allergies="", health_seen=intake.health_snapshot(
        member.id, shown))
    asthma = next(c for c in shown if c.name == "Asthma")
    await db.delete(asthma)  # the member removes Asthma after the owner opened the page
    await db.commit()
    with_snapshot["conditions"] = "Asthma"  # the owner deleted Migraine; Asthma is still in their stale box
    assert (await post_form(client, f"/owner/customers/{member.id}/health", with_snapshot)).status_code == 303
    assert await entries(db, member.id) == set()  # Migraine removed as asked, Asthma not brought back


# ============================================================================================ F36
async def test_a_member_with_many_entries_of_one_kind_never_blocks_the_owners_save(client, db):
    owner, member = await member_of_gym(db)
    await add_entries(db, member.id, [(C, f"Condition number {i:02d}", None) for i in range(25)])
    await login_as(client, owner)
    form = await owner_form(client, member.id)
    form.update(emergency_contact_name="Asha Verma", emergency_contact_phone="+91 98450 11111")
    r = await post_form(client, f"/owner/customers/{member.id}/health", form)
    assert r.status_code == 303, r.text
    assert (await db.get(CustomerProfile, member.id, populate_existing=True)).emergency_contact_name == "Asha Verma"
    assert len(await entries(db, member.id)) == 25

    # the 20 per list limits what one save adds; the member's 50 in total still holds - friendly refusals, no change
    too_many = dict(form, allergies=", ".join(f"Allergen {i:02d}" for i in range(21)))
    r = await post_form(client, f"/owner/customers/{member.id}/health", too_many)
    assert r.status_code == 400 and "Add at most 20 new allergies at a time" in html.unescape(r.text)
    past_total = dict(form, allergies=", ".join(f"Allergen {i:02d}" for i in range(20)),
                      medications=", ".join(f"Medicine {i:02d}" for i in range(10)))
    r = await post_form(client, f"/owner/customers/{member.id}/health", past_total)
    assert r.status_code == 400 and "at most 50" in html.unescape(r.text)
    assert len(await entries(db, member.id)) == 25
