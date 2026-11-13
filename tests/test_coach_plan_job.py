"""Coach plans are built in the background (docs/ai-guard-and-queue.md section 6): POST /me/coach/generate never waits
for the AI. It judges the click with the AI guard (a refusal is flashed with its reason and wait), queues ONE
``coach_plan`` job per member (a double click cannot queue two) and redirects to ``/me/coach?generating=<id>``, whose
progress card follows the job over ``task.update`` and the owner-scoped JSON poll ``GET /me/coach/status``. The job
builds the plan exactly as before (AI when the membership includes it, the safety-checked template otherwise), saves
it, notifies "Your new training plan is ready" and is paused while the member is away - resumed when they are back.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from app import db as dbmod
from app.ai.errors import AIRejected
from app.ai.guard import guard
from app.ai.owner import AIOwner, owner_scope
from app.config import get_settings
from app.models import CustomerProfile, Job, Notification, WorkoutPlan
from app.models.enums import JobStatus
from app.services import coach as coach_service
from app.services import jobs as J
from app.services.presence import presence
from app.services.realtime import hub
from app.services.subscriptions import plan_access
from tests.ask_ai_helpers import steady_minute  # noqa: F401 - fixture
from tests.conftest import api, login_as, make_client, post_form, run_jobs
from tests.factories import make_customer, make_gym, make_owner, make_plans, make_subscription

QUEUED, RUNNING, DONE, PAUSED, FAILED = (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.DONE, JobStatus.PAUSED,
                                         JobStatus.FAILED)
PLAN = json.dumps({
    "title": "Progressive strength week",
    "days": [{"name": f"Day {d + 1}", "focus": "Strength", "items": [
        {"exercise": "Goblet squat", "sets": 3, "reps": 10}, {"exercise": "Lat pulldown", "sets": 3, "reps": 10},
        {"exercise": "Cat-cow", "sets": 2, "duration_s": 40}]} for d in range(3)],
    "weekly_notes": ["Sleep 8 hours."],
})


@pytest.fixture(autouse=True)
def _guard_as_configured(monkeypatch):
    """The real guard with the shipped quotas (whatever the local .env says): an AI member may start one coach plan
    (3 'gen' units) per minute."""
    monkeypatch.setattr(get_settings(), "ai_guard_enabled", True)
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 1.0)


async def _member(db, *, ai: bool = True, **kwargs: Any):
    owner = await make_owner(db)
    plans = {p.code: p for p in await make_plans(db, await make_gym(db, owner))}
    customer = await make_customer(db, **kwargs)
    await make_subscription(db, customer, plans["online_ai" if ai else "online"])
    return customer


async def _job(customer) -> Job | None:
    async with dbmod.get_sessionmaker()() as s:
        return await s.scalar(select(Job).where(Job.dedupe_key == f"coach_plan:{customer.id}"))


async def _plans(customer) -> list[WorkoutPlan]:
    async with dbmod.get_sessionmaker()() as s:
        stmt = select(WorkoutPlan).where(WorkoutPlan.customer_id == customer.id).order_by(WorkoutPlan.id)
        return list((await s.execute(stmt)).scalars().all())


async def _gen_used(user_id: int) -> int:
    return (await guard.status(user_id))["usage"]["gen"][0]  # 'gen' quota units used this minute


def _updates(q: asyncio.Queue, kind: str = "coach_plan") -> list[dict[str, Any]]:
    out = []
    while not q.empty():
        evt = q.get_nowait()
        if isinstance(evt, dict) and (evt.get("type") != "task.update" or evt.get("kind") == kind):
            out.append(evt)
    return out


async def _ticks(n: int = 30) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def _wait_until(predicate: Callable[[], Any], what: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


# ============================================================================================ queueing
async def test_generate_queues_a_job_and_the_job_builds_the_plan(client, db, fake_ai):
    provider = fake_ai(responses=[PLAN])
    customer = await _member(db)
    await login_as(client, customer)
    r = await post_form(client, "/me/coach/generate", {"page_id": "tab-coach-0001"})
    job = await _job(customer)
    assert r.status_code == 303 and r.headers["location"] == f"/me/coach?generating={job.id}"
    # the request queued the work and returned: no AI call, no plan yet - but the click was charged by the guard
    assert provider.prompts == [] and await _plans(customer) == []
    assert (job.kind, job.status, job.user_id, job.exit_policy) == ("coach_plan", QUEUED, customer.id, "pause")
    assert job.payload["ai"] is True and job.payload["user_id"] == customer.id and job.payload["request"]
    assert await _gen_used(customer.id) == 3  # one 'coach' generation

    page = await client.get(r.headers["location"])
    assert page.status_code == 200
    text = html.unescape(page.text)
    assert 'data-component="coach-job"' in text and f'data-job="{job.id}"' in text
    assert f'data-status-url="/me/coach/status?job={job.id}"' in text and "Building your plan" in text
    assert 'title="Your plan is being built"' in text  # no second click while it is being built
    status = await client.get(f"/me/coach/status?job={job.id}", headers={"Accept": "application/json"})
    assert status.status_code == 200 and status.headers["cache-control"] == "no-store"
    assert status.json() == {"job": job.id, "status": "queued", "title": "Building your plan",
                             "message": "Queued - it starts in a moment.", "active": True, "link": None}

    assert await run_jobs() == 1
    (plan,) = await _plans(customer)
    assert plan.source == "ai" and plan.is_active and plan.plan["meta"]["ai_status"] == "ok"
    assert plan.plan["meta"]["request"] == job.payload["request"]
    assert len(provider.prompts) == 1 and "ALLOWED EXERCISES" in provider.prompts[0]["prompt"]
    note = (await db.execute(select(Notification).where(Notification.user_id == customer.id))).scalar_one()
    assert note.title == "Your new training plan is ready" and note.link == "/me/coach"
    done = (await client.get(f"/me/coach/status?job={job.id}", headers={"Accept": "application/json"})).json()
    assert done["status"] == "done" and done["active"] is False and done["link"] == "/me/coach"
    page = await client.get(f"/me/coach?generating={job.id}")
    assert 'data-component="coach-job"' not in page.text and "AI-personalised" in page.text
    assert await _gen_used(customer.id) == 3  # building it cost nothing more: jobs are never charged twice


async def test_members_without_ai_get_the_template_from_the_job_without_a_guard_charge(client, db, fake_ai):
    provider = fake_ai(responses=[PLAN])
    customer = await _member(db, ai=False)
    await login_as(client, customer)
    r = await post_form(client, "/me/coach/generate")
    assert r.status_code == 303 and (await _job(customer)).payload["ai"] is False
    assert await _gen_used(customer.id) == 0
    assert await run_jobs() == 1
    (plan,) = await _plans(customer)
    assert plan.source == "template" and plan.plan["meta"]["ai_status"] == "not_included" and provider.prompts == []


async def test_job_publishes_task_updates_after_the_plan_is_saved(client, db, fake_ai):
    fake_ai(responses=[PLAN])
    customer = await _member(db)
    await login_as(client, customer)
    await post_form(client, "/me/coach/generate")
    job = await _job(customer)
    q = hub.subscribe(customer.id)
    try:
        assert await run_jobs() == 1
        await _ticks()
        events = _updates(q)
    finally:
        hub.unsubscribe(customer.id, q)
    kinds = [(e["type"], e.get("status")) for e in events]
    # running -> the notification (after COMMIT) -> ONE 'done': the runner's, published after the DONE write with the
    # link and message the job set (jobs.set_result_event) - the job no longer sends a 'done' of its own first
    assert kinds == [("task.update", "running"), ("notification", None), ("task.update", "done")]
    running, notification, done = events
    assert running["task_id"] == job.id and running["kind"] == "coach_plan"
    assert notification["title"] == "Your new training plan is ready" and notification["link"] == "/me/coach"
    assert done["task_id"] == job.id and done["link"] == "/me/coach" and done["message"] == coach_service.PLAN_READY
    assert done["notify"] is False  # the notification is the island note - the browser shows no second one


# ============================================================================================ refusals
async def test_a_guard_refusal_is_flashed_with_the_reason_and_wait_and_queues_nothing(client, db, fake_ai,
                                                                                     steady_minute):
    # steady_minute: the guard's clock mid-minute - a refusal in the first second of a minute waits 59.x s ('1 min')
    provider = fake_ai(responses=[PLAN])
    customer = await _member(db)
    await login_as(client, customer)
    first = await post_form(client, "/me/coach/generate")
    assert first.status_code == 303 and await run_jobs() == 1
    job = await _job(customer)
    refused = await post_form(client, "/me/coach/generate")  # a second AI plan within the same minute
    assert refused.status_code == 303 and refused.headers["location"] == "/me/coach"
    page = html.unescape((await client.get("/me/coach")).text)
    assert "We couldn't start a new plan. You've used your AI generations for this minute." in page
    assert "You can generate more in" in page and "seconds" in page
    after = await _job(customer)
    assert (after.id, after.status, after.payload) == (job.id, DONE, job.payload)  # nothing new was queued
    assert len(await _plans(customer)) == 1 and len(provider.prompts) == 1
    assert await run_jobs() == 0
    assert (await guard.status(customer.id))["strikes"] > 0  # a refusal earns strike points like any other


async def test_a_cooldown_refusal_is_an_error_flash(client, db, monkeypatch):
    customer = await _member(db)
    await login_as(client, customer)

    async def paused(owner, feature, text=None):
        assert feature == "coach" and owner is not None and owner.user_id == customer.id
        raise AIRejected("cooldown", "AI is paused on your account for 2 min because of too many requests in a short "
                                     "time.", retry_after=120, feature=feature)

    monkeypatch.setattr(guard, "admit", paused)
    r = await post_form(client, "/me/coach/generate")
    assert r.status_code == 303 and r.headers["location"] == "/me/coach"
    page = html.unescape((await client.get("/me/coach")).text)
    assert "alert-danger" in page and "AI is paused on your account for 2 min" in page
    assert await _job(customer) is None


async def test_suspended_members_still_cannot_generate(client, db, fake_ai):
    provider = fake_ai(responses=[PLAN])
    customer = await _member(db)
    prof = await db.get(CustomerProfile, customer.id)
    prof.activity_suspended = True
    await db.commit()
    await login_as(client, customer)
    r = await post_form(client, "/me/coach/generate")
    assert r.status_code == 303 and r.headers["location"] == "/me/coach"
    assert await _job(customer) is None and await _gen_used(customer.id) == 0 and provider.prompts == []


async def test_a_member_suspended_after_the_click_gets_no_plan(client, db, fake_ai):
    provider = fake_ai(responses=[PLAN])
    customer = await _member(db)
    await login_as(client, customer)
    await post_form(client, "/me/coach/generate")
    prof = await db.get(CustomerProfile, customer.id)
    prof.activity_suspended = True  # e.g. a red-flag check-in while the plan was still queued
    await db.commit()
    q = hub.subscribe(customer.id)
    try:
        assert await run_jobs() == 1
        await _ticks()
        done = [e for e in _updates(q) if e["type"] == "task.update"][0]
    finally:
        hub.unsubscribe(customer.id, q)
    assert await _plans(customer) == [] and provider.prompts == [] and (await _job(customer)).status == DONE
    assert done["status"] == "done" and done["notify"] is False and "suspended" in done["message"]


async def test_the_charge_is_refunded_when_queueing_fails(db, monkeypatch):
    customer = await _member(db)

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr(coach_service, "enqueue", broken)
    owner = AIOwner(user_id=customer.id, role="customer")
    async with dbmod.get_sessionmaker()() as s:
        user = await s.get(type(customer), customer.id)
        access = await plan_access(s, customer.id)
        with owner_scope(owner), pytest.raises(RuntimeError):
            await coach_service.queue_plan(s, user, access=access, owner=owner)
    assert await _gen_used(customer.id) == 0 and await _job(customer) is None


# ============================================================================================ double clicks
async def test_a_double_click_queues_one_job_and_charges_once(client, db, fake_ai):
    fake_ai(responses=[PLAN])
    customer = await _member(db)
    await login_as(client, customer)
    first, second = await asyncio.gather(post_form(client, "/me/coach/generate"),
                                         post_form(client, "/me/coach/generate"))
    job = await _job(customer)
    assert first.status_code == second.status_code == 303
    assert first.headers["location"] == second.headers["location"] == f"/me/coach?generating={job.id}"
    assert await db.scalar(select(func.count(Job.id)).where(Job.kind == "coach_plan")) == 1
    assert await _gen_used(customer.id) == 3  # charged once: the second click found the first one's job
    third = await post_form(client, "/me/coach/generate")  # still queued: told so, nothing new
    assert third.headers["location"] == f"/me/coach?generating={job.id}"
    assert "already being built" in html.unescape((await client.get(third.headers["location"])).text)
    assert await run_jobs() == 1 and len(await _plans(customer)) == 1


# ============================================================================================ status endpoint
async def test_status_is_owner_scoped_read_only_and_never_charges(client, db, fake_ai):
    fake_ai(responses=[PLAN])
    alice = await _member(db)
    bob = await _member(db)
    await login_as(client, alice)
    await post_form(client, "/me/coach/generate")
    job = await _job(alice)
    used = await _gen_used(alice.id)
    for _ in range(5):
        assert (await api(client, "GET", f"/me/coach/status?job={job.id}")).status_code == 200
    assert await _gen_used(alice.id) == used and (await _job(alice)).status == QUEUED  # polling changes nothing

    async with make_client() as other:
        await login_as(other, bob)
        theirs = await api(other, "GET", f"/me/coach/status?job={job.id}")
        assert theirs.status_code == 404 and "queued" not in theirs.text
        for bad in ("", "abc", "-1", "0", "²", "1e3", "99999999999999999999"):
            assert (await api(other, "GET", f"/me/coach/status?job={bad}")).status_code == 404, bad
        page = await other.get(f"/me/coach?generating={job.id}")  # another member's job id in the URL
        assert page.status_code == 200 and 'data-component="coach-job"' not in page.text
    owner = await make_owner(db)
    async with make_client() as staff:
        await login_as(staff, owner)
        assert (await api(staff, "GET", f"/me/coach/status?job={job.id}")).status_code == 403


async def test_a_failed_job_shows_a_notice_only_on_its_redirect_page(client, db, monkeypatch):
    customer = await _member(db, ai=False)
    monkeypatch.setattr(coach_service, "PLAN_JOB_ATTEMPTS", 1)

    async def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("library table is corrupt")

    monkeypatch.setattr(coach_service, "generate_plan", explode)
    await login_as(client, customer)
    r = await post_form(client, "/me/coach/generate")
    job = await _job(customer)
    assert await run_jobs() == 1 and (await _job(customer)).status == FAILED
    status = (await api(client, "GET", f"/me/coach/status?job={job.id}")).json()
    assert status["status"] == "failed" and status["active"] is False and status["link"] is None
    page = html.unescape((await client.get(r.headers["location"])).text)
    assert "We couldn't build your plan" in page and 'data-component="coach-job"' not in page
    plain = html.unescape((await client.get("/me/coach")).text)
    assert "We couldn't build your plan" not in plain and 'title="Your plan is being built"' not in plain


# ============================================================================================ leaving and returning
async def test_leaving_pauses_a_queued_plan_and_returning_resumes_it(client, db, fake_ai, monkeypatch):
    monkeypatch.setattr(get_settings(), "presence_grace_seconds", 0.1)
    fake_ai(responses=[PLAN])
    customer = await _member(db)
    await login_as(client, customer)
    await post_form(client, "/me/coach/generate")
    job = await _job(customer)
    q = hub.subscribe(customer.id)
    try:
        await presence.on_left(customer.id)  # their last page closed and the grace period ran out
        await _ticks()
        paused = await _job(customer)
        assert (paused.status, paused.attempts, paused.cancel_reason) == (PAUSED, 0, "left")
        assert await run_jobs() == 0  # nothing runs for a member who is away
        assert [(e["task_id"], e["status"]) for e in _updates(q) if e["type"] == "task.update"] == [(job.id, "paused")]

        # back: any signed-in request resumes their paused work (load_user -> presence.touch)
        status = await api(client, "GET", f"/me/coach/status?job={job.id}")
        assert status.status_code == 200
        await presence.drain()
        resumed = await _job(customer)
        assert resumed.status == QUEUED and resumed.paused_at is None
        updates = [e for e in _updates(q) if e["type"] == "task.update"]
        assert [(e["status"], e.get("resumed")) for e in updates] == [("queued", True)]
    finally:
        hub.unsubscribe(customer.id, q)
    assert await run_jobs() == 1
    assert (await _job(customer)).status == DONE and len(await _plans(customer)) == 1


async def test_the_page_says_paused_while_away_and_the_poll_follows_the_resume(client, db, fake_ai):
    fake_ai(responses=[PLAN])
    customer = await _member(db)
    await login_as(client, customer)
    r = await post_form(client, "/me/coach/generate")
    job = await _job(customer)
    assert await J.apply_exit_policy(customer.id, reason="left") == {"paused": 1, "cancelled": 0, "running_stopped": 0}
    page = html.unescape((await client.get(r.headers["location"])).text)
    assert 'data-status="paused"' in page and "Paused while you were away - resumes now that you're back." in page
    assert (await api(client, "GET", f"/me/coach/status?job={job.id}")).json()["status"] == "paused"
    assert await J.resume_user_jobs(customer.id) == 1
    assert (await api(client, "GET", f"/me/coach/status?job={job.id}")).json()["status"] == "queued"
    assert await run_jobs() == 1
    assert (await api(client, "GET", f"/me/coach/status?job={job.id}")).json()["status"] == "done"


async def test_a_click_while_paused_puts_the_plan_back_in_the_queue_without_a_new_charge(client, db, fake_ai):
    fake_ai(responses=[PLAN])
    customer = await _member(db)
    await login_as(client, customer)
    await post_form(client, "/me/coach/generate")
    job = await _job(customer)
    await J.apply_exit_policy(customer.id, reason="left")
    r = await post_form(client, "/me/coach/generate")
    assert r.headers["location"] == f"/me/coach?generating={job.id}"
    again = await _job(customer)
    assert again.status == QUEUED and again.payload == job.payload and await _gen_used(customer.id) == 3
    assert await run_jobs() == 1 and len(await _plans(customer)) == 1


async def test_leaving_stops_a_running_plan_and_returning_finishes_it(client, db, fake_ai, monkeypatch):
    monkeypatch.setattr(get_settings(), "presence_grace_seconds", 0.1)
    release = asyncio.Event()
    calls = {"started": 0, "cancelled": 0}

    async def slow_model(**kwargs: Any) -> str:
        calls["started"] += 1
        try:
            await release.wait()
        except asyncio.CancelledError:
            calls["cancelled"] += 1
            raise
        return PLAN

    fake_ai(handler=slow_model)
    customer = await _member(db)
    await login_as(client, customer)
    await post_form(client, "/me/coach/generate")
    job = await _job(customer)
    runner = asyncio.create_task(run_jobs())
    await _wait_until(lambda: calls["started"] == 1, "the plan's AI call")
    assert (await _job(customer)).status == RUNNING

    result = await presence.on_left(customer.id)
    assert result is not None and result["jobs"]["paused"] == 1 and result["jobs"]["running_stopped"] == 1
    assert await asyncio.wait_for(runner, 10) == 1
    stopped = await _job(customer)
    assert (stopped.status, stopped.attempts, stopped.cancel_reason) == (PAUSED, 0, "left")  # not charged
    assert calls["cancelled"] == 1 and await _plans(customer) == []  # the model call stopped, nothing saved

    await client.get("/me/coach")  # they are back
    await presence.drain()
    assert (await _job(customer)).status == QUEUED
    release.set()
    assert await run_jobs() == 1
    done = await _job(customer)
    assert (done.status, done.attempts) == (DONE, 1) and calls["started"] == 2
    (plan,) = await _plans(customer)
    assert plan.source == "ai" and plan.plan["meta"]["request"] == job.payload["request"]


async def test_an_interrupted_run_that_already_saved_the_plan_does_not_build_a_second_one(db, fake_ai):
    provider = fake_ai(responses=[PLAN])
    customer = await _member(db)
    payload = {"user_id": customer.id, "ai": True, "request": "req-0001"}
    async with dbmod.get_sessionmaker()() as s:
        await coach_service.plan_job(s, payload)  # saved the plan, then (say) paused before the job was marked done
        await s.commit()
    async with dbmod.get_sessionmaker()() as s:
        await coach_service.plan_job(s, payload)  # resumed: the same click's plan already exists
        await s.commit()
    assert len(await _plans(customer)) == 1 and len(provider.prompts) == 1
    async with dbmod.get_sessionmaker()() as s:
        await coach_service.plan_job(s, {"user_id": 987654})  # unknown member -> no-op
        await coach_service.plan_job(s, {"user_id": "x"})  # malformed payload -> no-op


# ============================================================================================ the coach page script
COACH_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "src" / "js" / "51-coach.js"
MAC_JSC = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"


def _jsc() -> str | None:
    for candidate in (os.environ.get("JSC"), shutil.which("jsc"), MAC_JSC):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


COACH_HARNESS = r"""/* 51-coach.js "coach-job" in JavaScriptCore: a virtual clock, IP.on/IP.api stubs and a fake card. */
var window = globalThis;
var __now = 0, __timers = [], __tid = 0, __pass = 0, __fail = 0;
globalThis.setTimeout = function (fn, ms) {
  var id = ++__tid;
  __timers.push({ id: id, at: __now + Math.max(0, Number(ms) || 0), fn: fn });
  return id;
};
globalThis.clearTimeout = function (id) { __timers = __timers.filter(function (t) { return t.id !== id; }); };
function advance(ms) {
  var end = __now + ms;
  for (var guard = 0; guard < 100000; guard++) {
    drainMicrotasks();
    var due = null;
    __timers.forEach(function (t) { if (t.at <= end && (!due || t.at < due.at)) due = t; });
    if (!due) break;
    __now = Math.max(__now, due.at);
    __timers.splice(__timers.indexOf(due), 1);
    due.fn();
  }
  __now = end;
  drainMicrotasks();
}
var document = { hidden: false };
var replaced = [], apiCalls = [], apiQueue = [], listeners = {};
window.location = { replace: function (u) { replaced.push(String(u)); } };
window.IP = {
  _c: {},
  register: function (name, fn) { this._c[name] = fn; },
  on: function (type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  api: function (url) {
    apiCalls.push(url);
    var next = apiQueue.length ? apiQueue.shift() : { status: "queued" };
    return next instanceof Error ? Promise.reject(next) : Promise.resolve(next);
  },
};
function emit(type, detail) { (listeners[type] || []).forEach(function (fn) { fn(detail); }); drainMicrotasks(); }
function card(attrs) {
  var a = {}, kids = {};
  Object.keys(attrs).forEach(function (k) { a[k] = attrs[k]; });
  return { a: a, kids: kids, getAttribute: function (k) { return k in a ? a[k] : null; },
           setAttribute: function (k, v) { a[k] = String(v); },
           querySelector: function (s) { return kids[s] || (kids[s] = { textContent: "" }); } };
}
function check(name, cond, info) {
  if (cond) { __pass++; print("PASS " + name); } else { __fail++; print("FAIL " + name + (info ? " :: " + info : "")); }
}
function start(attrs) {
  __timers = []; __now = 0; replaced = []; apiCalls = []; apiQueue = []; listeners = {}; document.hidden = false;
  IP.ws = { connected: false };
  var c = card(attrs || { "data-job": "7", "data-status-url": "/me/coach/status?job=7", "data-status": "queued" });
  IP._c["coach-job"](c);
  return c;
}
function err(status) { var e = new Error("HTTP " + status); e.status = status; return e; }

load(arguments[0]);
check("registered", typeof IP._c["coach-job"] === "function" && typeof IP._c["coach-days"] === "function");

// --- WebSocket task.update drives the card; only this job's coach_plan events count
var c = start();
advance(2999);
check("first poll after 3 s", apiCalls.length === 0);
advance(1);
check("polls its own status url", apiCalls.length === 1 && apiCalls[0] === "/me/coach/status?job=7", apiCalls.join());
emit("task.update", { type: "task.update", kind: "coach_plan", task_id: 7, status: "paused" });
check("paused text", c.a["data-status"] === "paused" &&
      c.kids["[data-coach-job-text]"].textContent === "Paused while you were away - resumes now that you're back.");
emit("task.update", { kind: "coach_plan", task_id: 8, status: "done" });
emit("task.update", { kind: "health_ac_note", task_id: 7, status: "done" });
check("other jobs and kinds ignored", replaced.length === 0 && c.a["data-status"] === "paused");
emit("task.update", { kind: "coach_plan", task_id: "7", status: "queued", resumed: true });
check("resumed -> queued", c.a["data-status"] === "queued" && c.kids["[data-coach-job-title]"].textContent === "Building your plan");
emit("task.update", { kind: "coach_plan", task_id: 7, status: "running" });
check("running text", c.kids["[data-coach-job-text]"].textContent.indexOf("safety checks") !== -1);
emit("task.update", { kind: "coach_plan", task_id: 7, status: "done", link: "//evil.example/x" });
check("done shows the plan (never a foreign link)", replaced.length === 1 && replaced[0] === "/me/coach", replaced.join());
var calls = apiCalls.length;
advance(600000);
emit("task.update", { kind: "coach_plan", task_id: 7, status: "failed" });
check("stops after done", apiCalls.length === calls && replaced.length === 1);

// --- polling fallback: 3 s for the first ten checks, then 5 s; done/failed navigate
c = start();
apiQueue = [{ status: "queued" }, { status: "running" }, { status: "done", link: "/me/coach" }];
advance(9000);
check("poll fallback reaches done", apiCalls.length === 3 && replaced[0] === "/me/coach", apiCalls.length + " " + replaced.join());
c = start();
apiQueue = [{ status: "failed" }];
advance(3000);
check("failed reloads to the notice", replaced.length === 1 && replaced[0] === "/me/coach?generating=7", replaced.join());
c = start();
apiQueue = [{ status: "cancelled" }];
advance(3000);
check("cancelled reloads to the notice", replaced[0] === "/me/coach?generating=7");
c = start();
for (var i = 0; i < 12; i++) apiQueue.push({ status: "queued" });
advance(30000);
check("3 s cadence for ten checks", apiCalls.length === 10, String(apiCalls.length));
advance(10000);
check("then every 5 s", apiCalls.length === 12, String(apiCalls.length));

// --- with the socket up it delivers task.update: the poll is only a safety net (a gym shares one IP)
c = start();
IP.ws.connected = true;
advance(3000);
check("first check after 3 s even with the socket up", apiCalls.length === 1);
advance(9999);
check("socket up: no 3 s polling", apiCalls.length === 1, String(apiCalls.length));
advance(1);
check("socket up: every 10 s", apiCalls.length === 2, String(apiCalls.length));
IP.ws.connected = false;
advance(10000);
check("socket down again: back to 3 s", apiCalls.length === 3, String(apiCalls.length));
advance(3000);
check("socket down: 3 s cadence", apiCalls.length === 4, String(apiCalls.length));

// --- stop conditions: 404 / 401 / 403 stop, network errors keep trying, hidden tabs wait, a cap
c = start();
apiQueue = [err(404)];
advance(3000);
advance(600000);
check("404 stops polling", apiCalls.length === 1 && replaced.length === 0, String(apiCalls.length));
c = start();
apiQueue = [err(0), { status: "done" }];
advance(6000);
check("network error retried", apiCalls.length === 2 && replaced[0] === "/me/coach", String(apiCalls.length));
c = start();
document.hidden = true;
advance(13000);
check("hidden tab does not poll", apiCalls.length === 0);
document.hidden = false;
advance(5000);
check("visible again: polls", apiCalls.length === 1);
c = start();
advance(30 * 60 * 1000);
check("gives up polling after the cap", apiCalls.length === 240, String(apiCalls.length));
emit("task.update", { kind: "coach_plan", task_id: 7, status: "done" });
check("the socket still finishes it", replaced[0] === "/me/coach");
c = start();
apiQueue = [{ status: "queued" }, { status: "done" }];
advance(1000);
emit("page.restored", {});
advance(0);
check("bfcache restore polls at once", apiCalls.length === 1);

// --- a card without its attributes does nothing
c = start({ "data-status": "queued" });
advance(60000);
check("no job id: inert", apiCalls.length === 0 && __timers.length === 0);

print("SUMMARY passed=" + __pass + " failed=" + __fail);
"""


@pytest.mark.skipif(_jsc() is None, reason="JavaScriptCore (jsc) is not available on this machine")
def test_coach_page_script_follows_the_job_in_javascriptcore(tmp_path):
    harness = tmp_path / "coach_harness.js"
    harness.write_text(COACH_HARNESS, encoding="utf-8")
    proc = subprocess.run([_jsc(), str(harness), "--", str(COACH_JS)], capture_output=True, text=True, timeout=120,
                          cwd=tmp_path)
    out = proc.stdout + proc.stderr
    m = re.search(r"SUMMARY passed=(\d+) failed=(\d+)", out)
    assert m, out[-4000:]
    failures = [line for line in out.splitlines() if line.startswith("FAIL")]
    assert m.group(2) == "0" and not failures, "\n".join(failures) + "\n" + out[-3000:]
    assert int(m.group(1)) >= 20, out


def test_coach_page_script_keeps_to_the_csp():
    code = COACH_JS.read_text(encoding="utf-8")
    for token in ("eval(", "new Function(", "document.write(", "innerHTML", ".style ="):
        assert token not in code, token
