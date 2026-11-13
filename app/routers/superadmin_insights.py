"""``/superadmin/insights`` - the super-admin's site overview: "what is going on, and what should I fix first?"

GET  /superadmin/insights      the digest as cards and the urgent "fix first" list (app/services/admin_insights.py) -
                               works without AI and without JavaScript
POST /superadmin/insights/ask  one question for the AI, answered from the SAME digest only (feature 'admin_overview')
     JSON in  {"question": str} or {"suggestion": one of SUGGESTIONS} (IP.ai.request, 71-insights.js) -> JSON out
              {"ok": true, "source": "ai" | "fallback", "provider", "question", "overview", "fix_first": [{"title",
               "why", "severity", "link"}], "note", "urgent", "generated_at", "disclaimer"}
              a refusal: 429 / 409 / 422 + code + Retry-After (app/core/errors.py - IP.ai starts its cooldown)
     form in  (no JavaScript) -> this page again, with the answer or the reason it was refused

Every ask is rate-limited - the AI guard's superadmin quotas for 'admin_overview' (app/ai/policy.py) and at most
ASK_PER_MINUTE per admin per minute, shared by every worker - and audited (``insights.ask``: the question, the outcome,
the provider). The request's database connection goes back to the pool before the AI works. When the AI is unavailable
or busy the answer is the deterministic overview with a short note. The AI never runs queries and never acts.
CSP-safe: no inline script or style; behaviour comes from the bundle (app/static/src/js/71-insights.js).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.ai.client import AI_DISCLAIMER, AIUnavailableError, ai, flush_ai_call_logs
from app.ai.errors import AICancelled, AIRejected
from app.ai.owner import clean_page_id, current_owner
from app.core.deps import DB, SuperAdminUser, wants_json
from app.core.templating import render, templates
from app.core.utils import utcnow
from app.routers.superadmin import COUNT_CAP, SECTIONS, nav_counts
from app.security.ratelimit import limiter
from app.services import admin_insights as insights
from app.services.audit import audit

log = logging.getLogger("ironpulse.insights")
router = APIRouter()

SECTION = ("insights", "Site overview", "/superadmin/insights", "sparkles")
ASK_PER_MINUTE = 10  # questions one super-admin may ask per minute (on top of the AI guard's quotas)
ASK_WINDOW = 60.0
AI_TIMEOUT = 75.0  # per provider attempt, from the moment it holds its slot (never the wait in the AI queue)
MAX_TOKENS = 900
TEASER_MAX_AGE = 1800  # seconds a cached digest may feed the platform overview's teaser
UNAVAILABLE_NOTE = "AI is unavailable right now - this is the automatic overview of the same numbers."
BUSY_NOTE = "AI is busy right now - this is the automatic overview of the same numbers."
SEVERITY_TONES = {"critical": "red", "high": "red", "medium": "amber", "low": "blue"}


def sections() -> tuple[tuple[str, str, str, str], ...]:
    return SECTIONS if any(s[0] == SECTION[0] for s in SECTIONS) else (*SECTIONS, SECTION)


def _when(iso: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None


async def _render(request: Request, db: Any, digest: dict[str, Any], *, answer: dict[str, Any] | None = None,
                  error: str | None = None, question: str = "", status_code: int = 200) -> Any:
    counts = await nav_counts(db)
    return render(request, "superadmin/insights.html", {
        "sections": sections(), "section": SECTION[0], "counts": counts, "count_cap": COUNT_CAP,
        "d": digest, "urgent": insights.urgent(digest), "people": digest.get("people") or {},
        "generated_at": _when(digest.get("generated_at")), "suggestions": insights.SUGGESTIONS,
        "answer": answer, "ask_error": error, "question": question, "question_max": insights.QUESTION_MAX,
        "feature": insights.FEATURE, "tones": SEVERITY_TONES, "category_labels": insights.CATEGORY_LABELS,
        "guard_labels": insights.GUARD_REFUSALS, "enquiry_labels": insights.ENQUIRY_LABELS,
        "ask_per_minute": ASK_PER_MINUTE,
    }, status_code=status_code)


@router.get("/superadmin/insights")
async def insights_page(request: Request, db: DB, admin: SuperAdminUser):
    """The digest (cached ~2 minutes per process) and the urgent list - a constant number of statements."""
    return await _render(request, db, await insights.load_digest(db))


async def _input(request: Request, as_json: bool) -> dict[str, Any]:
    if as_json:
        try:
            body = await request.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}
    form = await request.form()
    return {key: value for key in ("question", "suggestion", "page_id")
            if isinstance(value := form.get(key), str)}


async def _audit(db: Any, admin: Any, request: Request, detail: dict[str, Any]) -> None:
    await audit(db, admin, "insights.ask", "site", None, request=request, detail=detail)
    await db.commit()


def _provider_label(name: str | None) -> str | None:
    if not name:
        return None
    return next((p["label"] for p in ai.status()["providers"] if p["name"] == name), name)


@router.post("/superadmin/insights/ask")
async def insights_ask(request: Request, db: DB, admin: SuperAdminUser):
    as_json = "application/json" in (request.headers.get("content-type") or "").lower()
    data = await _input(request, as_json)
    reply_json = as_json or wants_json(request)
    owner = current_owner()
    if owner is not None:  # a plain form post names its tab in a field (IP.ai sends the X-Page-Id header)
        owner.page_id = clean_page_id(data.get("page_id")) or owner.page_id
    suggestion = str(data.get("suggestion") or "")
    question = suggestion if suggestion in insights.SUGGESTIONS else insights.clean_question(data.get("question"))
    if question is None:
        detail = (f"Ask a question of {insights.QUESTION_MIN}-{insights.QUESTION_MAX} characters, or pick one of the "
                  "suggestions.")
        if reply_json:
            return JSONResponse({"error": "Invalid question", "detail": detail, "code": "invalid"}, status_code=400)
        return await _render(request, db, await insights.load_digest(db), error=detail, status_code=400,
                             question=str(data.get("question") or "")[: insights.QUESTION_MAX])
    asked = {"question": question[:200], "suggested": question in insights.SUGGESTIONS}

    recent = await limiter.note(f"insights:{admin.id}", "ask", window=ASK_WINDOW)
    if recent > ASK_PER_MINUTE:
        await _audit(db, admin, request, {**asked, "outcome": "rate_limited"})
        detail = f"You asked {ASK_PER_MINUTE} questions in the last minute - please wait a moment before asking again."
        if reply_json:
            return JSONResponse({"error": "Too many questions", "detail": detail, "code": "rate_limited",
                                 "retry_after": int(ASK_WINDOW)}, status_code=429,
                                headers={"Retry-After": str(int(ASK_WINDOW))})
        return await _render(request, db, await insights.load_digest(db), error=detail, status_code=429,
                             question=question)

    digest = await insights.load_digest(db)
    items = insights.urgent(digest)
    system, prompt = insights.build_prompt(digest, items, question)
    await db.commit()  # the AI may take a while: the request's connection goes back to the pool meanwhile
    started = time.perf_counter()
    source, note, provider = "ai", None, None
    try:
        result = await ai.complete(feature=insights.FEATURE, system=system, prompt=prompt, schema=insights.AIAnswer,
                                   max_tokens=MAX_TOKENS, temperature=0.2, timeout=AI_TIMEOUT, user_text=question)
        answer = insights.clean_answer(result.data)
        provider = _provider_label(result.provider)
        outcome: dict[str, Any] = {"outcome": "answered", "provider": result.provider,
                                   "latency_ms": result.latency_ms}
        if not (answer["overview"] or answer["fix_first"]):  # nothing usable came back: the automatic overview
            answer, source, note = insights.fallback_answer(digest, items), "fallback", UNAVAILABLE_NOTE
            outcome["outcome"] = "fallback"
    except AIRejected as exc:
        if exc.code != "busy":  # quota, cooldown, too many at once, duplicate, spam: the admin is told why
            await _audit(db, admin, request, {**asked, "outcome": "refused", "code": exc.code})
            if reply_json:
                raise
            return await _render(request, db, digest, error=exc.message, status_code=exc.status, question=question)
        answer, source, note = insights.fallback_answer(digest, items), "fallback", BUSY_NOTE
        outcome = {"outcome": "busy", "code": exc.code}
    except AICancelled:  # the admin left: nobody reads the answer - still on record
        await _audit(db, admin, request, {**asked, "outcome": "cancelled"})
        raise
    except AIUnavailableError as exc:
        answer, source, note = insights.fallback_answer(digest, items), "fallback", UNAVAILABLE_NOTE
        outcome = {"outcome": "fallback", "error": str(exc)[:200]}
    await flush_ai_call_logs(db)  # the AI page's call log shows this call right away
    await _audit(db, admin, request, {**asked, **outcome, "urgent": len(items), "items": len(answer["fix_first"]),
                                      "total_ms": int((time.perf_counter() - started) * 1000)})
    payload = {"ok": True, "source": source, "provider": provider, "question": question, **answer, "note": note,
               "urgent": len(items), "generated_at": digest.get("generated_at"), "disclaimer": AI_DISCLAIMER}
    if reply_json:
        return JSONResponse(payload)
    return await _render(request, db, digest, answer=payload, question=question)


# ===================================================================================== the platform overview's teaser
def insights_teaser() -> dict[str, Any] | None:
    """The urgent list of the digest this process computed last (no query: ``digest_cache.peek``), for the teaser on
    /superadmin (superadmin/insights_teaser.html) - None when there is none younger than TEASER_MAX_AGE."""
    try:
        digest = insights.digest_cache.peek(insights.DIGEST_KEY)
        at = _when(digest.get("generated_at")) if isinstance(digest, dict) else None
        if at is None:
            return None
        age = (utcnow() - at).total_seconds()
        if age > TEASER_MAX_AGE:
            return None
        items = insights.urgent(digest)
    except Exception:  # noqa: BLE001 - a teaser must never break the platform overview
        log.exception("site overview teaser unavailable")
        return None
    return {"items": items[:3], "total": len(items), "minutes": max(0, int(age // 60)), "tones": SEVERITY_TONES}


templates.env.globals["insights_teaser"] = insights_teaser
