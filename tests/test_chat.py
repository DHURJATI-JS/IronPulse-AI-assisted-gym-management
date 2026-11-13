"""WP-F live chat: permissions, sending/receiving, unread/read, typing relay, deleted parties, e-mail throttle."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text

from app.core.utils import utcnow
from app.db import QueryCounter
from app.models import AuditLog, ChatMessage, ChatThread, EmailOutbox, Notification, User
from app.models.enums import EmploymentStatus, NotificationCategory, Role, SubscriptionStatus, ThreadStatus
from app.services import chat as chat_service
from app.services.chat import ChatError, ChatPermissionError
from tests.conftest import api, csrf_token, login_as, make_client, post_form
from tests.factories import (
    make_customer,
    make_employee,
    make_gym,
    make_owner,
    make_plans,
    make_subscription,
    make_user,
)


# ====================================================================== helpers
async def world(db) -> dict[str, Any]:
    """Owner + gym + plans + an active public trainer + a member with an active plan."""
    owner = await make_owner(db, full_name="Olivia Owner")
    gym = await make_gym(db, owner, name="Pulse Indiranagar")
    plans = await make_plans(db, gym)
    trainer = await make_employee(db, gym)
    member = await make_customer(db, full_name="Maya Member")
    await make_subscription(db, member, plans[0])
    return {"owner": owner, "gym": gym, "plans": plans, "trainer": trainer, "member": member}


async def make_thread(db, w: dict[str, Any], *, employee: bool = False, body: str | None = "Hello there") -> ChatThread:
    thread = await chat_service.get_or_create_thread(
        db, gym_id=w["gym"].id, customer_id=w["member"].id, employee_id=w["trainer"].id if employee else None)
    if body:
        await chat_service.post_message(db, thread=thread, sender=w["member"], body=body, sender_role="customer")
    await db.commit()
    return thread


async def staff_user(db, emp) -> User:
    return await db.get(User, emp.user_id)


class Captured:
    def __init__(self) -> None:
        self.events: list[tuple[list[int], dict[str, Any]]] = []

    async def publish(self, user_ids, evt):  # noqa: ANN001
        self.events.append((sorted(set(user_ids)), evt))


@pytest.fixture
def captured(monkeypatch):
    from app.services.realtime import hub

    cap = Captured()
    monkeypatch.setattr(hub, "publish", cap.publish)
    return cap


def no_csp_violations(html: str) -> None:
    assert "<script>" not in html and " onclick=" not in html and ' style="' not in html


# ====================================================================== pages
async def test_pages_render_for_every_role(db):
    w = await world(db)
    thread = await make_thread(db, w, employee=True)
    staff = await staff_user(db, w["trainer"])
    for user, urls in ((w["member"], ("/chat", f"/chat/{thread.id}", "/chat/new")),
                       (staff, ("/chat", f"/chat/{thread.id}")),
                       (w["owner"], ("/chat", f"/chat/{thread.id}", "/chat?scope=team", "/chat?status=closed&q=maya"))):
        async with make_client() as c:
            await login_as(c, user)
            for url in urls:
                r = await c.get(url)
                assert r.status_code == 200, (user.role, url, r.text[-2000:])
                no_csp_violations(r.text)
    async with make_client() as c:
        await login_as(c, w["member"])
        r = await c.get(f"/chat/{thread.id}")
        assert 'data-component="chat-thread"' in r.text and "Hello there" in r.text
        assert 'data-component="chat-list"' in r.text


async def test_anonymous_redirected_and_superadmin_forbidden(client, db):
    r = await client.get("/chat")
    assert r.status_code == 303 and "/auth/login" in r.headers["location"]
    admin = await make_user(db, role=Role.SUPERADMIN)
    await login_as(client, admin)
    assert (await client.get("/chat")).status_code == 403


# ====================================================================== starting conversations
async def test_member_starts_team_and_trainer_threads(client, db):
    w = await world(db)
    await login_as(client, w["member"])
    r = await client.get("/chat/new")
    assert r.status_code == 200 and "Pulse Indiranagar team" in r.text and w["trainer"].full_name in r.text

    r = await post_form(client, "/chat/new", {"target": f"{w['gym'].id}:team", "body": "Can I get a locker?"})
    assert r.status_code == 303
    team = (await db.execute(select(ChatThread).where(ChatThread.employee_id.is_(None)))).scalar_one()
    assert r.headers["location"] == f"/chat/{team.id}"
    assert team.staff_unread == 1 and team.customer_unread == 0 and team.last_message_preview == "Can I get a locker?"

    r = await post_form(client, "/chat/new", {"target": f"{w['gym'].id}:{w['trainer'].id}", "body": "Is Monday ok?"})
    assert r.status_code == 303
    with_trainer = (await db.execute(select(ChatThread).where(ChatThread.employee_id == w["trainer"].id))).scalar_one()
    # team messages notify the owner, trainer messages notify the trainer
    owner_n = (await db.execute(select(Notification).where(Notification.user_id == w["owner"].id))).scalars().all()
    trainer_n = (await db.execute(select(Notification).where(Notification.user_id == w["trainer"].user_id))).scalars().all()
    assert [n.link for n in owner_n] == [f"/chat/{team.id}"] and owner_n[0].category == NotificationCategory.MESSAGE
    assert [n.link for n in trainer_n] == [f"/chat/{with_trainer.id}"]

    # writing to the same person again re-uses the existing thread
    r = await post_form(client, "/chat/new", {"target": f"{w['gym'].id}:team", "body": "Thanks!"})
    assert r.headers["location"] == f"/chat/{team.id}"
    assert await db.scalar(select(func.count(ChatThread.id))) == 2


async def test_non_subscriber_cannot_start(client, db):
    w = await world(db)
    outsider = await make_customer(db)
    await login_as(client, outsider)
    r = await client.get("/chat/new")
    assert r.status_code == 200 and "Chat opens with an active membership" in r.text
    r = await post_form(client, "/chat/new", {"target": f"{w['gym'].id}:team", "body": "Hello?"})
    assert r.status_code == 400
    assert await db.scalar(select(func.count(ChatThread.id))) == 0
    with pytest.raises(ChatPermissionError):
        await chat_service.get_or_create_thread(db, gym_id=w["gym"].id, customer_id=outsider.id, employee_id=None)
    # an expired membership does not allow new conversations either
    expired = await make_customer(db)
    await make_subscription(db, expired, w["plans"][1], status=SubscriptionStatus.EXPIRED)
    with pytest.raises(ChatPermissionError):
        await chat_service.get_or_create_thread(db, gym_id=w["gym"].id, customer_id=expired.id, employee_id=None)


async def test_cannot_target_other_gym_or_inactive_staff(client, db):
    w = await world(db)
    other_owner = await make_owner(db)
    other_gym = await make_gym(db, other_owner)
    foreign = await make_employee(db, other_gym)
    invited = await make_employee(db, w["gym"], status=EmploymentStatus.INVITED)
    await login_as(client, w["member"])
    for target in (f"{other_gym.id}:{foreign.id}", f"{w['gym'].id}:{foreign.id}", f"{w['gym'].id}:{invited.id}",
                   "garbage", f"{w['gym'].id}:abc"):
        r = await post_form(client, "/chat/new", {"target": target, "body": "hi"})
        assert r.status_code == 400, target
    with pytest.raises(ChatError):
        await chat_service.get_or_create_thread(db, gym_id=w["gym"].id, customer_id=w["member"].id, employee_id=foreign.id)
    assert await db.scalar(select(func.count(ChatThread.id))) == 0


# ====================================================================== visibility
async def test_other_gym_staff_and_owner_get_404(db):
    w = await world(db)
    thread = await make_thread(db, w, employee=True)
    other_owner = await make_owner(db)
    other_gym = await make_gym(db, other_owner)
    other_staff = await staff_user(db, await make_employee(db, other_gym))
    same_gym_staff = await staff_user(db, await make_employee(db, w["gym"]))
    stranger = await make_customer(db)
    for user in (other_owner, other_staff, same_gym_staff, stranger):
        async with make_client() as c:
            await login_as(c, user)
            assert (await c.get(f"/chat/{thread.id}")).status_code == 404, user.role
            assert (await c.get(f"/api/chat/{thread.id}/messages")).status_code == 404
            assert (await api(c, "POST", f"/api/chat/{thread.id}/messages", json={"body": "x"})).status_code == 404
            assert (await api(c, "POST", f"/api/chat/{thread.id}/read", json={})).status_code == 404
            r = await c.get("/chat")
            assert "Hello there" not in r.text
    async with make_client() as c:
        await login_as(c, w["owner"])
        r = await c.get("/chat")
        assert r.status_code == 200 and "Maya Member" in r.text and "Hello there" in r.text


async def test_owner_sees_all_threads_of_own_gyms_only(db):
    w = await world(db)
    await make_thread(db, w)
    await make_thread(db, w, employee=True)
    other_owner = await make_owner(db)
    other_gym = await make_gym(db, other_owner)
    other_plans = await make_plans(db, other_gym)
    await make_subscription(db, w["member"], other_plans[0])
    await chat_service.get_or_create_thread(db, gym_id=other_gym.id, customer_id=w["member"].id, employee_id=None)
    await db.commit()
    mine = await chat_service.list_threads(db, w["owner"])
    assert {t.gym_id for t in mine} == {w["gym"].id} and len(mine) == 2
    theirs = await chat_service.list_threads(db, other_owner)
    assert {t.gym_id for t in theirs} == {other_gym.id}
    assert len(await chat_service.list_threads(db, w["member"])) == 3
    assert len(await chat_service.list_threads(db, w["owner"], scope="team")) == 1


# ====================================================================== sending / receiving / read state
async def test_send_receive_unread_and_read(db, captured):
    w = await world(db)
    thread = await make_thread(db, w, employee=True, body=None)
    staff = await staff_user(db, w["trainer"])
    async with make_client() as member_c, make_client() as staff_c:
        await login_as(member_c, w["member"])
        await login_as(staff_c, staff)
        r = await api(member_c, "POST", f"/api/chat/{thread.id}/messages", json={"body": "  Hi coach!  "})
        assert r.status_code == 201, r.text
        msg = r.json()["message"]
        assert msg["body"] == "Hi coach!" and msg["sender_role"] == "customer" and msg["sender_name"] == "Maya Member"
        assert {"id", "body", "sender_name", "sender_role", "sender_photo", "created_at"} <= set(msg)

        # pushed live (after commit) to the member, the trainer and the owner
        live = [e for e in captured.events if e[1]["type"] == "chat.message"]
        assert live and live[-1][0] == sorted({w["member"].id, staff.id, w["owner"].id})
        assert live[-1][1]["thread_id"] == thread.id and live[-1][1]["message"]["id"] == msg["id"]

        await db.refresh(thread)
        assert thread.staff_unread == 1 and thread.customer_unread == 0
        r = await staff_c.get(f"/api/chat/{thread.id}/messages")
        assert r.status_code == 200 and [m["body"] for m in r.json()["items"]] == ["Hi coach!"]
        r = await api(staff_c, "POST", f"/api/chat/{thread.id}/read", json={})
        assert r.json() == {"ok": True, "changed": True}
        await db.refresh(thread)
        assert thread.staff_unread == 0

        r = await api(staff_c, "POST", f"/api/chat/{thread.id}/messages", json={"body": "Hi Maya, see you Monday"})
        assert r.status_code == 201 and r.json()["message"]["sender_role"] == "staff"
        await db.refresh(thread)
        assert thread.customer_unread == 1
        # opening the conversation page marks it read too
        r = await member_c.get(f"/chat/{thread.id}")
        assert r.status_code == 200 and "Hi Maya, see you Monday" in r.text
        await db.refresh(thread)
        assert thread.customer_unread == 0

        # polling with ?after= returns only newer messages; ?before= pages backwards
        r = await member_c.get(f"/api/chat/{thread.id}/messages?after={msg['id']}")
        assert [m["body"] for m in r.json()["items"]] == ["Hi Maya, see you Monday"]
        newest = r.json()["items"][0]["id"]
        r = await member_c.get(f"/api/chat/{thread.id}/messages?before={newest}&limit=50")
        assert [m["id"] for m in r.json()["items"]] == [msg["id"]] and r.json()["has_more"] is False


async def test_owner_replies_as_owner_and_paging(db):
    w = await world(db)
    thread = await make_thread(db, w, body=None)
    for i in range(55):
        await chat_service.post_message(db, thread=thread, sender=w["member"], body=f"msg {i}", sender_role="customer")
    await db.commit()
    async with make_client() as c:
        await login_as(c, w["owner"])
        r = await api(c, "POST", f"/api/chat/{thread.id}/messages", json={"body": "Owner here"})
        assert r.status_code == 201 and r.json()["message"]["sender_role"] == "owner"
        r = await c.get(f"/api/chat/{thread.id}/messages")
        data = r.json()
        assert len(data["items"]) == 50 and data["has_more"] is True and data["items"][-1]["body"] == "Owner here"
        r = await c.get(f"/chat/{thread.id}")
        assert "Earlier messages" in r.text
        r = await c.get(f"/chat/{thread.id}?before={data['items'][0]['id']}")
        assert r.status_code == 200 and "msg 0" in r.text and "Jump to latest" in r.text


async def test_message_validation(db):
    w = await world(db)
    thread = await make_thread(db, w)
    async with make_client() as c:
        await login_as(c, w["member"])
        for payload in ({"body": ""}, {"body": "   "}, {"body": "x" * 4001}, {"body": 12}, {"text": "hi"}, ["hi"]):
            r = await api(c, "POST", f"/api/chat/{thread.id}/messages", json=payload)
            assert r.status_code == 400, payload
        r = await c.post(f"/api/chat/{thread.id}/messages", content=b"not json",
                         headers={"X-CSRF-Token": await csrf_token(c), "Content-Type": "application/json"})
        assert r.status_code == 400
        r = await api(c, "POST", f"/api/chat/{thread.id}/messages", json={"body": "x" * 4000})
        assert r.status_code == 201
        # control characters are stripped, HTML is stored as text and escaped on render
        r = await api(c, "POST", f"/api/chat/{thread.id}/messages", json={"body": "<b>bold</b>\x00"})
        assert r.status_code == 201 and r.json()["message"]["body"] == "<b>bold</b>"
        page = await c.get(f"/chat/{thread.id}")
        assert "&lt;b&gt;bold&lt;/b&gt;" in page.text and "<b>bold</b>" not in page.text
    with pytest.raises(ChatPermissionError):
        await chat_service.post_message(db, thread=thread, sender=w["owner"], body="hi", sender_role="customer")
    with pytest.raises(ChatError):
        await chat_service.post_message(db, thread=thread, sender=w["member"], body="hi", sender_role="robot")


async def test_no_js_composer_fallback(client, db):
    w = await world(db)
    thread = await make_thread(db, w)
    await login_as(client, w["owner"])
    r = await post_form(client, f"/chat/{thread.id}/messages", {"body": "Reply without JavaScript"})
    assert r.status_code == 303 and r.headers["location"] == f"/chat/{thread.id}#latest"
    assert await db.scalar(select(func.count(ChatMessage.id)).where(ChatMessage.sender_role == "owner")) == 1
    r = await post_form(client, f"/chat/{thread.id}/messages", {"body": ""})
    assert r.status_code == 303  # flashed error, nothing stored
    assert await db.scalar(select(func.count(ChatMessage.id))) == 2


# ====================================================================== closing
async def test_close_and_reopen(client, db):
    w = await world(db)
    thread = await make_thread(db, w)
    async with make_client() as member_c:
        await login_as(member_c, w["member"])
        r = await post_form(member_c, f"/chat/{thread.id}/close")
        assert r.status_code == 403
    await login_as(client, w["owner"])
    r = await post_form(client, f"/chat/{thread.id}/close")
    assert r.status_code == 303
    await db.refresh(thread)
    assert thread.status == ThreadStatus.CLOSED
    assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "chat.close")) == 1
    notes = (await db.execute(select(Notification).where(Notification.user_id == w["member"].id))).scalars().all()
    assert any("closed" in n.title.lower() for n in notes)
    async with make_client() as member_c:
        await login_as(member_c, w["member"])
        r = await api(member_c, "POST", f"/api/chat/{thread.id}/messages", json={"body": "hello?"})
        assert r.status_code == 409
        page = await member_c.get(f"/chat/{thread.id}")
        assert "closed by the gym" in page.text and "Write again" in page.text
        # writing again via /chat/new reopens the same conversation
        r = await post_form(member_c, "/chat/new", {"target": f"{w['gym'].id}:team", "body": "Me again"})
        assert r.status_code == 303 and r.headers["location"] == f"/chat/{thread.id}"
    await db.refresh(thread)
    assert thread.status == ThreadStatus.OPEN


# ====================================================================== typing indicator (WebSocket handler)
async def test_typing_handler_permission_and_throttle(db, captured):
    from app.routers import chat as chat_router
    from app.routers.core import ws_handlers

    assert ws_handlers.get("chat.typing") is chat_router.ws_typing
    chat_router._typing_last.clear()
    w = await world(db)
    thread = await make_thread(db, w, employee=True)
    staff = await staff_user(db, w["trainer"])
    stranger = await make_customer(db)

    def typing() -> list[tuple[list[int], dict[str, Any]]]:
        return [e for e in captured.events if e[1]["type"] == "chat.typing"]

    await chat_router.ws_typing(stranger, {"type": "chat.typing", "thread_id": thread.id})
    await chat_router.ws_typing(w["member"], {"type": "chat.typing", "thread_id": "nope"})
    assert typing() == []

    await chat_router.ws_typing(w["member"], {"type": "chat.typing", "thread_id": thread.id})
    assert len(typing()) == 1
    targets, evt = typing()[0]
    assert targets == sorted({staff.id, w["owner"].id})
    assert evt["thread_id"] == thread.id and evt["name"] == "Maya" and evt["user_id"] == w["member"].id
    await chat_router.ws_typing(w["member"], {"type": "chat.typing", "thread_id": thread.id})  # within 2 s
    assert len(typing()) == 1
    await chat_router.ws_typing(staff, {"type": "chat.typing", "thread_id": thread.id})
    assert typing()[-1][0] == sorted({w["member"].id, w["owner"].id})
    chat_router._typing_last.clear()


# ====================================================================== deleted parties
async def test_deleted_member_thread_is_read_only(client, db):
    from app.services.accounts import soft_delete_user

    w = await world(db)
    thread = await make_thread(db, w, employee=True)
    await soft_delete_user(db, w["member"], reason="user request")
    await db.commit()
    staff = await staff_user(db, w["trainer"])
    await login_as(client, staff)
    r = await client.get(f"/chat/{thread.id}")
    assert r.status_code == 200
    assert "Deleted user" in r.text and "Maya Member" not in r.text
    assert "This account was deleted. The conversation will be permanently deleted on" in r.text
    assert "data-chat-form" not in r.text
    r = await api(client, "POST", f"/api/chat/{thread.id}/messages", json={"body": "still there?"})
    assert r.status_code == 409
    r = await client.get("/chat")
    assert "Deleted user" in r.text and "Read-only" in r.text


async def test_deleted_staff_thread_is_read_only(client, db):
    from app.services.accounts import soft_delete_user

    w = await world(db)
    thread = await make_thread(db, w, employee=True)
    staff = await staff_user(db, w["trainer"])
    await soft_delete_user(db, staff, reason="left")
    await db.commit()
    await login_as(client, w["member"])
    r = await client.get(f"/chat/{thread.id}")
    assert "Deleted staff member" in r.text and "permanently deleted on" in r.text
    r = await api(client, "POST", f"/api/chat/{thread.id}/messages", json={"body": "hello"})
    assert r.status_code == 409


# ====================================================================== notifications / e-mail throttle
async def test_offline_recipient_gets_one_email_per_15_minutes(db, monkeypatch):
    from app.services.realtime import hub

    w = await world(db)
    thread = await make_thread(db, w, body=None)
    for text in ("one", "two", "three"):
        await chat_service.post_message(db, thread=thread, sender=w["member"], body=text, sender_role="customer")
    await db.commit()
    owner_notes = (await db.execute(select(Notification).where(Notification.user_id == w["owner"].id))).scalars().all()
    assert len(owner_notes) == 3 and sum(1 for n in owner_notes if n.emailed) == 1
    assert await db.scalar(select(func.count(EmailOutbox.id)).where(EmailOutbox.user_id == w["owner"].id)) == 1

    # after 15 minutes another e-mail is allowed
    for n in owner_notes:
        n.created_at = utcnow() - timedelta(minutes=16)
    await db.commit()
    await chat_service.post_message(db, thread=thread, sender=w["member"], body="four", sender_role="customer")
    await db.commit()
    assert await db.scalar(select(func.count(EmailOutbox.id)).where(EmailOutbox.user_id == w["owner"].id)) == 2

    # online recipients get the live event only (no notification, no e-mail)
    monkeypatch.setattr(hub, "is_online", lambda uid: True)
    before = await db.scalar(select(func.count(Notification.id)))
    await chat_service.post_message(db, thread=thread, sender=w["owner"], body="I'm online", sender_role="owner")
    await db.commit()
    assert await db.scalar(select(func.count(Notification.id))) == before


async def test_staff_reply_notifies_member_with_guardian_cc(db):
    from datetime import date

    from app.models import CustomerProfile

    w = await world(db)
    minor = await make_customer(db, full_name="Teen Member", date_of_birth=date.today().replace(year=date.today().year - 15))
    prof = await db.get(CustomerProfile, minor.id)
    prof.guardian_email = "parent@example.com"
    await make_subscription(db, minor, w["plans"][0])
    thread = await chat_service.get_or_create_thread(db, gym_id=w["gym"].id, customer_id=minor.id,
                                                     employee_id=w["trainer"].id)
    staff = await staff_user(db, w["trainer"])
    await chat_service.post_message(db, thread=thread, sender=staff, body="Remember your inhaler", sender_role="staff")
    await db.commit()
    mail = (await db.execute(select(EmailOutbox).where(EmailOutbox.user_id == minor.id))).scalar_one()
    assert mail.cc == ["parent@example.com"]


# ====================================================================== N+1 guard
async def test_chat_list_constant_queries(db):
    w = await world(db)
    staff = await staff_user(db, w["trainer"])

    async def add_members(n: int) -> None:
        for _ in range(n):
            m = await make_customer(db)
            await make_subscription(db, m, w["plans"][0])
            t = await chat_service.get_or_create_thread(db, gym_id=w["gym"].id, customer_id=m.id,
                                                        employee_id=w["trainer"].id)
            await chat_service.post_message(db, thread=t, sender=m, body="hi coach", sender_role="customer")
        await db.commit()

    async def count(user: User) -> int:
        async with make_client() as c:
            await login_as(c, user)
            with QueryCounter() as qc:
                r = await c.get("/chat")
            assert r.status_code == 200
            return qc.count

    await add_members(2)
    small_owner, small_staff = await count(w["owner"]), await count(staff)
    await add_members(10)
    assert await count(w["owner"]) == small_owner
    assert await count(staff) == small_staff


# ====================================================================== online check across worker processes
# With several workers (REALTIME_PG_BRIDGE) a recipient's socket may live in another process: chat asks presence's
# cross-process view (Redis leases, one pipelined round trip) before it adds a notification and an e-mail on top of
# the live event. Without Redis - or with one process - this process's sockets decide (the safe side).
class _LeasePipe:
    def __init__(self, redis: "_LeaseRedis") -> None:
        self.redis, self.keys = redis, []

    def zrangebyscore(self, key: str, lo: str, hi: str) -> "_LeasePipe":
        assert hi == "+inf" and lo.startswith("(")  # only unexpired leases: score > now
        self.keys.append((key, float(lo[1:])))
        return self

    async def execute(self) -> list[list[str]]:
        self.redis.round_trips += 1
        self.redis.asked.append([k for k, _ in self.keys])
        if self.redis.broken:
            raise ConnectionError("redis is down")
        return [[m for m, until in sorted(self.redis.leases.get(k, {}).items()) if until > now] for k, now in self.keys]

    async def __aenter__(self) -> "_LeasePipe":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _LeaseRedis:
    """presence's lease ZSETs (``presence:<uid>``: process id -> lease expiry), in memory."""

    def __init__(self) -> None:
        self.leases: dict[str, dict[str, float]] = {}
        self.round_trips = 0
        self.asked: list[list[str]] = []  # the lease keys read, per round trip
        self.broken = False

    def pipeline(self, transaction: bool = True) -> _LeasePipe:
        return _LeasePipe(self)


@pytest.fixture
def several_workers(monkeypatch):
    """Several worker processes (REALTIME_PG_BRIDGE) sharing presence through a (fake) Redis."""
    from app.config import get_settings
    from app.services.presence import presence

    fake = _LeaseRedis()

    async def redis() -> _LeaseRedis:
        return fake

    monkeypatch.setattr(get_settings(), "realtime_pg_bridge", True)
    monkeypatch.setattr(presence, "_redis", redis)
    return fake


def _lease(fake: _LeaseRedis, user_id: int, holder: str, seconds: float) -> None:
    import time

    from app.services.presence import presence

    fake.leases.setdefault(presence._lease_key(user_id), {})[holder] = time.time() + seconds


async def _owner_notes(db, owner_id: int) -> tuple[int, int]:
    notes = await db.scalar(select(func.count(Notification.id)).where(Notification.user_id == owner_id))
    mails = await db.scalar(select(func.count(EmailOutbox.id)).where(EmailOutbox.user_id == owner_id))
    return int(notes or 0), int(mails or 0)


async def test_a_recipient_online_on_another_worker_gets_the_live_event_only(db, several_workers):
    w = await world(db)
    thread = await make_thread(db, w, body=None)
    before = await _owner_notes(db, w["owner"].id)
    _lease(several_workers, w["owner"].id, "worker-b:4242:abcdef", 30)  # the owner's tab lives on another worker
    await chat_service.post_message(db, thread=thread, sender=w["member"], body="Are you there?", sender_role="customer")
    await db.commit()
    assert await _owner_notes(db, w["owner"].id) == before  # no notification, no e-mail: they saw it live
    assert several_workers.round_trips == 1

    several_workers.leases.clear()  # that worker's lease ran out: they left
    _lease(several_workers, w["owner"].id, "worker-b:4242:abcdef", -1)
    await chat_service.post_message(db, thread=thread, sender=w["member"], body="Hello?", sender_role="customer")
    await db.commit()
    assert await _owner_notes(db, w["owner"].id) == (before[0] + 1, before[1] + 1)


async def test_only_this_processs_own_lease_never_counts_as_online(db, several_workers):
    """This process's own sockets are the truth here: its lease (kept through the grace period after the last tab
    closed) must not make the recipient look online."""
    from app.services.presence import presence

    w = await world(db)
    thread = await make_thread(db, w, body=None)
    _lease(several_workers, w["owner"].id, presence.process_id, 30)
    await chat_service.post_message(db, thread=thread, sender=w["member"], body="Hi!", sender_role="customer")
    await db.commit()
    assert (await _owner_notes(db, w["owner"].id))[0] == 1


async def test_every_recipient_is_checked_in_one_round_trip_and_local_sockets_need_none(db, several_workers):
    from app.services.presence import presence
    from app.services.realtime import hub

    w = await world(db)
    thread = await make_thread(db, w, employee=True, body=None)
    staff = await staff_user(db, w["trainer"])
    # an owner's message notifies the member and the trainer: both are checked in ONE pipelined round trip
    _lease(several_workers, staff.id, "worker-c:77:fedcba", 30)
    await chat_service.post_message(db, thread=thread, sender=w["owner"], body="Welcome both!", sender_role="owner")
    await db.commit()
    assert several_workers.round_trips == 1
    assert sorted(several_workers.asked[0]) == sorted(presence._lease_key(u) for u in (w["member"].id, staff.id))
    member_notes = await db.scalar(select(func.count(Notification.id)).where(Notification.user_id == w["member"].id))
    staff_notes = await db.scalar(select(func.count(Notification.id)).where(Notification.user_id == staff.id))
    assert (member_notes, staff_notes) == (1, 0)  # the trainer is online on worker C, the member nowhere

    q = hub.subscribe(w["member"].id)  # the member opens a tab in this process: no Redis needed for them
    try:
        await chat_service.post_message(db, thread=thread, sender=w["owner"], body="See you at 6", sender_role="owner")
        await db.commit()
    finally:
        hub.unsubscribe(w["member"].id, q)
    assert several_workers.round_trips == 2 and several_workers.asked[1] == [presence._lease_key(staff.id)]
    member_notes = await db.scalar(select(func.count(Notification.id)).where(Notification.user_id == w["member"].id))
    staff_notes = await db.scalar(select(func.count(Notification.id)).where(Notification.user_id == staff.id))
    assert (member_notes, staff_notes) == (1, 0)  # both online: one here, one on worker C


async def test_redis_trouble_falls_back_to_this_processs_sockets(db, several_workers):
    w = await world(db)
    thread = await make_thread(db, w, body=None)
    _lease(several_workers, w["owner"].id, "worker-b:4242:abcdef", 30)
    several_workers.broken = True
    await chat_service.post_message(db, thread=thread, sender=w["member"], body="Anyone?", sender_role="customer")
    await db.commit()  # never a 500: judged by this process's sockets - a notification too many, never one missing
    assert (await _owner_notes(db, w["owner"].id))[0] == 1


@pytest.mark.parametrize("mode", ["no-redis", "one-process"])
async def test_without_a_cross_process_view_this_processs_sockets_decide(db, monkeypatch, mode):
    from app.config import get_settings
    from app.services.presence import presence
    from app.services.realtime import hub

    fake = _LeaseRedis()

    async def redis() -> _LeaseRedis | None:
        return None if mode == "no-redis" else fake

    monkeypatch.setattr(get_settings(), "realtime_pg_bridge", mode == "no-redis")
    monkeypatch.setattr(presence, "_redis", redis)
    w = await world(db)
    thread = await make_thread(db, w, body=None)
    _lease(fake, w["owner"].id, "worker-b:4242:abcdef", 30)  # one process: another process's events never reach it
    await chat_service.post_message(db, thread=thread, sender=w["member"], body="One", sender_role="customer")
    await db.commit()
    assert (await _owner_notes(db, w["owner"].id))[0] == 1 and fake.round_trips == 0
    q = hub.subscribe(w["owner"].id)  # the owner's socket in this process: online, no notification
    try:
        await chat_service.post_message(db, thread=thread, sender=w["member"], body="Two", sender_role="customer")
        await db.commit()
    finally:
        hub.unsubscribe(w["owner"].id, q)
    assert (await _owner_notes(db, w["owner"].id))[0] == 1 and fake.round_trips == 0


async def test_online_check_against_a_real_redis(db, monkeypatch):
    """The lease read in the real Redis protocol (exclusive '(now' bound, decoded members) - skipped without Redis."""
    import time

    from app.config import get_settings
    from app.security.sessions import get_redis
    from app.services.presence import presence
    from app.services.realtime import hub

    r = await get_redis()
    if r is None:
        pytest.skip("Redis is not configured")
    try:
        await r.ping()
    except Exception:  # noqa: BLE001
        pytest.skip("Redis is not reachable")
    monkeypatch.setattr(get_settings(), "realtime_pg_bridge", True)
    w = await world(db)
    there, gone, mine = w["owner"].id, w["member"].id, w["trainer"].user_id
    keys = [presence._lease_key(u) for u in (there, gone, mine)]
    try:
        await r.zadd(keys[0], {"worker-b:1:aaaaaa": time.time() + 30})
        await r.zadd(keys[1], {"worker-b:1:aaaaaa": time.time() - 5})
        await r.zadd(keys[2], {presence.process_id: time.time() + 30})
        assert await hub.online_anywhere([there, gone, mine, None]) == {there}
    finally:
        await r.delete(*keys)


# ====================================================================== invisible / bidi-only messages (F33)
async def test_invisible_and_bidi_only_messages_are_refused_and_overrides_stripped(db):
    """F33: zero-width / bidi-only bodies are "Type a message first" (no blank bubble, no blank notification), bidi
    overrides are removed from what is stored ("pay <RLO>gnp.exe" can't read "pay exe.png"), while ZWJ / ZWNJ / RLM
    inside real text (emoji sequences, Indic and right-to-left scripts) are kept."""
    w = await world(db)
    thread = await make_thread(db, w, employee=True)
    staff = await staff_user(db, w["trainer"])
    staff_notes = select(func.count(Notification.id)).where(Notification.user_id == staff.id)
    before = await db.scalar(staff_notes)
    async with make_client() as c:
        await login_as(c, w["member"])
        for body in ("\u200b\u200b\u200b", "\u202e", "\u200d\u2060\ufeff", "  \u200b \n ", "\u2066\u2069", "\u3164",
                     "\u0301\u0301", "\u2800", "\u00ad"):
            r = await api(c, "POST", f"/api/chat/{thread.id}/messages", json={"body": body})
            assert r.status_code == 400 and r.json()["detail"] == "Type a message first.", repr(body)
        r = await post_form(c, f"/chat/{thread.id}/messages", {"body": "\u200b\u202e"})
        assert r.status_code == 303  # no-JS: the same refusal as a flash
        assert await db.scalar(staff_notes) == before
        assert await db.scalar(select(func.count(ChatMessage.id)).where(ChatMessage.thread_id == thread.id)) == 1
        r = await api(c, "POST", f"/api/chat/{thread.id}/messages", json={"body": "pay \u202egnp.exe"})
        assert r.status_code == 201 and r.json()["message"]["body"] == "pay gnp.exe"
        for body in ("\u0928\u092e\u0938\u094d\u0924\u0947\u200d",  # Hindi + ZWJ
                     "family day \U0001F468\u200d\U0001F469\u200d\U0001F467",  # an emoji ZWJ sequence
                     "\u200f\u0645\u0631\u062d\u0628\u0627"):  # RLM + Arabic
            r = await api(c, "POST", f"/api/chat/{thread.id}/messages", json={"body": body})
            assert r.status_code == 201 and r.json()["message"]["body"] == body, repr(body)
    stored = (await db.execute(select(ChatMessage.body).where(ChatMessage.thread_id == thread.id))).scalars().all()
    assert not any(ch in b for b in stored for ch in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200b")


async def test_notification_titles_carry_no_bidi_overrides(db):
    """F33: a name carrying a bidi override can't reorder the bell / e-mail title ("New message from ...")."""
    w = await world(db)
    member = await db.get(User, w["member"].id)
    member.full_name = "Maya \u202eMember"
    await db.commit()
    await make_thread(db, w, employee=True, body="Hello coach")
    staff = await staff_user(db, w["trainer"])
    note = (await db.execute(select(Notification).where(Notification.user_id == staff.id)
                             .order_by(Notification.id.desc()).limit(1))).scalar_one()
    assert "\u202e" not in note.title and "Maya Member" in note.title


# ====================================================================== polling never skips a message (F31)
async def _until_a_writer_waits(session, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        waiting = await session.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                                            "AND wait_event_type = 'Lock'"))
        await session.commit()
        if waiting:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the second writer never waited for the first")


async def test_polling_after_the_newest_id_never_skips_a_late_commit(db):
    """F31: a message's id is handed out under the conversation's row lock - in commit order within a conversation - so
    60-chat.js's poll (``?after=<newest id seen>``) can't miss a reply that commits after a higher id. Deterministic:
    the trainer's reply waits for the member's open transaction, which then posts again and commits first."""
    from app.db import get_sessionmaker

    w = await world(db)
    thread = await make_thread(db, w, employee=True)
    staff = await staff_user(db, w["trainer"])
    sessions = get_sessionmaker()
    async with sessions() as a, sessions() as b, sessions() as poller:
        ta, member = await a.get(ChatThread, thread.id), await a.get(User, w["member"].id)
        await chat_service.post_message(a, thread=ta, sender=member, body="first", sender_role="customer")
        tb, trainer = await b.get(ChatThread, thread.id), await b.get(User, staff.id)
        reply = asyncio.create_task(chat_service.post_message(b, thread=tb, sender=trainer, body="on my way",
                                                              sender_role="staff"))
        await _until_a_writer_waits(poller)
        await chat_service.post_message(a, thread=ta, sender=member, body="second", sender_role="customer")
        await a.commit()
        seen, _ = await chat_service.list_messages(poller, thread.id, after=0)
        await poller.commit()
        assert [m.body for m in seen][-2:] == ["first", "second"]
        last_id = max(m.id for m in seen)
        await asyncio.wait_for(reply, 10)
        await b.commit()
        newer, _ = await chat_service.list_messages(poller, thread.id, after=last_id)
        assert [m.body for m in newer] == ["on my way"]
