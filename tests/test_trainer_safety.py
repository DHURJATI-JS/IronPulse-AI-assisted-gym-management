"""Trainer-facing safety (app/services/trainer_safety.py): who sees a member's health & safety details (owner, assigned
trainer, other staff, other gyms, other members, anonymous - and the member's privacy choice), the safety panel in
chat / staff portal / owner member page, the send-time contraindication warning (JSON and no-JS, Send anyway audited
with the tag only), red-flag messages (guidance + urgent alerts, deduplicated, never refused by a cap), the chat caps
(friendly, race-safe), constant query counts and CSP-safe markup."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from app.core import caps
from app.core.utils import utcnow
from app.db import QueryCounter
from app.models import (
    AuditLog,
    ChatMessage,
    ChatThread,
    CustomerProfile,
    Employee,
    HealthCondition,
    Notification,
    TrainerClient,
    User,
)
from app.models.enums import ConditionKind, EmploymentStatus, Role, Severity, ShiftMode, ThreadStatus
from app.services import chat as chat_service
from app.services import trainer_safety as ts
from tests.conftest import api, login_as, make_client, post_form
from tests.factories import make_customer, make_employee, make_gym, make_owner, make_plans, make_subscription


# ====================================================================== world
async def world(db) -> dict[str, Any]:
    """Gym A: owner, assigned trainer T, other staff S, member M (knee injury, high blood pressure, peanut allergy,
    blood-pressure medication), member M2. Gym B: owner B and staff SB. Threads M<->team, M<->T, M<->S."""
    owner = await make_owner(db, full_name="Olivia Owner")
    gym = await make_gym(db, owner, name="Pulse Indiranagar")
    plans = await make_plans(db, gym)
    trainer = await make_employee(db, gym)
    staff = await make_employee(db, gym)
    member = await make_customer(db, full_name="Maya Member", country_code="IN")
    sub = await make_subscription(db, member, plans[1])
    other = await make_customer(db, full_name="Otto Other")
    await make_subscription(db, other, plans[0])
    for kind, name, tag in ((ConditionKind.INJURY, "Torn ACL (left knee)", "knee_injury"),
                            (ConditionKind.CONDITION, "High BP", "hypertension"),
                            (ConditionKind.ALLERGY, "Peanuts", "food_allergy"),
                            (ConditionKind.MEDICATION, "Amlodipine 5 mg", None)):
        db.add(HealthCondition(customer_id=member.id, kind=kind, name=name, tag=tag))
    db.add(TrainerClient(employee_id=trainer.id, customer_id=member.id, subscription_id=sub.id, mode=ShiftMode.ONLINE,
                         active=True))
    await db.commit()
    owner_b = await make_owner(db, full_name="Bob Elsewhere")
    gym_b = await make_gym(db, owner_b, name="Other Gym")
    staff_b = await make_employee(db, gym_b)
    threads = {}
    for key, emp in (("team", None), ("trainer", trainer), ("staff", staff)):
        t = await chat_service.get_or_create_thread(db, gym_id=gym.id, customer_id=member.id,
                                                    employee_id=emp.id if emp else None)
        await chat_service.post_message(db, thread=t, sender=member, body=f"Hello {key}", sender_role="customer")
        await db.commit()
        threads[key] = t
    users = {}
    for key, emp in (("trainer", trainer), ("staff", staff), ("staff_b", staff_b)):
        users[key] = await db.get(User, emp.user_id)
    return {"owner": owner, "gym": gym, "plans": plans, "trainer": trainer, "staff": staff, "member": member,
            "other": other, "owner_b": owner_b, "staff_b": staff_b, "threads": threads, **{f"{k}_user": v for k, v in users.items()}}


async def set_sharing(db, member: User, share: bool) -> None:
    profile = await db.get(CustomerProfile, member.id)
    profile.share_health_with_trainers = share
    await db.commit()


def no_csp_violations(html: str) -> None:
    assert "<script>" not in html and " onclick=" not in html and ' style="' not in html


async def page(user: User | None, url: str):
    async with make_client() as c:
        if user is not None:
            await login_as(c, user)
        return await c.get(url)


DETAILS = ("Torn ACL", "Peanuts", "Amlodipine", "Knee injury", "High blood pressure")


# ====================================================================== who may see
async def test_health_access_matrix(db):
    w = await world(db)
    m = w["member"].id
    expect = {
        "owner": ts.FULL, "trainer_user": ts.FULL, "owner_b": ts.NONE, "staff_b_user": ts.NONE, "other": ts.NONE,
        "member": ts.NONE,
    }
    for key, level in expect.items():
        assert (await ts.health_access(db, w[key], m)).level == level, key
    assert (await ts.health_access(db, None, m)).level == ts.NONE
    # other staff: only inside a conversation with the member, and only the summary line
    assert (await ts.health_access(db, w["staff_user"], m)).level == ts.NONE
    assert (await ts.health_access(db, w["staff_user"], m, in_conversation=True)).level == ts.SUMMARY
    # the member keeps health details private: staff are told to ask, the gym's admin team still sees them
    await set_sharing(db, w["member"], False)
    assert (await ts.health_access(db, w["trainer_user"], m)).level == ts.WITHHELD
    assert (await ts.health_access(db, w["staff_user"], m, in_conversation=True)).level == ts.WITHHELD
    assert (await ts.health_access(db, w["owner"], m)).level == ts.FULL
    assert (await ts.health_access(db, w["staff_b_user"], m)).level == ts.NONE
    # an ended assignment is no longer "their trainer"
    await set_sharing(db, w["member"], True)
    link = (await db.execute(select(TrainerClient).where(TrainerClient.customer_id == m))).scalar_one()
    link.active = False
    await db.commit()
    assert (await ts.health_access(db, w["trainer_user"], m)).level == ts.NONE
    assert (await ts.health_access(db, w["trainer_user"], m, in_conversation=True)).level == ts.SUMMARY


async def test_chat_panel_matrix(db):
    w = await world(db)
    t = w["threads"]
    r = await page(w["owner"], f"/chat/{t['team'].id}")
    assert r.status_code == 200 and "data-safety-panel" in r.text and 'data-level="full"' in r.text
    for word in DETAILS + ("Avoid", "Library exercises to skip", "Barbell back squat"):
        assert word in r.text, word
    no_csp_violations(r.text)
    r = await page(w["trainer_user"], f"/chat/{t['trainer'].id}")
    assert r.status_code == 200 and 'data-level="full"' in r.text and all(d in r.text for d in DETAILS)
    r = await page(w["staff_user"], f"/chat/{t['staff'].id}")
    assert r.status_code == 200 and 'data-level="summary"' in r.text
    assert "This member has safety notes - check with their trainer." in r.text
    assert not any(d in r.text for d in DETAILS)
    r = await page(w["member"], f"/chat/{t['trainer'].id}")  # the member's own chat: no trainer panel
    assert r.status_code == 200 and "data-safety-panel" not in r.text and "data-safety-badge" not in r.text
    for user in (w["other"], w["staff_b_user"], w["owner_b"]):  # never other members, never across gyms
        assert (await page(user, f"/chat/{t['team'].id}")).status_code == 404
        assert (await page(user, f"/chat/{t['staff'].id}")).status_code == 404
    r = await page(None, f"/chat/{t['team'].id}")
    assert r.status_code == 303 and "/auth/login" in r.headers["location"]
    # the list badge: owner, trainer and staff (share on)
    for user in (w["owner"], w["trainer_user"], w["staff_user"]):
        r = await page(user, "/chat")
        assert "data-safety-badge" in r.text, user.id
    # private: staff get "ask before training" and no badge, the owner keeps the details
    await set_sharing(db, w["member"], False)
    for user, tid in ((w["trainer_user"], t["trainer"].id), (w["staff_user"], t["staff"].id)):
        r = await page(user, f"/chat/{tid}")
        assert 'data-level="withheld"' in r.text and "chose not to share health details - ask before training" in r.text
        assert not any(d in r.text for d in DETAILS)
        assert "data-safety-badge" not in (await page(user, "/chat")).text
    r = await page(w["owner"], f"/chat/{t['team'].id}")
    assert 'data-level="full"' in r.text and "Peanuts" in r.text
    async with make_client() as c:
        await login_as(c, w["trainer_user"])
        data = (await c.get("/api/chat/threads")).json()
        assert data["items"] and not any(i["safety"] for i in data["items"])


async def test_privacy_toggle_in_account_and_staff_portal(db):
    w = await world(db)
    async with make_client() as c:
        await login_as(c, w["member"])
        r = await c.get("/account")
        assert r.status_code == 200 and "Share my health and safety information with my gym" in r.text
        assert 'id="f-share-health"' in r.text and " checked" in r.text.split('id="f-share-health"')[1].split(">")[0]
        no_csp_violations(r.text)
        r = await post_form(c, "/account/health-sharing", {"share": "0"})
        assert r.status_code == 303
        r = await c.get("/account")
        assert " checked" not in r.text.split('id="f-share-health"')[1].split(">")[0]
    await db.refresh(await db.get(CustomerProfile, w["member"].id))
    assert (await db.get(CustomerProfile, w["member"].id)).share_health_with_trainers is False
    row = (await db.execute(select(AuditLog).where(AuditLog.action == "account.health_sharing"))).scalar_one()
    assert row.detail == {"share": False}
    r = await page(w["trainer_user"], f"/staff/clients/{w['member'].id}")
    assert r.status_code == 200 and "chose not to share health details - ask before training" in r.text
    assert not any(d in r.text for d in DETAILS) and "Safety notes" not in r.text
    r = await page(w["trainer_user"], "/staff/clients")
    assert "Health details private" in r.text and not re.search(r"\d+ health notes?<", r.text)
    r = await page(w["owner"], f"/owner/customers/{w['member'].id}")
    assert r.status_code == 200 and "Peanuts" in r.text and "chose not to share health details with trainers" in r.text
    async with make_client() as c:  # and back on
        await login_as(c, w["member"])
        assert (await post_form(c, "/account/health-sharing", {"share": ["0", "1"]})).status_code == 303
    r = await page(w["trainer_user"], f"/staff/clients/{w['member'].id}")
    assert 'data-level="full"' in r.text and all(d in r.text for d in DETAILS)
    # staff and owners have no such setting
    async with make_client() as c:
        await login_as(c, w["trainer_user"])
        assert (await post_form(c, "/account/health-sharing", {"share": "0"})).status_code == 403


async def test_staff_and_owner_pages_show_the_panel(db):
    w = await world(db)
    r = await page(w["trainer_user"], f"/staff/clients/{w['member'].id}")
    assert r.status_code == 200 and 'id="client-safety"' in r.text and 'data-level="full"' in r.text
    assert "Library exercises to skip" in r.text and "Burpee" in r.text
    no_csp_violations(r.text)
    r = await page(w["owner"], f"/owner/customers/{w['member'].id}")
    assert r.status_code == 200 and 'id="member-safety"' in r.text and "Barbell deadlift" in r.text
    no_csp_violations(r.text)
    # other staff of the gym are not the member's trainer: no client page at all
    assert (await page(w["staff_user"], f"/staff/clients/{w['member'].id}")).status_code == 404
    assert (await page(w["owner_b"], f"/owner/customers/{w['member'].id}")).status_code == 404


# ====================================================================== send-time warning
async def test_flagged_staff_message_warns_and_send_anyway_is_audited(db):
    w = await world(db)
    tid = w["threads"]["trainer"].id
    async with make_client() as c:
        await login_as(c, w["trainer_user"])
        before = await db.scalar(select(func.count(ChatMessage.id)))
        r = await api(c, "POST", f"/api/chat/{tid}/messages", {"body": "Tomorrow: 3x10 box jumps and heavy deadlifts"})
        assert r.status_code == 409, r.text
        data = r.json()
        assert data["code"] == "safety_check" and "Knee injury" in data["detail"] and "Maya" in data["detail"]
        flagged = {f["exercise"] for f in data["warning"]["flags"]}
        assert "box jumps" in flagged and any("deadlift" in e for e in flagged)
        assert await db.scalar(select(func.count(ChatMessage.id))) == before  # nothing stored
        r = await api(c, "POST", f"/api/chat/{tid}/messages", {"body": "See you at 6 - bring water"})
        assert r.status_code == 201
        r = await api(c, "POST", f"/api/chat/{tid}/messages", {"body": "No box jumps for you, do glute bridges"})
        assert r.status_code == 201  # advising against it is fine
        r = await api(c, "POST", f"/api/chat/{tid}/messages",
                      {"body": "Tomorrow: 3x10 box jumps and heavy deadlifts", "confirm_safety": True})
        assert r.status_code == 201
        msg_id = r.json()["message"]["id"]
    row = (await db.execute(select(AuditLog).where(AuditLog.action == "chat.safety_override"))).scalar_one()
    assert row.entity_id == tid and row.detail["message_id"] == msg_id
    assert set(row.detail["tags"]) == {"hypertension", "knee_injury"}
    blob = json.dumps(row.detail)
    assert "ACL" not in blob and "Knee injury" not in blob and "Peanuts" not in blob  # tags only, no health details


async def test_warning_levels_owner_other_staff_member_and_private(db):
    w = await world(db)
    t = w["threads"]
    body = {"body": "Let's do burpees and jump squats"}
    async with make_client() as c:
        await login_as(c, w["owner"])
        r = await api(c, "POST", f"/api/chat/{t['team'].id}/messages", body)
        assert r.status_code == 409 and "Knee injury" in r.json()["detail"]
    async with make_client() as c:  # other staff: told to check with the trainer, never the condition
        await login_as(c, w["staff_user"])
        r = await api(c, "POST", f"/api/chat/{t['staff'].id}/messages", body)
        assert r.status_code == 409
        data = r.json()
        assert "check with their trainer" in data["detail"].lower()
        assert not any(d in json.dumps(data) for d in DETAILS) and "knee" not in json.dumps(data).lower()
    async with make_client() as c:  # members are never checked
        await login_as(c, w["member"])
        assert (await api(c, "POST", f"/api/chat/{t['trainer'].id}/messages", body)).status_code == 201
    await set_sharing(db, w["member"], False)
    async with make_client() as c:  # private: no condition-based warning for staff
        await login_as(c, w["trainer_user"])
        assert (await api(c, "POST", f"/api/chat/{t['trainer'].id}/messages", body)).status_code == 201


async def test_send_time_warning_works_without_javascript(db):
    w = await world(db)
    tid = w["threads"]["trainer"].id
    async with make_client() as c:
        await login_as(c, w["trainer_user"])
        r = await post_form(c, f"/chat/{tid}/messages", {"body": "Finish with 50 burpees"})
        assert r.status_code == 409 and "data-safety-warning" in r.text and "Send anyway" in r.text
        assert 'name="confirm_safety" value="1"' in r.text and "Finish with 50 burpees" in r.text
        no_csp_violations(r.text)
        assert await db.scalar(select(func.count(ChatMessage.id)).where(ChatMessage.body == "Finish with 50 burpees")) == 0
        r = await post_form(c, f"/chat/{tid}/messages", {"body": "Finish with 50 burpees", "confirm_safety": "1"})
        assert r.status_code == 303
    assert await db.scalar(select(func.count(ChatMessage.id)).where(ChatMessage.body == "Finish with 50 burpees")) == 1
    assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "chat.safety_override")) == 1


def test_check_text_rules():
    flags = ts.check_text("Do 3x10 box jumps, then back squats", ["knee_injury"])
    assert [f.exercise for f in flags] == ["box jumps", "back squats"] and all(f.label == "Knee injury" for f in flags)
    assert ts.check_text("Avoid burpees and box jumps this week", ["knee_injury"]) == []
    assert ts.check_text("Sorry, running late! Contact me later", ["knee_injury", "pregnancy"]) == []
    assert ts.check_text("See you tomorrow at the gym", ["recent_surgery"]) == []
    assert [f.tag for f in ts.check_text("Test your 1RM today", ["hypertension"])] == ["hypertension"]
    assert ts.check_text("Squats at 90 kg", ["cardiac"], weight_kg=70)[0].exercise == "90 kg"
    assert ts.check_text("Squats at 30 kg", ["cardiac"], weight_kg=70) == []
    assert ts.check_text("Hold your breath on the way up", ["hypertension"])[0].reason.startswith("avoid breath")
    assert ts.check_text("Plank then crunches", ["pregnancy"]) and ts.check_text("burpees", []) == []
    assert len(ts.check_text("burpees, box jumps, jump squats, sprints, lunges, high knees, jumping jacks",
                             ["knee_injury"])) == ts.WARNING_FLAGS_MAX


# ====================================================================== red flags
async def _urgent(db) -> list[Notification]:
    return list((await db.execute(select(Notification).where(Notification.severity == Severity.DANGER)
                                  .order_by(Notification.user_id))).scalars().all())


async def test_red_flag_guidance_alerts_and_dedupe(db):
    w = await world(db)
    tid = w["threads"]["trainer"].id
    async with make_client() as c:
        await login_as(c, w["member"])
        r = await api(c, "POST", f"/api/chat/{tid}/messages", {"body": "I have chest pain after the warm up"})
        assert r.status_code == 201
        safety = r.json()["safety"]
        assert safety["number"] == "112" and safety["tel"] == "tel:112" and safety["alerted"] is True
        assert "Stop exercising" in safety["message"] and "emergency" in safety["message"]
        system = (await db.execute(select(ChatMessage).where(ChatMessage.thread_id == tid,
                                                             ChatMessage.sender_role == "system"))).scalars().all()
        assert len(system) == 1 and system[0].body.startswith(ts.SAFETY_PREFIX) and system[0].sender_id is None
        urgent = await _urgent(db)
        assert {n.user_id for n in urgent} == {w["owner"].id, w["trainer_user"].id}
        assert all(n.link == f"/chat/{tid}" and n.title.startswith("Urgent") for n in urgent)
        # within the hour: the member still gets the guidance, nobody is alerted twice
        r = await api(c, "POST", f"/api/chat/{tid}/messages", {"body": "Still feel dizzy and my chest is tight"})
        assert r.status_code == 201 and r.json()["safety"]["alerted"] is False
        assert len(await _urgent(db)) == 2
        assert await db.scalar(select(func.count(ChatMessage.id)).where(ChatMessage.sender_role == "system")) == 1
        r = await api(c, "POST", f"/api/chat/{tid}/messages", {"body": "No chest pain today, all good!"})
        assert r.status_code == 201 and "safety" not in r.json()
        r = await c.get(f"/chat/{tid}")
        assert "chat-bubble-safety" in r.text
    audit_row = (await db.execute(select(AuditLog).where(AuditLog.action == "chat.red_flag"))).scalar_one()
    assert audit_row.detail["flag"] == "chest_pain" and "chest pain" not in json.dumps(audit_row.detail).lower().replace(
        "chest_pain", "")


async def test_red_flag_in_staff_thread_reaches_trainer_unless_private(db):
    w = await world(db)
    tid = w["threads"]["staff"].id
    async with make_client() as c:
        await login_as(c, w["member"])
        r = await api(c, "POST", f"/api/chat/{tid}/messages", {"body": "I fainted on the treadmill"})
        assert r.status_code == 201 and r.json()["safety"]["alerted"]
    links = {n.user_id: n.link for n in await _urgent(db)}
    assert links == {w["owner"].id: f"/chat/{tid}", w["staff_user"].id: f"/chat/{tid}",
                     w["trainer_user"].id: f"/staff/clients/{w['member'].id}"}
    # private member: only the people in the conversation (the gym admin and that staff member)
    await set_sharing(db, w["member"], False)
    team = w["threads"]["team"].id
    async with make_client() as c:
        await login_as(c, w["member"])
        r = await api(c, "POST", f"/api/chat/{team}/messages", {"body": "severe shortness of breath right now"})
        assert r.status_code == 201
    team_alerts = [n for n in await _urgent(db) if n.link == f"/chat/{team}"]
    assert {n.user_id for n in team_alerts} == {w["owner"].id}


async def test_red_flag_no_js_flash_and_new_thread(db):
    w = await world(db)
    async with make_client() as c:
        await login_as(c, w["member"])
        r = await post_form(c, f"/chat/{w['threads']['team'].id}/messages", {"body": "my heart is racing and I feel faint"})
        assert r.status_code == 303
        follow = await c.get(r.headers["location"].split("#")[0])
        assert "112" in follow.text and "Safety first" in follow.text


def test_detect_red_flag():
    assert ts.detect_red_flag("I have chest pain").flag == "chest_pain"
    assert ts.detect_red_flag("no chest pain today") is None
    assert ts.detect_red_flag("I had a headache yesterday") is None  # stays with the live coach
    assert ts.detect_red_flag("headache and severe pain in my back").flag == "severe_pain"
    assert ts.detect_red_flag("sudden sharp pain in my lower back").flag == "severe_pain"
    assert ts.detect_red_flag("can we move my session to monday?") is None
    for text, flag in (("I feel dizzy", "dizziness"), ("I fainted", "fainting"), ("call an ambulance", "emergency"),
                       ("slurred speech and my face is drooping", "stroke")):
        assert ts.detect_red_flag(text).flag == flag, text


# ====================================================================== caps
@pytest.fixture
def low_caps(monkeypatch):
    """Lower the chat caps - the test fills in the values (after building its world)."""
    values: dict[str, int] = {}
    real = caps.limit

    def fake(name: str) -> int:
        return values.get(name) or real(name)

    monkeypatch.setattr(caps, "limit", fake)
    return values


async def test_message_cap_is_friendly_and_never_blocks_a_red_flag(db, low_caps):
    w = await world(db)  # the world already sent 3 member messages today
    low_caps[chat_service.MESSAGES_CAP] = 3
    tid = w["threads"]["trainer"].id
    async with make_client() as c:
        await login_as(c, w["member"])
        r = await api(c, "POST", f"/api/chat/{tid}/messages", {"body": "one more question"})
        assert r.status_code == 429 and r.json()["code"] == "cap_reached" and "in the last 24 hours" in r.json()["detail"]
        assert r.headers.get("retry-after")
        r = await post_form(c, f"/chat/{tid}/messages", {"body": "one more question"})
        assert r.status_code == 303  # no-JS: back to the thread with the friendly message
        r = await api(c, "POST", f"/api/chat/{tid}/messages", {"body": "I think I'm having a heart attack"})
        assert r.status_code == 201 and r.json()["safety"]["number"] == "112"
    async with make_client() as c:  # staff replies are not member messages
        await login_as(c, w["trainer_user"])
        assert (await api(c, "POST", f"/api/chat/{tid}/messages", {"body": "Calling you now"})).status_code == 201


async def test_thread_cap_on_new_conversations(db, low_caps):
    w = await world(db)  # 3 threads today (the cap is 1): starting another is refused, a red flag is not
    low_caps[chat_service.THREADS_CAP] = 1
    other_trainer = await make_employee(db, w["gym"])
    async with make_client() as c:
        await login_as(c, w["member"])
        r = await post_form(c, "/chat/new", {"target": f"{w['gym'].id}:{other_trainer.id}", "body": "Hi there"})
        assert r.status_code == 429 and "new conversations in the last 24 hours" in r.text
        r = await post_form(c, "/chat/new", {"target": f"{w['gym'].id}:{other_trainer.id}",
                                             "body": "I collapsed in the changing room"})
        assert r.status_code == 303
    assert await db.scalar(select(func.count(ChatThread.id)).where(ChatThread.employee_id == other_trainer.id)) == 1


async def test_message_cap_is_race_safe(db, low_caps):
    w = await world(db)  # 3 used
    low_caps[chat_service.MESSAGES_CAP] = 6
    tid = w["threads"]["trainer"].id
    async with make_client() as c:
        await login_as(c, w["member"])
        results = await asyncio.gather(*(api(c, "POST", f"/api/chat/{tid}/messages", {"body": f"parallel {i}"})
                                         for i in range(8)))
    codes = sorted(r.status_code for r in results)
    assert codes.count(201) == 3 and codes.count(429) == 5, codes
    assert await db.scalar(select(func.count(ChatMessage.id)).where(ChatMessage.sender_id == w["member"].id)) == 6


# ====================================================================== bounded, constant queries
async def test_query_counts_do_not_grow_with_members_or_conditions(db):
    w = await world(db)

    async def add_members(n: int) -> None:
        for i in range(n):
            m = await make_customer(db, conditions=["asthma", "knee pain"])
            await make_subscription(db, m, w["plans"][0])
            t = await chat_service.get_or_create_thread(db, gym_id=w["gym"].id, customer_id=m.id, employee_id=None)
            await chat_service.post_message(db, thread=t, sender=m, body=f"hi {i}", sender_role="customer")
            await db.commit()

    async def count(user: User, url: str) -> int:
        async with make_client() as c:
            await login_as(c, user)
            with QueryCounter() as qc:
                r = await c.get(url)
            assert r.status_code == 200
            return qc.count

    team = f"/chat/{w['threads']['team'].id}"
    await count(w["owner"], team)  # warm-up: first-request caches
    small = (await count(w["owner"], "/chat"), await count(w["owner"], team))
    await add_members(6)
    for i in range(40):
        db.add(HealthCondition(customer_id=w["member"].id, kind=ConditionKind.CONDITION, name=f"Old note {i}",
                               tag="asthma" if i % 2 else None))
    await db.commit()
    assert (await count(w["owner"], "/chat"), await count(w["owner"], team)) == small
    assert await count(w["trainer_user"], f"/staff/clients/{w['member'].id}") == await count(
        w["trainer_user"], f"/staff/clients/{w['member'].id}")
    brief = await ts.load_brief(db, w["member"], ts.Access(ts.FULL))
    assert len(brief.conditions) <= ts.PANEL_LIST_MAX and brief.lists_more > 0


# ====================================================================== the README Limits table states these values
def test_readme_limits_rows_state_the_chat_safety_values():
    from app.config import BASE_DIR

    text = (BASE_DIR / "README.md").read_text(encoding="utf-8")
    section = text[text.index("\n## Limits\n"):]
    section = section[:section.index("\n## ", 10)]
    rows = [line for line in section.splitlines() if line.startswith("| **")]
    caps_row = next(r for r in rows if "`CHAT_CAPS`" in r)
    threads, messages = (caps.CAPS[n].default for n in (chat_service.THREADS_CAP, chat_service.MESSAGES_CAP))
    assert f"{threads} new conversations and {messages} messages in any 24 hours" in caps_row
    panel_row = next(r for r in rows if "`PANEL_READ_MAX`" in r)
    for phrase in (f"reads {ts.PANEL_READ_MAX} health entries", f"lists {ts.PANEL_LIST_MAX} per list",
                   f"{ts.PANEL_EXERCISES_MAX} library exercises", f"at most {ts.WARNING_FLAGS_MAX} flagged",
                   f"per conversation per {int(ts.RED_FLAG_WINDOW.total_seconds() // 3600)} hour".replace(" 1 hour", " hour"),
                   f"latest {ts.RED_FLAG_SCAN} messages", f"at most {ts.BADGE_IDS_MAX} members"):
        assert phrase in panel_row, phrase
    from app.routers import owner_customers

    assert f"the {owner_customers.THREADS_SHOWN} with the newest activity" in next(r for r in rows if "`THREADS_SHOWN`" in r)


# ====================================================================== the gym side's chat caps (F10)
async def test_staff_and_owner_messages_are_capped_per_sender(db, low_caps):
    """F10: a staff member's / owner's messages count towards their own daily cap (all conversations) - friendly 429
    for the JSON API, a flash for the no-JS form, never a 500. The member can still write."""
    w = await world(db)
    tid = w["threads"]["trainer"].id
    low_caps["chat_messages_per_staff_day"] = 3
    async with make_client() as c:
        await login_as(c, w["trainer_user"])
        for i in range(3):
            assert (await api(c, "POST", f"/api/chat/{tid}/messages", {"body": f"note {i}"})).status_code == 201
        r = await api(c, "POST", f"/api/chat/{tid}/messages", {"body": "one more"})
        assert r.status_code == 429, r.text
        data = r.json()
        assert data["code"] == "cap_reached" and data["cap"] == "chat_messages_per_staff_day" and data["limit"] == 3
        assert "in the last 24 hours" in data["detail"] and r.headers.get("retry-after")
        r = await post_form(c, f"/chat/{tid}/messages", {"body": "one more"})
        assert r.status_code == 303  # no-JS: back to the conversation with the friendly message
    assert await db.scalar(select(func.count(ChatMessage.id)).where(ChatMessage.sender_id == w["trainer_user"].id)) == 3
    async with make_client() as c:
        await login_as(c, w["member"])
        assert (await api(c, "POST", f"/api/chat/{tid}/messages", {"body": "ok, thanks"})).status_code == 201


async def test_gym_side_messages_are_capped_per_conversation(db, low_caps):
    """F10: the member's protection - the gym team (staff member + owner together) has a daily ceiling per
    conversation; other conversations are not affected."""
    w = await world(db)
    tid, team = w["threads"]["trainer"].id, w["threads"]["team"].id
    low_caps["chat_staff_messages_per_conversation_day"] = 4
    for sender in (w["trainer_user"], w["owner"]):
        async with make_client() as c:
            await login_as(c, sender)
            for i in range(2):
                assert (await api(c, "POST", f"/api/chat/{tid}/messages", {"body": f"update {i}"})).status_code == 201
    async with make_client() as c:
        await login_as(c, w["owner"])
        r = await api(c, "POST", f"/api/chat/{tid}/messages", {"body": "and another"})
        assert r.status_code == 429 and r.json()["cap"] == "chat_staff_messages_per_conversation_day", r.text
        assert "this conversation" in r.json()["detail"]
        assert (await api(c, "POST", f"/api/chat/{team}/messages", {"body": "a different conversation"})).status_code == 201


async def test_staff_message_cap_is_race_safe(db, low_caps):
    w = await world(db)
    low_caps["chat_messages_per_staff_day"] = 3
    tid = w["threads"]["trainer"].id
    async with make_client() as c:
        await login_as(c, w["trainer_user"])
        results = await asyncio.gather(*(api(c, "POST", f"/api/chat/{tid}/messages", {"body": f"parallel {i}"})
                                         for i in range(8)))
    codes = sorted(r.status_code for r in results)
    assert codes.count(201) == 3 and codes.count(429) == 5, codes
    assert await db.scalar(select(func.count(ChatMessage.id)).where(ChatMessage.sender_id == w["trainer_user"].id)) == 3


# ====================================================================== suspended / former staff (F27)
async def _set_employment(db, emp_id: int, state: str) -> None:
    emp = await db.get(Employee, emp_id)
    await db.refresh(emp)
    if state == "deleted":
        emp.deleted_at, emp.purge_after = utcnow(), utcnow() + timedelta(days=30)
    elif state == "active":
        emp.status, emp.deleted_at, emp.purge_after = EmploymentStatus.ACTIVE, None, None
    else:
        emp.status = EmploymentStatus(state)
    await db.commit()


@pytest.mark.parametrize("state", ["suspended", "terminated", "pending_verification", "deleted"])
async def test_staff_who_cannot_work_lose_conversations_panel_and_badge(db, state):
    """F27: a suspended, ended, unverified or deleted employment no longer opens the member's conversations - no history,
    no safety panel, no badge, no close / reopen, no unread count - and gets them back once active again."""
    w = await world(db)
    tid, sid = w["threads"]["trainer"].id, w["threads"]["staff"].id
    for key in ("trainer", "staff"):
        await _set_employment(db, w[key].id, state)
    for user, thread_id in ((w["trainer_user"], tid), (w["staff_user"], sid)):
        async with make_client() as c:
            await login_as(c, user)
            assert (await c.get(f"/chat/{thread_id}")).status_code == 404
            assert (await c.get(f"/api/chat/{thread_id}/messages")).status_code == 404
            assert (await api(c, "POST", f"/api/chat/{thread_id}/messages", {"body": "still here?"})).status_code == 404
            assert (await api(c, "POST", f"/api/chat/{thread_id}/read", {})).status_code == 404
            assert (await post_form(c, f"/chat/{thread_id}/close")).status_code == 404
            assert (await c.get("/api/chat/threads")).json()["items"] == []
            r = await c.get("/chat")
            assert r.status_code == 200 and "data-safety-badge" not in r.text
            assert "Hello trainer" not in r.text and "Hello staff" not in r.text
        assert await chat_service.unread_total(db, user) == 0
        assert (await ts.health_access(db, user, w["member"].id, in_conversation=True)).level == ts.NONE
        assert await ts.panel_for(db, user, w["member"], in_conversation=True) is None
        assert await ts.badge_ids(db, user, [w["member"]]) == set()
    assert (await db.get(ChatThread, tid)).status == ThreadStatus.OPEN  # the close attempt changed nothing
    r = await page(w["member"], f"/chat/{tid}")  # the member keeps the (read-only) conversation
    assert r.status_code == 200 and "Hello trainer" in r.text and "data-chat-form" not in r.text
    for key in ("trainer", "staff"):  # active again: everything is back
        await _set_employment(db, w[key].id, "active")
    r = await page(w["trainer_user"], f"/chat/{tid}")
    assert r.status_code == 200 and 'data-level="full"' in r.text
    r = await page(w["staff_user"], f"/chat/{sid}")
    assert r.status_code == 200 and 'data-level="summary"' in r.text


# ====================================================================== the send-time warning reads instructions (F28)
@pytest.mark.parametrize(("tags", "text"), [
    (["lower_back"], "Don't forget your heavy deadlifts tomorrow"),
    (["lower_back"], "Never skip your good mornings"),
    (["lower_back"], "Swap the bike for heavy deadlifts"),
    (["knee_injury"], "Stop walking and start sprinting"),
    (["knee_injury"], "Replace the walk with box jumps"),
    (["knee_injury"], "No excuses 100 burpees"),
    (["knee_injury"], "No pain no gain - 50 box jumps"),
    (["knee_injury"], "Without fail 50 box jumps daily"),
    (["knee_injury"], "Skip the treadmill and do sprints"),
    (["knee_injury"], "No rest between sets of box jumps"),
    (["knee_injury"], "Don't be late, we start with box jumps"),
    (["hypertension"], "Never miss your 1RM test on Friday"),
    (["hypertension"], "Don't stop until failure on the leg press - go to failure"),
    (["hypertension"], "85 percent of your max"),
    (["knee_injury"], "boxjumps x 20"),
    (["knee_injury"], "box\u200bjumps x 20"),
    (["knee_injury"], "box jumpps x 20"),
    (["knee_injury"], "burpess for warm up"),
    (["lower_back"], "deadlfits on friday"),
    (["lower_back"], "Do 3x10 good mornings"),
])
def test_check_text_flags_instructions_typos_and_glued_words(tags, text):
    assert ts.check_text(text, tags, weight_kg=70), text


@pytest.mark.parametrize("text", [
    "Sprint planning meeting at 5", "Good morning Aditya, see you at 6", "goodmorning!", "Push up the session to 5pm",
    "Pull up your plan in the app", "Sit up straight at your desk", "No box jumps today, just walking",
    "Avoid burpees and box jumps this week", "Skip heavy deadlifts, do walking instead", "Replace box jumps with walking",
    "Instead of box jumps do glute bridges", "Don't try to do box jumps", "Don't do 3 sets of 10 box jumps",
    "You should not be doing any box jumps", "You're on crutches this week, rest", "Shin splints again?",
    "Hot springs trip on Sunday", "Our squads meet at the lounges", "My lungs feel fine", "I'm hoping you feel better",
    "I crunched the numbers on your plan", "The deadline is Friday",
])
def test_check_text_does_not_flag_other_senses_or_real_negations(text):
    assert ts.check_text(text, ["lower_back", "knee_injury", "hypertension", "cardiac"], weight_kg=70) == [], text


async def test_affirmative_phrasings_warn_over_http(db):
    """F28 over HTTP: "don't forget your heavy deadlifts" / "swap the bike for heavy deadlifts" are instructions."""
    w = await world(db)
    tid = w["threads"]["trainer"].id
    async with make_client() as c:
        await login_as(c, w["trainer_user"])
        for body in ("Don't forget your heavy deadlifts tomorrow", "Swap the bike for heavy deadlifts",
                     "Never skip your box jumps"):
            r = await api(c, "POST", f"/api/chat/{tid}/messages", {"body": body})
            assert r.status_code == 409 and r.json()["code"] == "safety_check", (body, r.text)
        assert (await api(c, "POST", f"/api/chat/{tid}/messages", {"body": "Good morning Maya!"})).status_code == 201


# ====================================================================== summary-level staff can't probe (F29)
async def test_summary_staff_get_the_same_reminder_whatever_the_conditions(db):
    """F29: other staff (summary level) get ONE condition-independent reminder for any exercise - a contraindicated
    one and a harmless one answer the same, so probing reveals nothing beyond "safety notes on file"."""
    w = await world(db)
    sid = w["threads"]["staff"].id
    answers = set()
    async with make_client() as c:
        await login_as(c, w["staff_user"])
        for body in ("Try some box jumps", "Try some bicep curls", "Try heavy deadlifts", "Try a cold plunge",
                     "Try the sauna", "Try kickboxing"):
            r = await api(c, "POST", f"/api/chat/{sid}/messages", {"body": body})
            assert r.status_code == 409, (body, r.text)
            data = r.json()
            assert data["warning"]["flags"] == [] and data["warning"]["lines"] == [] and data["warning"]["level"] == "summary"
            answers.add((data["detail"], json.dumps(data["warning"], sort_keys=True)))
        assert len(answers) == 1 and "check with their trainer" in data["detail"].lower()
        assert not any(d.lower() in json.dumps(data).lower() for d in DETAILS) and "knee" not in json.dumps(data).lower()
        assert (await api(c, "POST", f"/api/chat/{sid}/messages", {"body": "See you at the desk at 6"})).status_code == 201
        r = await api(c, "POST", f"/api/chat/{sid}/messages", {"body": "Try some box jumps", "confirm_safety": True})
        assert r.status_code == 201
    row = (await db.execute(select(AuditLog).where(AuditLog.action == "chat.safety_override"))).scalar_one()
    assert row.detail["level"] == "summary" and row.detail["tags"] == [] and row.detail["exercises"] == ["box jumps"]
    # a member with nothing on file: no reminder (and no summary panel either)
    t = await chat_service.get_or_create_thread(db, gym_id=w["gym"].id, customer_id=w["other"].id,
                                                employee_id=w["staff"].id)
    await db.commit()
    async with make_client() as c:
        await login_as(c, w["staff_user"])
        assert (await api(c, "POST", f"/api/chat/{t.id}/messages", {"body": "Try some box jumps"})).status_code == 201


# ====================================================================== red flags in read-only conversations (F30)
@pytest.fixture
def published(monkeypatch):
    from app.services.realtime import hub

    events: list[tuple[list[int], dict[str, Any]]] = []

    async def publish(user_ids, evt):  # noqa: ANN001
        events.append((sorted(set(user_ids)), evt))

    monkeypatch.setattr(hub, "publish", publish)
    return events


async def test_red_flag_in_a_closed_conversation_reopens_it_with_guidance_and_alerts(db, published):
    w = await world(db)
    tid = w["threads"]["trainer"].id
    async with make_client() as c:
        await login_as(c, w["owner"])
        assert (await post_form(c, f"/chat/{tid}/close")).status_code == 303
    async with make_client() as c:
        await login_as(c, w["member"])
        r = await api(c, "POST", f"/api/chat/{tid}/messages", {"body": "hello again"})
        assert r.status_code == 409  # an ordinary message still can't
        r = await api(c, "POST", f"/api/chat/{tid}/messages", {"body": "I fainted on the treadmill and still feel dizzy"})
        assert r.status_code == 201, r.text
        assert r.json()["safety"]["number"] == "112" and r.json()["safety"]["alerted"] is True
    thread = await db.get(ChatThread, tid)
    await db.refresh(thread)
    assert thread.status == ThreadStatus.OPEN
    assert {n.user_id for n in await _urgent(db)} == {w["owner"].id, w["trainer_user"].id}
    reopened = [ids for ids, e in published if e["type"] == "chat.status" and e["thread_id"] == tid and e["status"] == "open"]
    assert reopened and w["owner"].id in reopened[-1] and w["member"].id not in reopened[-1]
    # no JavaScript: the form gets the guidance as a flash, never only an error
    team = w["threads"]["team"].id
    async with make_client() as c:
        await login_as(c, w["owner"])
        assert (await post_form(c, f"/chat/{team}/close")).status_code == 303
    async with make_client() as c:
        await login_as(c, w["member"])
        r = await post_form(c, f"/chat/{team}/messages", {"body": "I have crushing chest pain"})
        assert r.status_code == 303
        follow = await c.get(r.headers["location"].split("#")[0])
        assert "Safety first" in follow.text and "112" in follow.text


@pytest.mark.parametrize("state", ["suspended", "terminated"])
async def test_red_flag_to_a_staff_member_who_is_away_reaches_the_gym_admin(db, state):
    w = await world(db)
    tid = w["threads"]["trainer"].id
    await _set_employment(db, w["trainer"].id, state)
    trainer_notes = select(func.count(Notification.id)).where(Notification.user_id == w["trainer_user"].id)
    before = await db.scalar(trainer_notes)
    async with make_client() as c:
        await login_as(c, w["member"])
        r = await api(c, "POST", f"/api/chat/{tid}/messages", {"body": "Thanks!"})
        assert r.status_code == 409  # an ordinary message: "message the gym team instead"
        r = await api(c, "POST", f"/api/chat/{tid}/messages", {"body": "I have crushing chest pain and my left arm is numb"})
        assert r.status_code == 201, r.text
        assert r.json()["safety"]["alerted"] is True and r.json()["safety"]["number"] == "112"
        r = await post_form(c, f"/chat/{tid}/messages", {"body": "my chest pain is getting worse"})
        assert r.status_code == 303
        follow = await c.get(r.headers["location"].split("#")[0])
        assert "Safety first" in follow.text
    assert {n.user_id for n in await _urgent(db)} == {w["owner"].id}  # the admin, never the staff member who is away
    assert await db.scalar(trainer_notes) == before
    bodies = (await db.execute(select(ChatMessage.sender_role).where(ChatMessage.thread_id == tid)
                               .order_by(ChatMessage.id))).scalars().all()
    assert bodies.count("system") == 1 and bodies.count("customer") == 3


async def test_new_conversation_red_flag_to_a_staff_member_who_is_away_goes_to_the_gym_team(db):
    """F30 (/chat/new): a stale form naming a staff member who is away no longer refuses a red-flag first message -
    the gym team gets it (admin alerted); with no gym to send it to, the guidance is still shown."""
    w = await world(db)
    await _set_employment(db, w["staff"].id, "suspended")
    async with make_client() as c:
        await login_as(c, w["member"])
        r = await post_form(c, "/chat/new", {"target": f"{w['gym'].id}:{w['staff'].id}", "body": "Hello there"})
        assert r.status_code == 400 and "isn&#39;t available to message" in r.text  # an ordinary message: choose again
        r = await post_form(c, "/chat/new", {"target": f"{w['gym'].id}:{w['staff'].id}",
                                             "body": "I collapsed in the changing room"})
        assert r.status_code == 303 and r.headers["location"] == f"/chat/{w['threads']['team'].id}#latest"
    assert w["owner"].id in {n.user_id for n in await _urgent(db)}
    outsider = await make_customer(db)
    async with make_client() as c:
        await login_as(c, outsider)
        r = await post_form(c, "/chat/new", {"target": f"{w['gym'].id}:team", "body": "I have chest pain right now"})
        assert r.status_code == 400 and "112" in r.text and "Stop exercising" in r.text
