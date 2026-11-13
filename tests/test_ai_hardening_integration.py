"""AI hardening round 2 - integration of the fixers' cross-owner requests (docs/ai-guard-and-queue.md section 12).

Each test fails without the integration change it names:

* the friendly AI note of a normal reading (``health_note``) counts in the members' 'notes' quota, never in the coach
  plans' 'gen' (app/ai/policy.py);
* account deletion stops the person's AI requests in every process: the deletion hook goes through
  ``hub.cancel_ai``, which also tells the other workers over the PG bridge (app/services/accounts.py);
* ``manage.py run --workers N`` without REDIS_URL says that leaving is then only noticed at sign-out, and
  ``check_env`` says Redis is needed with several workers;
* the super-admin AI page shows the new presence counters and the voice cache (``tts.status(admin=True)``);
* a coach voice line is charged once Kokoro works on it, like ``ai.complete`` (app/services/tts.py): the new
  disconnect watcher must not turn aborted lines into free Kokoro time.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.ai import policy as ai_policy
from app.ai.guard import guard
from app.config import get_settings
from app.models.enums import Role
from app.services import health as hs
from app.services.realtime import hub
from tests.ask_ai_helpers import steady_minute  # noqa: F401 - fixture
from tests.conftest import login_as, post_form
from tests.factories import make_customer, make_gym, make_owner, make_plans, make_subscription, make_user

ROOT = Path(__file__).resolve().parent.parent


def _script(name: str) -> Any:
    """scripts/<name>.py, loaded under the key tests/test_scripts.py uses (one module object per script)."""
    key = f"ironpulse_scripts_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


async def _wait_until(predicate: Any, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


# ============================================================================================ health note quota group
async def test_the_health_note_is_charged_to_the_notes_quota_not_the_coach_plans(client, db, monkeypatch):
    """Before: 'health_note' had no policy row, so policy_for() fell back to DEFAULT_POLICY - every reading a member
    with AI logged used up their coach-plan quota ('gen': 3 a minute, 30 a day) instead of their notes."""
    monkeypatch.setattr(get_settings(), "ai_guard_enabled", True)
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 1.0)
    policy = ai_policy.policy_for(hs.NOTE_FEATURE)
    assert policy.name == hs.NOTE_FEATURE and policy.group == "notes" and not policy.safety
    assert (policy.priority, policy.on_exit, policy.dedupe_seconds) == (ai_policy.BACKGROUND, ai_policy.EXIT_PAUSE, 0.0)

    owner = await make_owner(db)
    plans = {p.code: p for p in await make_plans(db, await make_gym(db, owner))}
    member = await make_customer(db)
    await make_subscription(db, member, plans["online_ai"])
    await login_as(client, member)
    r = await post_form(client, "/me/health/metrics", {"height_cm": "175", "weight_kg": "71.5"})
    assert r.status_code == 303, r.text[:300]
    usage = (await guard.status(member.id))["usage"]
    assert usage["notes"][2] == 1  # the click was judged as a note ...
    assert usage["gen"] == [0, 0, 0]  # ... and never touched the coach plans' quota


# ============================================================================================ account deletion
async def test_account_deletion_stops_the_ai_work_in_every_process(db, monkeypatch):
    """Before: the deletion hook called ``scheduler.cancel`` - this process only. With several workers, the deleted
    account's AI requests running in another process (keep-policy ones included) went on to the end."""
    from app.services.accounts import soft_delete_user

    sent: list[dict[str, Any]] = []

    class FakeBridge:
        async def execute(self, sql: str, channel: str, payload: str) -> None:
            sent.append(json.loads(payload))

    monkeypatch.setattr(hub, "_bridge_conn", FakeBridge())

    def cancels() -> list[dict[str, Any]]:
        return [m["ctl"] for m in sent if isinstance(m.get("ctl"), dict) and m["ctl"].get("op") == "cancel_ai"]

    user = await make_customer(db)
    await soft_delete_user(db, user, reason="integration test")
    await asyncio.sleep(0.05)
    assert cancels() == []  # nothing before the COMMIT
    await db.commit()
    await _wait_until(lambda: bool(cancels()))
    assert cancels() == [{"op": "cancel_ai", "user_id": user.id, "reason": "account_deleted",
                          "policies": ["cancel", "keep", "pause"]}]


async def test_a_rolled_back_deletion_tells_no_process_anything(db, monkeypatch):
    from app.services.accounts import soft_delete_user

    sent: list[dict[str, Any]] = []

    class FakeBridge:
        async def execute(self, sql: str, channel: str, payload: str) -> None:
            sent.append(json.loads(payload))

    monkeypatch.setattr(hub, "_bridge_conn", FakeBridge())
    user = await make_customer(db)
    await soft_delete_user(db, user, reason="integration test")
    await db.rollback()
    await asyncio.sleep(0.1)
    assert not [m for m in sent if (m.get("ctl") or {}).get("op") == "cancel_ai"]


# ============================================================================================ several workers: say so
def test_manage_run_warns_when_several_workers_have_no_redis(monkeypatch, capsys):
    """Before: ``manage.py run --workers 3`` without REDIS_URL started silently, although leaving is then only
    noticed at sign-out (each worker logs it too - presence.startup_check)."""
    manage = _script("manage")
    calls: list[list[str]] = []
    monkeypatch.setattr(manage, "run_python", lambda args, env=None: calls.append(list(args)) or 0)
    settings = SimpleNamespace(host="127.0.0.1", port=8000, trust_proxy_headers=False, redis_url=None)
    monkeypatch.setattr(manage, "load_settings", lambda: settings)
    assert manage.main(["run", "--workers", "3"]) == 0
    out = capsys.readouterr().out
    assert "warning: several workers without REDIS_URL" in out and "sign out" in out
    assert calls and calls[-1][:3] == ["-m", "uvicorn", "app.main:app"]  # it still starts

    settings.redis_url = "redis://127.0.0.1:6379/0"
    assert manage.main(["run", "--workers", "3"]) == 0
    assert "REDIS_URL" not in capsys.readouterr().out
    assert manage.main(["run"]) == 0  # one process: nothing to share, nothing to say
    assert "warning" not in capsys.readouterr().out


def test_check_env_says_redis_is_needed_with_several_workers(tmp_path, monkeypatch):
    check_env = _script("check_env")
    monkeypatch.delenv("REDIS_URL", raising=False)
    env = tmp_path / "check.env"
    env.write_text("PORT=8000\n", encoding="utf-8")
    result = check_env.check_redis(check_env.Ctx(env_path=env, load_app_settings=False))
    assert result.status == check_env.INFO
    assert "fine for one worker" in result.detail and "--workers N" in result.detail


# ============================================================================================ admin panel
async def test_the_ai_admin_page_shows_the_new_presence_counters_and_the_voice_cache(client, db, monkeypatch):
    from app.services import selfcheck
    from app.services.presence import presence

    service = (await selfcheck.probe_tts())["service"]
    assert service is not None and "cached" in service and "hits" in service  # the admin view of tts.status()

    for name, value in (("parks_overtaken", 3), ("notes_kept", 5), ("notes_delivered", 4), ("kept_unseen", 2)):
        monkeypatch.setitem(presence.stats, name, value)
    admin = await make_user(db, role=Role.SUPERADMIN, full_name="Platform Admin")
    await login_as(client, admin)
    r = await client.get("/superadmin/ai")
    assert r.status_code == 200
    import html

    page = html.unescape(r.text)
    assert "Lines cached (per member)" in page
    assert "Paused jobs resumed as their requester came back" in page
    assert "'Resuming now' notes kept · delivered" in page and "5 · 4" in page
    assert "Exits not judged (workers without shared Redis)" in page


@pytest.mark.parametrize("feature", [hs.NOTE_FEATURE])
def test_every_guarded_feature_used_by_the_ai_team_has_its_own_policy_row(feature):
    """A guarded feature without a row silently borrows DEFAULT_POLICY ('gen' quota, interactive priority, cancel on
    exit) - the health note did until this round."""
    assert feature in ai_policy.FEATURES


# ============================================================================================ coach voice: drops cost
@pytest.fixture
def voice(monkeypatch):
    """A fake Kokoro (tests/test_tts.py FakeSpeech) behind the real TTS service, with the AI guard on."""
    from app.services.tts import tts
    from tests.test_tts import FakeSpeech

    monkeypatch.setattr(get_settings(), "ai_guard_enabled", True)
    installed: list[Any] = []

    def install(**kwargs: Any) -> Any:
        fake = FakeSpeech(**kwargs)
        tts.use_client(fake)
        installed.append(fake)
        return fake

    yield install
    for fake in installed:
        fake.release()
    tts.use_client(None)


async def test_dropping_voice_lines_mid_synthesis_is_no_way_around_the_voice_quota(voice, monkeypatch,
                                                                                    steady_minute):
    """The disconnect watcher of /api/tts (this round) stops Kokoro when the coach page aborts a line - and the line was
    refunded: a member could keep Kokoro busy for free by aborting every line once it was being synthesised (defect
    #0's hole, in the voice lane). Now a line is charged once Kokoro works on it (as ``ai.complete`` charges a call
    whose provider slot was held); a line dropped while it only waited for a voice-lane slot stays free."""
    from app.ai.errors import AIRejected
    from app.ai.owner import AIOwner, owner_scope
    from app.ai.scheduler import scheduler
    from app.services import tts as tts_mod
    from app.services.tts import tts

    monkeypatch.setattr(get_settings(), "ai_quota_scale", 0.05)  # a member's voice quota: 3 lines a minute
    limit = guard._limits("member", "voice")[0]
    assert limit == 3
    fake = voice()
    member = AIOwner(user_id=987, role="customer", session_hash="voice-session", page_id="voice-page-0001-xyz")

    async def line(text: str) -> Any:
        with owner_scope(member):
            return await tts.synthesize_or_refuse(text)

    async def settled() -> None:
        await _wait_until(lambda: not tts_mod._background and not tts._inflight)

    for n in range(limit):  # every line dropped while Kokoro speaks it
        text = f"Line number {n} - hold the plank."
        fake.hold(text)
        task = asyncio.create_task(line(text))
        await _wait_until(lambda text=text: fake.started(text) == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _wait_until(lambda text=text: text in fake.cancelled)  # Kokoro was stopped at once ...
        await settled()
    assert (await guard.status(987))["usage"]["voice"][0] == limit  # ... and each line was charged
    with pytest.raises(AIRejected) as refused:  # so the quota holds
        await line("One more line, please.")
    assert refused.value.code == "quota" and fake.started("One more line, please.") == 0
    assert (await guard.status(987))["strikes"] <= 1  # the quota refusal's own point - the drops earned none

    # a line dropped while it only waited for a voice-lane slot never reached Kokoro: free
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 1.0)
    monkeypatch.setattr(get_settings(), "ai_tts_concurrency", 1)
    other = AIOwner(user_id=988, role="customer", session_hash="other-session", page_id="voice-page-0002-xyz")
    fake.hold("Busy phrase")
    with owner_scope(other):
        busy = asyncio.create_task(tts.synthesize("Busy phrase"))
    await _wait_until(lambda: fake.started("Busy phrase") == 1)
    before = (await guard.status(987))["usage"]["voice"][0]
    queued = asyncio.create_task(line("Waiting for a slot."))
    await _wait_until(lambda: scheduler.snapshot()["lanes"]["tts"]["queued"] == 1)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    fake.release("Busy phrase")
    await busy
    await settled()
    assert fake.started("Waiting for a slot.") == 0
    assert (await guard.status(987))["usage"]["voice"][0] == before  # refunded
