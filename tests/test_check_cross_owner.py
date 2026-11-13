"""Regression tests of the Check step: the cross-owner requests of this round's builders, applied in files the builders
did not own. One test (or more) per request - each fails without its fix.

Trust & safety: a restore never lifts a ban; suspended staff can't chat; the registry matches a national phone number
with its country. Trainer safety: the profile page, the live coach's discomfort alerts and the assistant respect the
member's "share health details" choice and red flags. Caps: wellness check-ins per day, the gym slots line on /me,
staff documents charged to the gym's storage quota. Sessions: re-authentication in flight, /ws 4409 and its
subscribe race, namespaced Redis keys and no Redis wait inside a transaction, the unknown-account lockout mirror,
X-Submit-Id in CORS, the AI repeat key. Platform: the mailer and the periodic runner hold no lock or transaction
across slow work, the IPv6 /64 bans on the IP page, the user_sessions address index. LLM fallback: the Docker
model's first-token allowance, the simulated voice and AI rows of the self-check.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select, text

from app import db as dbmod
from app.config import BASE_DIR, get_settings
from app.core import caps
from app.core.utils import utcnow
from app.models import (
    AccountBan,
    ChatThread,
    CustomerProfile,
    EmailOutbox,
    EmployeeDocument,
    IpBan,
    User,
    UserSession,
    WellnessCheck,
)
from app.models.enums import EmailStatus, EmploymentStatus, Role
from app.services import chat as chat_service
from app.services import trainer_safety as ts
from tests.conftest import api, login_as, make_client, post_form
from tests.factories import (
    DEFAULT_PASSWORD,
    make_customer,
    make_employee,
    make_gym,
    make_owner,
    make_plans,
    make_subscription,
    make_user,
)
from tests.test_trainer_safety import low_caps, set_sharing, world  # noqa: F401 - low_caps is a fixture

ROOT = BASE_DIR


async def page(user: User | None, url: str):
    async with make_client() as c:
        if user is not None:
            await login_as(c, user)
        return await c.get(url)


# ====================================================================== trust & safety
async def test_restoring_a_deleted_account_never_lifts_its_ban(db):
    admin = await make_user(db, role=Role.SUPERADMIN)
    gone = await make_customer(db, full_name="Banned Then Deleted")
    gone.deleted_at, gone.purge_after, gone.is_active = utcnow() - timedelta(days=1), utcnow() + timedelta(days=20), False
    gone.deletion_reason = "user request"
    db.add(AccountBan(user_id=gone.id, person_name=gone.full_name, source="severe", reason="Severe violation at a gym"))
    await db.commit()
    gid = gone.id
    async with make_client() as c:
        await login_as(c, admin)
        r = await post_form(c, f"/superadmin/users/{gid}/restore", {})
        assert r.status_code == 303
        db.expire_all()
        user = await db.get(User, gid)
        assert user.deleted_at is not None and user.is_active is False  # still deleted, still disabled
        ban = await db.scalar(select(AccountBan).where(AccountBan.user_id == gid))
        ban.lifted_at = utcnow()  # the super-admin lifts the ban on /superadmin/trust first ...
        await db.commit()
        r = await post_form(c, f"/superadmin/users/{gid}/restore", {})
        assert r.status_code == 303
    db.expire_all()
    user = await db.get(User, gid)
    assert user.deleted_at is None and user.is_active is True  # ... then the restore works


async def test_a_suspended_trainer_can_neither_send_nor_receive_chat_messages(db):
    w = await world(db)
    member_id = w["member"].id
    trainer = w["trainer"]
    trainer.status = EmploymentStatus.SUSPENDED
    await db.commit()
    tid = w["threads"]["trainer"].id
    async with make_client() as c:
        await login_as(c, w["member"])
        r = await api(c, "POST", f"/api/chat/{tid}/messages", {"body": "Are we training today?"})
        assert r.status_code >= 400 and "isn't available for messages" in r.text
        html = (await c.get(f"/chat/{tid}")).text
        assert "available for messages" in html  # the read-only notice instead of the composer
    async with make_client() as c:
        await login_as(c, w["trainer_user"])
        r = await api(c, "POST", f"/api/chat/{tid}/messages", {"body": "Still here"})
        assert r.status_code >= 400
    db.expire_all()
    thread = await chat_service.load_thread(db, await db.get(User, member_id), tid)
    assert thread is not None and chat_service.read_only_state(thread).kind == "suspended"


async def test_the_registry_matches_a_national_phone_number_with_its_country(db):
    from app.services import auth_flows as af
    from app.services import trust_safety as TS

    gone = await make_customer(db)
    await TS.register_entries(db, [("phone", TS.phone_hash("+44 7700 900123"))], reason="banned", user_id=gone.id,
                              actor=None)
    await db.commit()
    assert await af.blocked_identity(db, phone="07700 900123", country_code="GB") == TS.BLOCKED_MESSAGES["phone"]
    assert await af.blocked_identity(db, phone="+447700900123") == TS.BLOCKED_MESSAGES["phone"]
    assert await af.blocked_identity(db, phone="07700 900124", country_code="GB") is None
    assert await af.blocked_identity(db, phone="07700 900123", country_code="GB", exclude_user_id=gone.id) is None


# ====================================================================== trainer safety
async def test_the_profile_page_respects_the_members_sharing_choice(db):
    w = await world(db)
    url = f"/profile/{w['member'].id}"
    shared = await page(w["trainer_user"], url)
    assert shared.status_code == 200 and "Torn ACL" in shared.text
    await set_sharing(db, w["member"], False)
    withheld = await page(w["trainer_user"], url)
    assert withheld.status_code == 200 and ts.WITHHELD_TEXT in withheld.text
    for detail in ("Torn ACL", "Peanuts", "Amlodipine", "Activity paused"):
        assert detail not in withheld.text, detail
    owner_view = await page(w["owner"], url)  # the gym's owner still sees everything
    assert "Torn ACL" in owner_view.text and ts.WITHHELD_TEXT not in owner_view.text
    own = await page(w["member"], url)  # and so does the member
    assert "Torn ACL" in own.text


async def test_live_discomfort_reaches_the_trainer_only_while_the_member_shares(db):
    from tests.test_live_coach import assign_trainer, make_member, notified, seed_library, start

    await seed_library(db)
    customer, _owner, gym = await make_member(db)
    emp = await assign_trainer(db, gym, customer)
    trainer_id, name, customer_id = emp.user_id, customer.full_name, customer.id

    async def share(value: bool) -> None:
        async with dbmod.get_sessionmaker()() as s:
            (await s.get(CustomerProfile, customer_id)).share_health_with_trainers = value
            await s.commit()

    await share(False)
    async with make_client() as c:
        await login_as(c, customer)
        s = await start(c)
        r = await api(c, "POST", f"/api/live/sessions/{s['id']}/discomfort", json={"body_part": "knee", "severity": "mild"})
        assert r.status_code == 200, r.text
    assert [n.title for n in await notified(db, trainer_id)] == []  # private: no health detail to the trainer
    await share(True)
    customer = await db.get(User, customer_id)  # (notified() expired the session's objects)
    async with make_client() as c:
        await login_as(c, customer)
        s = await start(c)
        r = await api(c, "POST", f"/api/live/sessions/{s['id']}/discomfort", json={"body_part": "knee", "severity": "mild"})
        assert r.status_code == 200, r.text
    assert [n.title for n in await notified(db, trainer_id)] == [f"Client discomfort: {name}"]


async def test_the_assistant_never_lets_a_chat_limit_block_a_red_flag(db, fake_ai, low_caps):
    from tests.test_assistant import _member_with_staff, _reply

    customer, gym, trainer = await _member_with_staff(db)
    other = await make_employee(db, gym)
    team = await chat_service.get_or_create_thread(db, gym_id=gym.id, customer_id=customer.id, employee_id=None)
    await db.commit()
    assert team is not None
    low_caps[chat_service.THREADS_CAP] = 1  # one new conversation a day - already used
    fake_ai(responses=[_reply(
        "Sending.",
        {"type": "message_staff", "gym_id": gym.id, "employee_id": other.id, "body": "Can we move Friday's session?"},
        {"type": "message_staff", "gym_id": gym.id, "employee_id": trainer.id,
         "body": "I have chest pain and feel faint after the run"},
    )])
    async with make_client() as c:
        await login_as(c, customer)
        data = (await api(c, "POST", "/api/assistant/chat", json={"message": "tell my trainers"})).json()
    first, second = data["actions"]
    assert first["ok"] is False and "new conversations in the last 24 hours" in first["detail"]  # the cap's own words
    assert second["ok"] is True and "112" in second["detail"]  # the red flag got through, guidance first
    assert await db.scalar(select(func.count(ChatThread.id)).where(ChatThread.employee_id == trainer.id)) == 1


# ====================================================================== caps
async def test_wellness_check_ins_are_capped_per_day_but_a_red_flag_is_never_refused(db, monkeypatch):
    from app.services import health as hs

    member = await make_customer(db)
    real = caps.limit
    monkeypatch.setattr(caps, "limit", lambda name: 2 if name == hs.WELLNESS_CAP else real(name))
    calm = {"q1": "0", "q2": "4", "q3": "4", "q4": "0", "symptoms": "none"}
    async with make_client() as c:
        await login_as(c, member)
        for _ in range(2):
            assert (await post_form(c, "/me/wellness", calm)).status_code == 303
        refused = await post_form(c, "/me/wellness", calm)
        assert refused.status_code == 303 and refused.headers["location"] == "/me/wellness"
        assert await db.scalar(select(func.count(WellnessCheck.id)).where(WellnessCheck.customer_id == member.id)) == 2
        red = await post_form(c, "/me/wellness", {**calm, "symptoms": "chest_pain"})
        assert red.status_code == 303 and red.headers["location"].startswith("/me/wellness/")
    assert await db.scalar(select(func.count(WellnessCheck.id)).where(WellnessCheck.customer_id == member.id)) == 3
    assert hs.WELLNESS_CAP in caps.CAPS and caps.CAPS[hs.WELLNESS_CAP].default == 10


async def test_the_member_dashboard_shows_the_gym_slots(db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    plans = {p.code: p for p in await make_plans(db, gym)}
    member = await make_customer(db)
    await make_subscription(db, member, plans["combo"])
    html = (await page(member, "/me")).text
    assert "data-gym-slots" in html and "Online gyms 1/3" in html and "In-person gym 1/1" in html


async def test_staff_documents_are_charged_to_the_gyms_storage_quota(db, monkeypatch):
    from tests.factories import png_bytes

    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    emp = await make_employee(db, gym)
    real = caps.limit
    monkeypatch.setattr(caps, "limit", lambda name: 0 if name == "storage_mb_per_gym" else real(name))
    files = {"document": ("cert.png", png_bytes((200, 200)), "image/png")}
    async with make_client() as c:
        await login_as(c, owner)
        r = await post_form(c, f"/owner/staff/{emp.id}/documents", {"doc_type": "certification"}, files=files)
        assert r.status_code == 400 and "storage quota" in r.text
    async with make_client() as c:  # the staff member's own upload too
        await login_as(c, await db.get(User, emp.user_id))
        r = await post_form(c, f"/staff/employments/{emp.id}/documents", {"doc_type": "certification"}, files=files)
        assert r.status_code == 303
    assert await db.scalar(select(func.count(EmployeeDocument.id)).where(EmployeeDocument.employee_id == emp.id)) == 0


# ====================================================================== sessions and sign-in
async def test_reauthentication_in_flight_while_the_account_locks_is_refused(db, monkeypatch):
    from app.routers import account as account_router

    user = await make_customer(db)
    uid = user.id

    async def verify(hashed: Any, password: str) -> bool:
        # the right password, verified only after wrong guesses in flight locked the account
        async with dbmod.get_sessionmaker()() as other:
            row = await other.get(User, uid)
            row.locked_until = utcnow() + timedelta(minutes=15)
            await other.commit()
        return True

    monkeypatch.setattr(account_router, "verify_password", verify)
    request = SimpleNamespace(url=SimpleNamespace(path="/account/password"))
    message = await account_router._check_password(db, request, user, DEFAULT_PASSWORD)
    assert message is not None and message.startswith("Too many wrong passwords")


async def _ended_session(db, user: User) -> str:
    """A session of ``user`` replaced by a sign-in on another device (revoked_at = the newer one's created_at)."""
    from app.security.crypto import new_token, token_hash

    old, new = new_token(32), new_token(32)
    at = utcnow()
    async with dbmod.get_sessionmaker()() as s:
        s.add(UserSession(id=token_hash(old), user_id=user.id, expires_at=at + timedelta(hours=2), ip="127.0.0.1",
                          user_agent="pytest", created_at=at - timedelta(hours=1), revoked_at=at))
        s.add(UserSession(id=token_hash(new), user_id=user.id, expires_at=at + timedelta(hours=2), ip="127.0.0.1",
                          user_agent="Mozilla/5.0 (Windows NT 10.0) Chrome/130.0", created_at=at))
        await s.commit()
    return old


@pytest.fixture
def ws_no_redis(monkeypatch):
    from app.services.presence import presence

    async def none() -> None:
        return None

    monkeypatch.setattr(presence, "_redis", none)


async def test_a_socket_of_a_device_signed_out_elsewhere_closes_with_4409(db, ws_no_redis):
    from tests.test_presence import FakeWS, open_ws

    user = await make_customer(db)
    ws = FakeWS(await _ended_session(db, user))
    task = await open_ws(ws)
    await asyncio.wait_for(task, 3)
    assert ws.closed == 4409 and ws.sent == []


async def test_a_socket_whose_session_ends_while_it_subscribes_is_closed(db, ws_no_redis, monkeypatch):
    """The revocation committed between the socket's session lookup and ``hub.subscribe`` (its close already ran):
    the second look after subscribing closes it."""
    from app.security import sessions as sess_mod
    from app.services.realtime import hub
    from tests.test_presence import FakeWS, new_session, open_ws

    user = await make_customer(db)
    token = await new_session(user)
    real = sess_mod.lookup_session
    calls = 0

    async def lookup(db_: Any, tok: str | None):
        nonlocal calls
        calls += 1
        return await real(db_, tok) if calls == 1 else sess_mod.SessionLookup()  # revoked in between

    monkeypatch.setattr(sess_mod, "lookup_session", lookup)
    ws = FakeWS(token)
    task = await open_ws(ws)
    await asyncio.wait_for(task, 3)
    assert ws.closed == 4401 and calls == 2
    assert not hub.is_online(user.id)


def test_session_cache_keys_are_namespaced_like_the_shared_limits():
    from app.security import sessions as sess_mod
    from app.security.ratelimit import namespace

    s = get_settings()
    db_name = s.database_url.rsplit("/", 1)[-1].split("?", 1)[0]
    assert sess_mod._key("abc") == namespace().removesuffix("sec:") + "sess:abc"
    assert f":{s.environment}:{db_name}:" in sess_mod._key("abc") and sess_mod._ukey(7).endswith(":usess:7")


async def test_a_session_lookup_ends_its_read_transaction_before_talking_to_redis(db, monkeypatch):
    from app.security import sessions as sess_mod
    from tests.test_presence import new_session

    user = await make_customer(db)
    token = await new_session(user)
    seen: list[bool] = []

    async def r_get(h: str) -> None:
        return None  # a cold cache: the database path, which warms Redis afterwards

    async def r_set(h: str, uid: int, ttl: int) -> None:
        seen.append(db.in_transaction())

    monkeypatch.setattr(sess_mod, "_r_get", r_get)
    monkeypatch.setattr(sess_mod, "_r_set", r_set)
    found = await sess_mod.lookup_session(db, token)
    assert found.session is not None and found.session.user.id == user.id
    assert seen == [False]  # never "idle in transaction" while Redis answers (or stalls)


async def test_the_unknown_account_lockout_continues_from_the_shared_count_in_a_redis_outage():
    from app.services.auth_flows import GhostThrottle

    class Store:
        def __init__(self) -> None:
            self.up, self.count = True, 0

        async def run(self, script: Any, keys: Any, args: Any, convert: Any) -> Any:
            if not self.up:
                return None  # Redis down: this worker's memory decides
            self.count += 1
            return convert(["0", str(self.count)])

    store = Store()
    throttle = GhostThrottle(store=store)  # type: ignore[arg-type]
    limit = get_settings().login_max_failures
    for _ in range(limit - 1):
        assert await throttle.fail("ghost@example.com") is None
    store.up = False
    assert await throttle.fail("ghost@example.com") is not None  # the limit-th failure locks (not a fresh count of 1)


def test_cors_lets_listed_origins_send_the_submit_id():
    from app.main import app

    cors = [m for m in app.user_middleware if getattr(m.cls, "__name__", "") == "CORSMiddleware"]
    assert cors and "X-Submit-Id" in cors[0].kwargs["allow_headers"]


def test_the_ai_repeat_key_ignores_the_one_time_submit_id():
    for path in (ROOT / "app/static/src/js/01-ai.js", ROOT / "app/static/dist/app.js"):
        source = path.read_text(encoding="utf-8")
        assert 'k !== "csrf_token" && k !== "page_id" && k !== "submit_id"' in source, path


def test_env_example_documents_this_rounds_settings_without_pinning_them():
    from dotenv import dotenv_values

    from app.config import Settings

    path = ROOT / ".env.example"
    pinned, text_ = dotenv_values(path), path.read_text(encoding="utf-8")
    for key in ("SINGLE_SESSION_PER_ACCOUNT", "VIOLATION_AUTOBAN_THRESHOLD", "VIOLATION_WINDOW_DAYS",
                "SEVERE_IP_BAN_DAYS", "SHARED_IP_THRESHOLD", "RATE_LIMIT_REDIS_TIMEOUT_MS",
                "AI_DOCKER_FIRST_TOKEN_SECONDS"):
        default = Settings.model_fields[key.lower()].default
        shown = str(default).lower() if isinstance(default, bool) else f"{default:g}" if isinstance(default, float) \
            else str(default)
        assert key not in pinned and f"# {key}={shown}" in text_, key


# ====================================================================== platform
async def test_the_mailer_holds_no_lock_while_a_mail_server_answers(db, monkeypatch):
    from app.services import mailer

    for to in ("a@example.com", "b@example.com"):
        db.add(EmailOutbox(to_email=to, subject="Hi", text_body="Hello", status=EmailStatus.QUEUED))
    await db.commit()
    free: list[int] = []

    async def deliver(msg: Any) -> None:
        async with dbmod.get_sessionmaker()() as other:  # NOWAIT: fails at once if the batch's rows were still locked
            rows = (await other.execute(text("SELECT id FROM email_outbox FOR UPDATE NOWAIT"))).all()
            free.append(len(rows))
            await other.rollback()
        if msg["To"] == "b@example.com":
            raise ConnectionError("mail server down")

    monkeypatch.setattr(mailer, "_deliver", deliver)
    monkeypatch.setattr(get_settings(), "smtp_host", "smtp.test")
    async with dbmod.session_scope() as s:
        assert await mailer.send_pending_emails(s) == 2
    assert free == [2, 2]
    db.expire_all()
    rows = {r.to_email: r for r in (await db.execute(select(EmailOutbox))).scalars()}
    assert rows["a@example.com"].status == EmailStatus.SENT and rows["a@example.com"].attempts == 1
    b = rows["b@example.com"]
    assert b.status == EmailStatus.QUEUED and b.attempts == 1 and "mail server down" in b.last_error
    assert b.next_attempt_at is not None and b.next_attempt_at > utcnow()


async def test_the_mailer_reclaims_a_batch_whose_worker_died(db, monkeypatch):
    from app.services import mailer

    now = utcnow()
    db.add(EmailOutbox(to_email="stale@example.com", subject="Hi", text_body="x", status=EmailStatus.SENDING, attempts=1,
                       next_attempt_at=now - timedelta(minutes=1)))  # its lease ran out
    db.add(EmailOutbox(to_email="busy@example.com", subject="Hi", text_body="x", status=EmailStatus.SENDING, attempts=1,
                       next_attempt_at=now + timedelta(minutes=10)))  # another worker is on it
    await db.commit()
    monkeypatch.setattr(get_settings(), "smtp_host", "")  # spooled to var/mail
    async with dbmod.session_scope() as s:
        assert await mailer.send_pending_emails(s) == 1
    db.expire_all()
    rows = {r.to_email: r for r in (await db.execute(select(EmailOutbox))).scalars()}
    assert rows["stale@example.com"].status == EmailStatus.LOGGED and rows["stale@example.com"].attempts == 2
    assert rows["busy@example.com"].status == EmailStatus.SENDING


async def test_a_periodic_task_that_commits_keeps_its_lock_for_the_whole_run():
    from app.services.jobs import PeriodicTask, _lock_id, run_periodic

    key = _lock_id("check_lock_probe")
    others: list[bool] = []

    async def fn(db: Any) -> None:
        await db.execute(text("SELECT 1"))
        await db.commit()  # a task that commits mid-run (payroll per gym, retention batches)
        async with dbmod.get_engine().connect() as conn:
            got = await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
            others.append(bool(got))
            if got:
                await conn.scalar(text("SELECT pg_advisory_unlock(:k)"), {"k": key})

    assert await run_periodic(PeriodicTask(name="check_lock_probe", every_seconds=60, fn=fn)) is True
    assert others == [False]  # another process could not start the same task while it ran
    async with dbmod.get_engine().connect() as conn:  # ... and the lock is released afterwards
        assert await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}) is True
        await conn.scalar(text("SELECT pg_advisory_unlock(:k)"), {"k": key})


async def test_the_ip_page_lists_the_ipv6_64_ban_of_the_address(db):
    admin = await make_user(db, role=Role.SUPERADMIN)
    db.add(IpBan(ip="2001:db8:1:2::/64", reason="Scanner (per /64)", severity=3, created_by="auto",
                 banned_until=utcnow() + timedelta(hours=2)))
    await db.commit()
    r = await page(admin, "/superadmin/security/ip/2001:db8:1:2::5")
    assert r.status_code == 200 and "2001:db8:1:2::/64" in r.text and "Scanner (per /64)" in r.text
    assert "/lift" in r.text  # the active /64 ban can be lifted from here


async def test_user_sessions_have_the_address_index_the_trust_checks_use(db):
    assert "ix_user_sessions_ip_seen" in {i.name for i in UserSession.__table__.indexes}
    found = await db.scalar(text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_user_sessions_ip_seen'"))
    assert found is not None and "(ip, last_seen_at)" in found and "ip IS NOT NULL" in found


async def test_the_public_terms_and_privacy_pages_state_the_registry_and_conduct_rules():
    terms = (await page(None, "/terms")).text
    assert "One person, one account - forever." in terms and "Staff conduct, removal and bans." in terms
    assert "keyed hash" in terms and "two different gym businesses within 12 months" in terms
    privacy = (await page(None, "/privacy")).text
    assert "One person, one account - forever:" in privacy and "until the erase completes" not in privacy


# ====================================================================== AI fallback
def test_the_docker_model_gets_a_longer_first_token_allowance_within_the_answers_budget():
    from app.ai.client import build_provider

    s = get_settings().model_copy(update={"ai_docker_first_token_seconds": 25.0})
    docker, gateway = build_provider(s, "docker"), build_provider(s, "gateway")
    assert docker.first_token_allowance(10.0, 30.0) == 25.0  # plan questions: 10 s asked, 25 s for the CPU model
    assert docker.first_token_allowance(8.0, 20.0) == 15.0  # never past the whole-answer budget minus the margin
    assert docker.first_token_allowance(40.0, 60.0) == 40.0  # a feature asking for more keeps it
    assert gateway.first_token_allowance(10.0, 30.0) == 10.0  # the GPU gateway: the feature's own deadline


async def test_a_slow_local_model_answers_a_stream_instead_of_being_abandoned(monkeypatch):
    from app.ai.client import FakeProvider, ai

    slow = FakeProvider("docker", chunks=["Slow ", "but ", "sure."], delay=0.6)
    slow.min_first_token_seconds = 5.0
    ai.use_providers([slow])
    try:
        meta: dict[str, Any] = {}
        words = [w async for w in ai.stream(feature="plans", system="s", prompt="q", first_token_timeout=0.2,
                                            total_timeout=10.0, meta=meta)]
    finally:
        ai.use_providers(None)
    assert "".join(words) == "Slow but sure." and meta["provider"] == "docker" and not meta.get("truncated")


def test_tts_status_names_the_engine_that_is_actually_speaking():
    from app.services.tts import tts

    try:
        tts.use_client(SimpleNamespace(engine="simulated", audio=SimpleNamespace(speech=SimpleNamespace(create=None))))
        assert tts.status()["engine"] == "simulated"
        tts.use_client(SimpleNamespace(audio=None))  # a test fake without an engine name: still the Kokoro lane
        assert tts.status()["engine"] == "kokoro"
    finally:
        tts.use_client(None)
    assert tts.status()["engine"] == "kokoro"


def test_the_self_check_lists_the_simulated_ai(monkeypatch):
    from app.services import selfcheck

    off = selfcheck.check_simulated_ai()
    assert off.check == "Simulated AI (load tests)" and off.status == "ok" and off.group == "AI & OCR"
    on = get_settings().model_copy(update={"ai_provider_order": ["simulated"]})
    monkeypatch.setattr(selfcheck, "get_settings", lambda: on)
    assert selfcheck.check_simulated_ai().status == "warn" and "ON" in selfcheck.check_simulated_ai().detail
    prod = get_settings().model_copy(update={"environment": "production", "ai_simulated_allowed": True,
                                             "ai_provider_order": ["gateway", "simulated"]})
    monkeypatch.setattr(selfcheck, "get_settings", lambda: prod)
    assert selfcheck.check_simulated_ai().status == "fail"
    assert "ai_simulated" in selfcheck.ORDER


async def test_the_self_check_never_hashes_inside_a_transaction(db, monkeypatch):
    from app.security import passwords
    from app.services import selfcheck

    await make_user(db, role=Role.SUPERADMIN)
    seen: list[bool] = []
    real = passwords.verify_password

    async def verify(hashed: Any, password: str) -> bool:
        seen.append(db.in_transaction())
        return await real(hashed, password)

    monkeypatch.setattr(passwords, "verify_password", verify)
    result = await selfcheck.check_superadmin_password(db)
    assert result.status == "ok" and seen and not any(seen)


# ====================================================================== integrator's open item: OCR refusal in a race
async def test_an_ocr_vision_step_refused_in_a_race_says_why(client, db, monkeypatch, fake_ai):
    """Admitted at the click, then refused by the AI guard in a race (another tab used the last AI photo check): the
    member is told why (the guard's own words) instead of a silent "engine failed" - and still no second AI call."""
    import html

    from app.ai import health as health_ai
    from app.ai.errors import AIRejected
    from app.services import ocr
    from tests.test_member_caps import _real_ocr, _scan

    monkeypatch.setattr(get_settings(), "ai_guard_enabled", True)
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
    text = html.unescape(r.text)
    assert r.status_code == 200 and 'value="72.4"' in text  # the local engine's reading still fills in
    assert "You've used your AI photo checks for this minute." in text and 'data-scan-ai-refusal="quota"' in text
    assert vision_model == []


def test_chat_messages_use_only_the_global_rate_limit_bucket():
    """Trainer safety: a member's red-flag chat message must never be refused by a chat-specific per-address bucket."""
    from app.security.ratelimit import bucket_for

    assert bucket_for("POST", "/api/chat/5/messages", "application/json") is None
    assert bucket_for("POST", "/chat/5/messages", "application/x-www-form-urlencoded") is None


async def test_an_email_or_phone_change_to_a_registry_identifier_is_refused(db):
    """Trust & safety (mandatory): e-mail and phone changes consult the blocked-identity registry - a deleted or banned
    account's identifiers never come back on another account."""
    import html

    from app.services import trust_safety as TS

    gone = await make_customer(db)
    await TS.register_entries(db, [("email", TS.email_hash("gone.member@example.com")),
                                   ("phone", TS.phone_hash("+91 90000 44444"))], reason="banned", user_id=gone.id,
                              actor=None)
    await db.commit()
    member = await make_customer(db, email="still.here@example.com", phone="+91 90000 55555", country_code="IN",
                                 city="Bengaluru")
    base = {"full_name": member.full_name, "email": "still.here@example.com", "username": "",
            "phone": "90000 55555", "phone_country": "IN", "date_of_birth": "1995-05-17", "country": "IN",
            "city": "Bengaluru", "timezone": "Asia/Kolkata"}
    async with make_client() as c:
        await login_as(c, member)
        r = await post_form(c, "/account", {**base, "email": "Gone.Member+x@example.com"})
        assert r.status_code == 400 and TS.BLOCKED_MESSAGES["email"] in html.unescape(r.text)
        r = await post_form(c, "/account", {**base, "phone": "90000 44444"})
        assert r.status_code == 400 and TS.BLOCKED_MESSAGES["phone"] in html.unescape(r.text)
    member_id = member.id
    db.expire_all()
    me = await db.get(User, member_id)
    assert me.email == "still.here@example.com" and me.phone_normalized == "+919000055555"


async def test_the_bans_list_finds_the_ipv6_64_ban_of_an_address(db):
    admin = await make_user(db, role=Role.SUPERADMIN)
    db.add(IpBan(ip="2001:db8:7:8::/64", reason="Flood from this /64", severity=3, created_by="auto",
                 banned_until=utcnow() + timedelta(hours=2)))
    await db.commit()
    r = await page(admin, "/superadmin/bans?state=active&ip=2001:db8:7:8::99")
    assert r.status_code == 200 and "Flood from this /64" in r.text


def test_the_startup_log_names_https_in_lan_mode():
    import inspect

    from app import main

    source = inspect.getsource(main.lifespan)
    assert 'os.environ.get("IRONPULSE_LAN") == "1"' in source and "ready on %s://" in source


# ====================================================================== N+1 guard (this round's failing cases)
async def test_usernames_are_backfilled_uniquely_in_a_constant_number_of_statements(db):
    from app.db import QueryCounter
    from app.services.identity_lookup import backfill_usernames

    await make_customer(db, full_name="Riya Kapoor", username="riya.kapoor")  # the plain handle is taken
    await make_customer(db, full_name="Ann Lee", username="ann_lee")  # '_' must not act as a LIKE wildcard
    for name in ("Riya Kapoor", "Riya Kapoor", "Arjun Rao", "Ann Lee"):
        await make_customer(db, full_name=name)
    with QueryCounter() as qc:
        assert await backfill_usernames(db) == 4
    await db.commit()
    assert qc.count <= 3  # the batch, one LIKE ANY of the taken handles, one bulk UPDATE
    names = list((await db.execute(select(User.username).where(User.username.is_not(None)))).scalars())
    assert len(names) == len(set(names)) == 6 and "arjun.rao" in names and "ann.lee" in names
    assert sum(1 for n in names if n.startswith("riya.kapoor")) == 3
    assert await backfill_usernames(db) == 0  # nothing left to do
