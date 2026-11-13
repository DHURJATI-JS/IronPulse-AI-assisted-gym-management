"""WP-E2 AI assistant chat API, calendar, tasks and reminders.

Routes: ``GET /calendar`` · ``GET /tasks`` (open tasks, ``?page=``; ``?day=YYYY-MM-DD``: everything on one day - the
calendar's "+N more" list) · ``POST /tasks`` · ``POST /tasks/{id}/toggle`` · ``POST /tasks/{id}/delete`` ·
``POST /api/assistant/chat`` · ``GET /api/assistant/history`` · ``POST /api/memory/confirm`` / ``POST /api/memory/undo``
(``{"token"}``: the "Remember this?" offers of the assistant and the live coach, app/ai/memory.py). Importing
``app.services.tasks`` registers the ``task_reminders`` periodic job.

Load safety: the month grid reads at most ``CALENDAR_DAY_MAX`` tasks a day, the task lists are paginated
(``TASKS_PER_PAGE``) with true totals, and one person holds at most ``open_task_limit()`` open tasks - the form says so
(409, the page re-rendered) and so does the assistant, in words (never a 500).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.ai import assistant as assistant_ai
from app.ai import memory as memory_ai
from app.core.deps import DB, AuthUser, CustomerUser, wants_json
from app.core.pagination import parse_page
from app.core.templating import redirect, render
from app.core.utils import is_safe_redirect, to_local, utcnow
from app.services import tasks as task_service  # noqa: F401  (registers the task_reminders periodic job)

router = APIRouter()


def _back(value: Any, default: str) -> str:
    url = str(value or "").strip()
    if is_safe_redirect(url) and url.split("?", 1)[0] in ("/calendar", "/tasks"):
        return url[:300]
    return default


def _day(value: Any) -> date | None:
    """``?day=YYYY-MM-DD`` of the one-day list; junk (or nothing) -> None, the open-tasks list."""
    try:
        return task_service.parse_date(str(value)[:40]) if value else None
    except ValueError:
        return None


# ------------------------------------------------------------------ calendar
@router.get("/calendar")
async def calendar_page(request: Request, db: DB, user: AuthUser, month: str | None = None, day: str | None = None):
    view = await task_service.build_month(db, user, month, day)
    return render(request, "calendar/month.html", {"view": view, "kinds": list(task_service.USER_KINDS)})


# ------------------------------------------------------------------ tasks
async def _tasks_page(request: Request, db: DB, user: AuthUser, *, values: dict[str, str] | None = None,
                      errors: dict[str, str] | None = None, limit_error: str | None = None, status_code: int = 200):
    """One page of the open tasks (stats + page + recently completed: 3 statements), or with ``?day=`` one page of
    everything on that day (1 statement) - a constant number of queries whatever the person holds."""
    tz = user.timezone or "UTC"
    now = utcnow()
    today = to_local(now, tz).date()
    pr = parse_page(request, default_per_page=task_service.TASKS_PER_PAGE, max_per_page=task_service.TASKS_MAX_PER_PAGE)
    day = _day(request.query_params.get("day"))
    ctx: dict[str, Any] = {
        "tz": tz,
        "when": lambda t: task_service.when_label(t, tz, today=today),
        "values": values or {},
        "errors": errors or {},
        "limit_error": limit_error,
        "kinds": list(task_service.USER_KINDS),
        "open_limit": task_service.open_task_limit(),
        "day": day,
    }
    if day is not None:
        page = await task_service.day_tasks_page(db, user.id, day, tz, page=pr.page, per_page=pr.per_page)
        ctx.update(page=page, groups=[], completed=[], stats=None, back=f"/tasks?day={day.isoformat()}",
                   calendar_link=f"/calendar?month={day:%Y-%m}&day={day.isoformat()}")
    else:
        stats = await task_service.open_task_stats(db, user.id, tz, now=now)
        page = await task_service.open_tasks_page(db, user.id, page=pr.page, per_page=pr.per_page)
        completed = await task_service.recently_completed(db, user.id)
        ctx.update(page=page, groups=task_service.group_tasks(page.items, tz, now=now), completed=completed,
                   stats=stats, open_count=stats.total, today_count=stats.today, overdue_count=stats.overdue,
                   back="/tasks" if page.page == 1 else f"/tasks?page={page.page}")
    return render(request, "calendar/tasks.html", ctx, status_code=status_code)


@router.get("/tasks")
async def tasks_page(request: Request, db: DB, user: AuthUser):
    return await _tasks_page(request, db, user)


@router.post("/tasks")
async def create_task(request: Request, db: DB, user: AuthUser):
    form = await request.form()
    data, errors, raw = task_service.validate_task_form(form, user.timezone)
    if data is None:
        return await _tasks_page(request, db, user, values=raw, errors=errors, status_code=400)
    try:
        task = await task_service.create_task(db, user, data)
    except task_service.TaskLimitError as exc:  # nothing was added: the form comes back with what they typed
        return await _tasks_page(request, db, user, values=raw, limit_error=exc.message, status_code=409)
    await db.commit()
    back = _back(form.get("next"), "/tasks")
    if task.remind_at is not None:
        msg = f"Saved “{task.title}”. We'll remind you {task_service.when_label_for(task.remind_at, user.timezone)}."
    else:
        msg = f"Added “{task.title}”."
    return redirect(back, msg, "success")


@router.post("/tasks/{task_id}/toggle")
async def toggle_task(request: Request, task_id: int, db: DB, user: AuthUser):
    try:
        task = await task_service.toggle_task(db, user, task_id)
    except task_service.TaskLimitError as exc:  # reopening one more would pass the open-task cap: nothing changed
        if wants_json(request):
            return JSONResponse({"error": "Too many open tasks", "detail": exc.message, "code": "task_limit"},
                                status_code=409)
        form = await request.form()
        return redirect(_back(form.get("next"), "/tasks"), exc.message, "error")
    await db.commit()
    done = task.completed_at is not None
    if wants_json(request):
        return JSONResponse({"ok": True, "id": task.id, "completed": done})
    form = await request.form()
    return redirect(_back(form.get("next"), "/tasks"), f"“{task.title}” {'completed' if done else 'reopened'}.", "success")


@router.post("/tasks/{task_id}/delete")
async def delete_task(request: Request, task_id: int, db: DB, user: AuthUser):
    await task_service.delete_task(db, user, task_id)
    await db.commit()
    if wants_json(request):
        return JSONResponse({"ok": True, "id": task_id})
    form = await request.form()
    return redirect(_back(form.get("next"), "/tasks"), "Task deleted.", "success")


# ------------------------------------------------------------------ assistant API
class ChatIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: str = Field(min_length=1, max_length=assistant_ai.MAX_MESSAGE)


def _task_limit_answer(exc: task_service.TaskLimitError) -> dict[str, Any]:
    """The assistant's answer when one of its task actions met the open-task cap: words, never a 500 (the widget
    renders it like any answer - a reply bubble and a failed action chip linking to the task list)."""
    from app.ai.client import ai

    return {"reply": f"I couldn't add that. {exc.message}", "provider": None,
            "ai_available": bool(ai.status().get("available")),
            "actions": [{"type": "create_task", "ok": False, "detail": exc.message, "link": "/tasks"}]}


@router.post("/api/assistant/chat")
async def assistant_chat(payload: ChatIn, db: DB, user: AuthUser):
    """One assistant message. An AI guard / queue refusal (``AIRejected``) and an abandoned request (``AICancelled``)
    propagate to app/core/errors.py - the contract's refusal JSON (429/409/422 + Retry-After) or 503 'cancelled'
    (docs/ai-guard-and-queue.md section 7); nothing was executed or stored then. ``chat`` gives the request's DB
    connection back while the AI works and writes the actions and history afterwards; they are committed here.

    A task action that meets the open-task cap (``TaskLimitError``) is answered in words; nothing of that message is
    kept - it is not committed, so the session's close rolls back the actions it had already run."""
    message = assistant_ai.clean_message(payload.message)
    if not message:
        return JSONResponse({"error": "Invalid input", "detail": "Type a message first."}, status_code=422)
    try:
        result = await assistant_ai.chat(db, user, message)
    except task_service.TaskLimitError as exc:
        return _task_limit_answer(exc)
    queue_ac = bool(result.pop("_queue_ac", False))
    await db.commit()
    if queue_ac:  # a "forget ..." changed the conditions of a plan with AI: the AC wording follows in the background
        await memory_ai.after_commit(db, user, {"queue_ac": True})
    return result


@router.get("/api/assistant/history")
async def assistant_history(db: DB, user: AuthUser):
    return {"items": await assistant_ai.history(db, user)}


# ------------------------------------------------------------------ memory: "remember this?" / undo
class TicketIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    token: str = Field(min_length=10, max_length=memory_ai.TOKEN_MAX_CHARS)


def _memory_refusal(exc: memory_ai.MemoryRefused) -> JSONResponse:
    return JSONResponse({"ok": False, "code": exc.code, "detail": exc.message}, status_code=exc.status,
                        headers={"Cache-Control": "no-store"})


@router.post("/api/memory/confirm")
async def memory_confirm(payload: TicketIn, db: DB, user: CustomerUser):
    """The member clicked "Remember" on an offer of the assistant or the live coach (a signed, member-bound ticket):
    saved behind the cap locks - 409 ``full`` at a cap (the friendly words in ``detail``), 400 ``expired``/``invalid``.
    A second click answers ``known`` (nothing saved twice). The answer carries an ``undo`` ticket."""
    try:
        result = await memory_ai.confirm(db, user, payload.token)
    except memory_ai.MemoryRefused as exc:
        await db.rollback()
        return _memory_refusal(exc)
    await db.commit()
    await memory_ai.after_commit(db, user, result)
    return JSONResponse(memory_ai.public(result), headers={"Cache-Control": "no-store"})


@router.post("/api/memory/undo")
async def memory_undo(payload: TicketIn, db: DB, user: CustomerUser):
    """Undo a "Remember" or a "forget ..." (its ticket). Idempotent; 409 ``full`` when restoring would pass a cap."""
    try:
        result = await memory_ai.undo(db, user, payload.token)
    except memory_ai.MemoryRefused as exc:
        await db.rollback()
        return _memory_refusal(exc)
    await db.commit()
    await memory_ai.after_commit(db, user, result)
    return JSONResponse(memory_ai.public(result), headers={"Cache-Control": "no-store"})
