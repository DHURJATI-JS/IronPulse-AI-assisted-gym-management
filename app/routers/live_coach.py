"""WP-K live AI workout companion: animated face, Kokoro/browser voice, live voice chat, safety stop.

Pages
    GET  /me/live                 start screen (plan day / quick sessions, voice settings, gear, recent sessions)
    POST /me/live/start           no-JS fallback for starting a session (form)
    GET  /me/live/{session_id}    the live session (coach stage + workout HUD + conversation)
JSON (login required - anonymous callers get 401, other members' sessions 404)
    POST /api/live/sessions                       {"plan_id"?, "day_index"?, "quick"?, "voice_enabled"?}
    GET  /api/live/sessions/{id}
    POST /api/live/sessions/{id}/message          {"text", "state": {"index", "set", "phase", "elapsed_s"}, "via"?, "ai"?}
    POST /api/live/sessions/{id}/event            {"type": started|set_done|exercise_done|paused|resumed|skipped|completed}
    POST /api/live/sessions/{id}/discomfort       {"body_part", "severity", "note"?}
    POST /api/live/sessions/{id}/end              {"reason"}
    POST /api/tts                                 {"text" (<= 400 chars), "voice"?} -> audio/mpeg | 503 {"fallback": "browser"}
                                                  | 429 {"code": quota|cooldown, "retry_after", "fallback": "browser"}
    GET  /api/tts/status[?probe=1]

AI refusals never stop a workout: a refused coach reply is still HTTP 200 - a short spoken line plus
``"refused": {"code", "detail", "retry_after"}`` (the browser pauses AI chat that long and sends ``"ai": false``
meanwhile: safety checks and commands still run, nothing reaches the AI). A refused voice line answers 429 (per-user
voice quota, AI paused for the account) or 503 (voice lane busy) with ``Retry-After`` and ``"fallback": "browser"``.

No route holds a pooled DB connection while it waits for the AI: the coach's AI turn commits what the member said
before it asks (app/services/live_coach.py), and the voice routes close the session that signed the member in before
they wait for the voice lane and Kokoro.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app.ai.client import AI_DISCLAIMER
from app.ai.errors import AIRejected
from app.config import get_settings
from app.core.deps import DB, AuthUser, CustomerUser, NotFound
from app.core.templating import redirect, render
from app.models import WorkoutSession
from app.services import live_coach as live
from app.services.conditions import TAG_LABELS
from app.services.gear import recommend_gear
from app.services.tts import clean_text, tts, valid_voice

router = APIRouter()


# ====================================================================================== request bodies
class StateIn(BaseModel):
    index: int = Field(0, ge=0, le=500)
    set: int = Field(1, ge=1, le=50)
    phase: str = Field("work", max_length=12)
    elapsed_s: int = Field(0, ge=0, le=86400)
    voice: bool | None = None


class StartIn(BaseModel):
    plan_id: int | None = Field(None, ge=1)
    day_index: int | None = Field(None, ge=0, le=13)
    quick: str | None = Field(None, max_length=40)
    voice_enabled: bool = True


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    state: StateIn | None = None
    via: Literal["voice", "text"] = "text"
    ai: bool = True  # false while the browser waits out an AI refusal: safety checks + commands only, no AI call


class EventIn(BaseModel):
    type: Literal["started", "set_done", "exercise_done", "paused", "resumed", "skipped", "completed"]
    state: StateIn | None = None


class DiscomfortIn(BaseModel):
    body_part: Literal["chest", "head", "back", "knee", "shoulder", "wrist", "ankle", "neck", "stomach", "other"]
    severity: Literal["mild", "moderate", "severe"] = "moderate"
    note: str | None = Field(None, max_length=300)
    state: StateIn | None = None


class EndIn(BaseModel):
    reason: str = Field("user_ended", max_length=120)


class TTSIn(BaseModel):
    text: str = Field(min_length=1, max_length=400)
    voice: str | None = Field(None, max_length=60)


# ====================================================================================== helpers
SUSPENDED_DETAIL = "Your gym activities are paused for your safety - a gym admin must reactivate them first."
NO_PLAN_DETAIL = "An active membership is required for live coaching."


def _forbidden(detail: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": "Access denied", "detail": detail, **extra}, status_code=403)


async def _own_session(db: Any, user: Any, session_id: int) -> WorkoutSession:
    s = await live.get_session(db, user, session_id)
    if s is None:
        raise NotFound("Session not found.")
    return s


def _state(payload: Any) -> live.ClientState:
    return live.parse_state(payload.model_dump() if payload is not None else None)


def _condition_labels(member: live.Member) -> list[str]:
    return sorted(TAG_LABELS.get(t, t.replace("_", " ").capitalize()) for t in member.tags if t != "minor")


def _blocked(request: Request, member: live.Member, reason: str):
    return render(request, "customer/live/blocked.html", {
        "reason": reason,
        "suspension_reason": member.profile.suspension_reason if member.profile else None,
        "emergency": member.emergency,
    })


def _session_config(member: live.Member, s: WorkoutSession) -> dict[str, Any]:
    base = f"/api/live/sessions/{s.id}"
    return {
        "ai": bool(member.access.ai),
        "first_name": member.first_name,
        "voice": get_settings().tts_voice,
        "tts": tts.status(),
        "emergency": member.emergency,
        "disclaimer": AI_DISCLAIMER,
        "suspended": member.suspended,
        "urls": {"message": f"{base}/message", "event": f"{base}/event", "discomfort": f"{base}/discomfort",
                 "end": f"{base}/end", "tts": "/api/tts", "tts_status": "/api/tts/status"},
    }


# ====================================================================================== pages
@router.get("/me/live")
async def live_home(request: Request, db: DB, user: CustomerUser):
    member = await live.load_member(db, user)
    if member.suspended:
        return _blocked(request, member, "suspended")
    if not member.access.has_any:
        return _blocked(request, member, "no_subscription")
    library = await live.load_library(db)
    plan = await live.active_plan(db, user.id)
    options = live.start_screen_options(plan, library, member)
    recent = live.recent_view(await live.recent_sessions(db, user.id))
    return render(request, "customer/live/index.html", {
        "ai_feature": True,
        "first_name": member.first_name,
        "ai_plan": bool(member.access.ai),
        "plan": plan if options["days"] else None,
        "options": options,
        "gear": recommend_gear(member.condition_names, age=member.age),
        "recent": recent,
        "resumable": next((r for r in recent if r["fresh"]), None),
        "tts": tts.status(),
        "emergency": member.emergency,
        "condition_labels": _condition_labels(member),
        "error": None,
    })


@router.post("/me/live/start")
async def live_start_form(request: Request, db: DB, user: CustomerUser):
    form = await request.form()
    member = await live.load_member(db, user)
    if member.suspended:
        return redirect("/me/live", SUSPENDED_DETAIL, "error")
    if not member.access.has_any:
        return redirect("/me/live", NO_PLAN_DETAIL, "error")
    pick = str(form.get("pick") or "").strip()
    kwargs: dict[str, Any] = {"voice_enabled": str(form.get("voice") or "") == "1"}
    if pick.startswith("quick-"):
        kwargs["quick"] = pick[6:46]
    elif pick.startswith("day-") and pick[4:].isdigit() and str(form.get("plan_id") or "").isdigit():
        kwargs.update(plan_id=int(str(form.get("plan_id"))), day_index=min(int(pick[4:]), 13))
    else:
        return redirect("/me/live", "Please pick a workout first.", "error")
    try:
        s = await live.create_session(db, member, **kwargs)
    except live.LiveError as exc:
        return redirect("/me/live", str(exc), "error")
    await db.commit()
    return redirect(f"/me/live/{s.id}")


@router.get("/me/live/{session_id}")
async def live_session_page(request: Request, session_id: int, db: DB, user: CustomerUser):
    s = await _own_session(db, user, session_id)
    member = await live.load_member(db, user)
    if s.status != "stopped_for_safety":
        if member.suspended:
            return _blocked(request, member, "suspended")
        if not member.access.has_any:
            return _blocked(request, member, "no_subscription")
    return render(request, "customer/live/session.html", {
        "ai_feature": True,
        "session": s,
        "payload": live.session_payload(s),
        "config": _session_config(member, s),
        "first_name": member.first_name,
        "ai_plan": bool(member.access.ai),
        "emergency": member.emergency,
        "body_parts": live.BODY_PARTS,
        "safety_text": await live.safety_guidance(db, member, s),
    })


# ====================================================================================== JSON API
@router.post("/api/live/sessions")
async def api_create_session(payload: StartIn, db: DB, user: CustomerUser):
    member = await live.load_member(db, user)
    if member.suspended:
        return _forbidden(SUSPENDED_DETAIL, suspended=True)
    if not member.access.has_any:
        return _forbidden(NO_PLAN_DETAIL)
    try:
        s = await live.create_session(db, member, plan_id=payload.plan_id, day_index=payload.day_index,
                                      quick=payload.quick, voice_enabled=payload.voice_enabled)
    except live.LiveError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    await db.commit()
    return {"session": live.session_payload(s), "url": f"/me/live/{s.id}", "disclaimer": AI_DISCLAIMER}


@router.get("/api/live/sessions/{session_id}")
async def api_get_session(session_id: int, db: DB, user: CustomerUser):
    s = await _own_session(db, user, session_id)
    return {"session": live.session_payload(s), "disclaimer": AI_DISCLAIMER}


@router.post("/api/live/sessions/{session_id}/message")
async def api_message(request: Request, session_id: int, payload: MessageIn, db: DB, user: CustomerUser):
    s = await _own_session(db, user, session_id)
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Say or type something first.")
    member = await live.load_member(db, user)
    out = await live.handle_message(db, member, s, text, _state(payload.state), via=payload.via, request=request,
                                    ai=payload.ai)
    queue_ac = bool(out.pop("_queue_ac", False))
    await db.commit()
    if queue_ac:  # "forget ..." changed the conditions of a plan with AI: the AC wording follows in the background
        from app.ai import memory

        await memory.after_commit(db, user, {"queue_ac": True})
    return out


@router.post("/api/live/sessions/{session_id}/event")
async def api_event(session_id: int, payload: EventIn, db: DB, user: CustomerUser):
    s = await _own_session(db, user, session_id)
    member = await live.load_member(db, user)
    if not member.access.has_any and s.status not in live.ENDED:
        return _forbidden(NO_PLAN_DETAIL)
    out = await live.handle_event(db, member, s, payload.type, _state(payload.state))
    await db.commit()
    out["cue"] = out.get("reply", "")
    return out


@router.post("/api/live/sessions/{session_id}/discomfort")
async def api_discomfort(request: Request, session_id: int, payload: DiscomfortIn, db: DB, user: CustomerUser):
    s = await _own_session(db, user, session_id)
    member = await live.load_member(db, user)
    out = await live.report_discomfort(db, member, s, body_part=payload.body_part, severity=payload.severity,
                                       note=payload.note, source="button", request=request)
    await db.commit()
    return out


@router.post("/api/live/sessions/{session_id}/end")
async def api_end(session_id: int, payload: EndIn, db: DB, user: CustomerUser):
    s = await _own_session(db, user, session_id)
    member = await live.load_member(db, user)
    out = await live.end_session(db, member, s, payload.reason)
    await db.commit()
    return out


# ====================================================================================== text-to-speech
def _voice_refused(exc: AIRejected) -> JSONResponse:
    """A refused voice line: the browser speaks with its own voice and does not ask again for ``retry_after`` seconds.
    429 + the contract's refusal JSON for the member's own limits (voice quota, AI paused on the account); 503 for
    everything else (too many voice requests at once, the voice lane busy) - like an unavailable engine."""
    headers = {"Cache-Control": "no-store", **exc.headers()}
    if exc.code in ("quota", "cooldown"):
        return JSONResponse({**exc.payload(), "fallback": "browser", "tts": tts.status()}, status_code=429,
                            headers=headers)
    return JSONResponse({"fallback": "browser", "detail": exc.message, "code": exc.code, "retry_after": exc.retry_after,
                         "tts": tts.status()}, status_code=503, headers=headers)


@router.post("/api/tts")
async def api_tts(payload: TTSIn, db: DB, user: AuthUser):
    text = clean_text(payload.text)
    if not text:
        raise HTTPException(status_code=422, detail="Nothing to say.")
    if payload.voice and not valid_voice(payload.voice):
        raise HTTPException(status_code=422, detail="Unknown voice.")
    await db.close()  # signed in, nothing to write: no pooled connection while the voice lane and Kokoro work
    try:
        # the member's own cached lines are free and never refused; another member's lines are never visible here
        # (cached per member), and a fetch the browser aborts gives its place in the voice lane back at once
        audio = await tts.synthesize_or_refuse(text, payload.voice)
    except AIRejected as exc:
        return _voice_refused(exc)
    if audio is None:
        return JSONResponse({"fallback": "browser", "detail": "The natural voice is unavailable - using the browser voice.",
                             "tts": tts.status()}, status_code=503, headers={"Cache-Control": "no-store"})
    return Response(audio, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/api/tts/status")
async def api_tts_status(db: DB, user: AuthUser, probe: bool = False):
    if not probe:
        return tts.status()
    await db.close()  # the probe synthesises a short phrase: no pooled connection meanwhile
    return await tts.probe()
