"""WP-F live chat (customers <-> staff/admin) + misconduct reports.

Pages:   GET /chat?q=&status=&scope=&older= · GET/POST /chat/new · GET /chat/{id} ·
         POST /chat/{id}/messages (no-JS composer fallback) · POST /chat/{id}/close · POST /chat/{id}/reopen
API:     POST /api/chat/{id}/messages {"body"} · GET /api/chat/{id}/messages?after=|before=&limit= ·
         POST /api/chat/{id}/read · GET /api/chat/threads?q=&status=&scope=&older= ("Load more conversations")
WS:      {"type": "chat.typing", "thread_id"} -> {"type": "chat.typing", "thread_id", "name"} to the other side
Reports: GET/POST /reports/new?employee= · GET /reports?page= · GET /owner/reports · GET/POST /owner/reports/{id}
Privacy: POST /account/health-sharing {"share": "1"|"0"} (a member shares health details with their trainers)

The conversation list is a keyset window of ``THREAD_PAGE_SIZE`` (``older`` cursor); messages page by id.

Trainer safety (app/services/trainer_safety.py): staff / owners see the member's safety panel (details only for the
gym's admin team and the member's trainers), a safety badge in the list, and a send-time warning when their message
suggests something the member's conditions rule out (``{"confirm_safety": true}`` / the form's "Send anyway" sends it,
audited). A member's red-flag message gets emergency guidance at once and alerts the gym - never refused (not by a cap,
a closed conversation or a staff member who is away). Staff see the conversations of their ACTIVE employments only.

Caps (approved limits): app/services/chat.py ``CHAT_CAPS`` (members, staff / owners, per conversation) and the misconduct
report caps below (``REPORT_CAPS``), all race-safe.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased, joinedload

from app.core import caps
from app.core.deps import DB, AuthUser, CustomerUser, Forbidden, NotFound, OwnerUser
from app.core.pagination import paginate, parse_page
from app.core.templating import media_url, redirect, render
from app.core.utils import clamp, local_today, utcnow
from app.models import ChatThread, CustomerProfile, Employee, Gym, MisconductReport, Subscription, User
from app.models.enums import (
    EmploymentStatus,
    NotificationCategory,
    ReportStatus,
    Role,
    Severity,
    SubscriptionStatus,
    ThreadStatus,
)
from app.routers.core import ws_handler
from app.services import chat as chat_service
from app.services import trainer_safety
from app.services.audit import audit
from app.services.chat import ChatError, ChatLimitError, ChatPermissionError
from app.services.notify import notify
from app.services.realtime import hub

log = logging.getLogger("ironpulse.chat")
router = APIRouter()

CHAT_ROLES = (Role.CUSTOMER, Role.STAFF, Role.OWNER)


# ====================================================================== helpers
def _require_chat_user(user: User) -> None:
    if user.role not in CHAT_ROLES:
        raise Forbidden("Live chat is for members, staff and gym owners.")


def _int(value: Any) -> int | None:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if 0 < n < 2**62 else None


def _one_line(value: Any, cap: int) -> str:
    return " ".join(str(value or "").split())[:cap]


def _like(q: str) -> str:
    return "%" + re.sub(r"([\\%_])", r"\\\1", q) + "%"


async def _thread_or_404(db: DB, user: User, thread_id: int) -> ChatThread:
    _require_chat_user(user)
    if not 0 < thread_id <= chat_service.MAX_ID:  # beyond BIGINT: no such conversation (not a driver error)
        raise NotFound("Conversation not found.")
    thread = await chat_service.load_thread(db, user, thread_id)
    if thread is None:
        raise NotFound("Conversation not found.")
    return thread


def _list_filters(user: User, q: str, status: str, scope: str, older: str = "") -> dict[str, str]:
    return {
        "q": _one_line(q, 80),
        "status": status if status in ("open", "closed") else "",
        "scope": scope if (scope in ("team", "staff") and user.role == Role.OWNER) else "",
        "older": older if chat_service.parse_thread_cursor(older) else "",
    }


def _qs(filters: dict[str, str], **over: str | None) -> str:
    params = {**filters, **over}
    return urlencode({k: v for k, v in params.items() if v})


async def _thread_window(db: DB, user: User, filters: dict[str, str]) -> chat_service.ThreadPage:
    return await chat_service.list_threads_page(db, user, q=filters["q"], status=filters["status"],
                                                scope=filters["scope"], older=filters["older"] or None)


def _more_links(window: chat_service.ThreadPage, filters: dict[str, str]) -> dict[str, str | None]:
    if not (window.has_more and window.next_cursor):
        return {"more_url": None, "more_api": None}
    qs = _qs(filters, older=window.next_cursor)
    return {"more_url": "/chat?" + qs, "more_api": "/api/chat/threads?" + qs}


def _card_json(card: dict[str, Any], qs: str) -> dict[str, Any]:
    """A sidebar item for 60-chat.js (same fields as chat/_macros.html:thread_item renders)."""
    last_at = card.get("last_at")
    return {
        "id": card["id"], "href": f"/chat/{card['id']}" + (f"?{qs}" if qs else ""), "name": card["name"],
        "photo_url": media_url(card.get("photo")), "subtitle": card["subtitle"], "preview": card["preview"],
        "when": card["when"], "last_at": last_at.isoformat() if last_at else None, "unread": card["unread"],
        "closed": card["closed"], "deleted": card["deleted"], "team": card["team"], "safety": bool(card.get("safety")),
    }


async def _cards(db: DB, user: User, threads: list[ChatThread]) -> list[dict[str, Any]]:
    """Sidebar cards + the safety badge (members with active safety tags - only for viewers who may know it)."""
    cards = [chat_service.thread_card(t, user) for t in threads]
    badges = await trainer_safety.badge_ids(db, user, [t.customer for t in threads])
    for card, t in zip(cards, threads, strict=True):
        card["safety"] = t.customer_id in badges
    return cards


async def _chat_page(request: Request, db: DB, user: User, *, thread: ChatThread | None, filters: dict[str, str],
                     before: int | None = None, draft: str = "", warning: trainer_safety.SendCheck | None = None,
                     status_code: int = 200) -> Any:
    window = await _thread_window(db, user, filters)
    threads = list(window.items)
    if thread is not None and all(t.id != thread.id for t in threads):
        threads.insert(0, thread)
    ctx: dict[str, Any] = {
        "threads": await _cards(db, user, threads),
        "filters": filters,
        "filter_qs": _qs(filters),
        "newest_url": ("/chat?" + _qs(filters, older=None)).rstrip("?") if filters["older"] else None,
        **_more_links(window, filters),
        "thread": None,
        "draft": draft,
        "max_body": chat_service.MAX_BODY,
        "safety": None,
        "safety_warning": warning,
    }
    if thread is not None:
        role = chat_service.viewer_role(user, thread) or "owner"
        messages, has_more = await chat_service.list_messages(db, thread.id, before=before)
        report_employee = None
        if role == "customer" and thread.employee is not None and not chat_service.employee_gone(thread.employee):
            report_employee = thread.employee.id
        ctx.update({
            "thread": thread,
            "role": role,
            "other": chat_service.counterpart(thread, role),
            "rows": chat_service.message_rows(messages, thread, user),
            "has_more": has_more,
            "first_id": messages[0].id if messages else None,
            "last_id": messages[-1].id if messages else 0,
            "read_only": chat_service.read_only_state(thread, user.timezone),
            "can_manage": role in ("owner", "staff"),
            "report_employee": report_employee,
            "older_page": before is not None,
        })
        if role in ("owner", "staff"):
            ctx["safety"] = await trainer_safety.panel_for(db, user, thread.customer, in_conversation=True)
    return render(request, "chat/index.html", ctx, status_code=status_code)


# ====================================================================== pages
@router.get("/chat")
async def chat_index(request: Request, db: DB, user: AuthUser, q: str = "", status: str = "", scope: str = "",
                     older: str = "") -> Any:
    _require_chat_user(user)
    return await _chat_page(request, db, user, thread=None, filters=_list_filters(user, q, status, scope, older))


def _preselect(targets: list[chat_service.ChatTarget], gym: str, to: str) -> str:
    gym_id = _int(gym)
    target = next((t for t in targets if t.gym.id == gym_id), None) or (targets[0] if len(targets) == 1 else None)
    if target is None:
        return ""
    emp_id = _int(to)
    if emp_id and any(e.id == emp_id for e in target.employees):
        return f"{target.gym.id}:{emp_id}"
    return f"{target.gym.id}:team"


def _new_ctx(targets: list[chat_service.ChatTarget], selected: str, *, body: str = "",
             errors: dict[str, str] | None = None) -> dict[str, Any]:
    return {"targets": targets, "selected": selected, "body": body, "errors": errors or {},
            "max_body": chat_service.MAX_BODY}


@router.get("/chat/new")
async def chat_new(request: Request, db: DB, user: CustomerUser, gym: str = "", to: str = "") -> Any:
    targets = await chat_service.chat_targets(db, user)
    return render(request, "chat/new.html", _new_ctx(targets, _preselect(targets, gym, to)))


@router.post("/chat/new")
async def chat_new_submit(request: Request, db: DB, user: CustomerUser) -> Any:
    form = await request.form()
    raw_target = str(form.get("target") or "").strip()
    body = chat_service.clean_body(form.get("body"))
    targets = await chat_service.chat_targets(db, user)
    errors: dict[str, str] = {}
    gym_part, _, to_part = raw_target.partition(":")
    target = next((t for t in targets if t.gym.id == _int(gym_part)), None)
    red = trainer_safety.detect_red_flag(body) if body else None
    urgent = red is not None  # a safety message is never refused by a cap - nor by a staff member who is away
    employee_id: int | None = None
    if target is None:
        errors["target"] = "Choose who you'd like to message."
    elif to_part != "team":
        employee_id = _int(to_part)
        if employee_id is None or all(e.id != employee_id for e in target.employees):
            if urgent:  # the gym team gets it instead (the gym admin sees and is alerted about every conversation)
                employee_id = None
            else:
                errors["target"] = "That person isn't available to message - choose someone else."
    if not body:
        errors["body"] = "Write your first message."
    elif len(body) > chat_service.MAX_BODY:
        errors["body"] = f"Messages can be at most {chat_service.MAX_BODY:,} characters."
    status = 400
    if not errors and target is not None:
        try:
            # SAVEPOINT: a failure undoes the new thread without expiring request.state.user (a full rollback would)
            async with db.begin_nested():
                thread = await chat_service.get_or_create_thread(db, gym_id=target.gym.id, customer_id=user.id,
                                                                 employee_id=employee_id, urgent=urgent)
                msg = await chat_service.post_message(db, thread=thread, sender=user, body=body, sender_role="customer")
                thread_id = thread.id
        except ChatLimitError as exc:
            errors["body"], status = str(exc), 429
        except (ChatError, ChatPermissionError) as exc:
            errors["body"] = str(exc)
        else:
            await db.commit()
            alert = getattr(msg, "safety_alert", None)
            if alert is not None:
                return redirect(f"/chat/{thread_id}#latest", alert.guidance, "error")
            return redirect(f"/chat/{thread_id}", "Message sent.", "success")
    if red is not None:  # not sent (no gym to send it to): the emergency guidance still comes first, never only an error
        guidance, _number, _tel = trainer_safety.guidance_text(red.flag, user.country_code)
        reason = errors.get("body")
        errors["body"] = (guidance.replace(" Your trainer and the gym team have been alerted.", "")
                          + (f" (Not sent: {reason})" if reason else ""))
    return render(request, "chat/new.html", _new_ctx(targets, raw_target, body=body, errors=errors),
                  status_code=status)


@router.get("/chat/{thread_id}")
async def chat_thread(request: Request, db: DB, user: AuthUser, thread_id: int, before: int | None = None,
                      q: str = "", status: str = "", scope: str = "", older: str = "") -> Any:
    thread = await _thread_or_404(db, user, thread_id)
    if await chat_service.mark_read(db, thread, user):
        await db.commit()
    return await _chat_page(request, db, user, thread=thread, filters=_list_filters(user, q, status, scope, older),
                            before=before if before and before > 0 else None)


@router.post("/chat/{thread_id}/messages")
async def chat_post_form(request: Request, db: DB, user: AuthUser, thread_id: int) -> Any:
    """No-JS fallback of the composer (the chat-thread component uses the JSON API). A staff / owner message that the
    safety check flags comes back as the conversation page with the warning (Edit / Send anyway) - nothing stored."""
    thread = await _thread_or_404(db, user, thread_id)
    form = await request.form()
    role = chat_service.viewer_role(user, thread) or "owner"
    raw = form.get("body")
    body = raw if isinstance(raw, str) else ""
    check = None
    if role in ("owner", "staff") and chat_service.read_only_state(thread) is None:
        check = await trainer_safety.check_staff_message(db, thread, user, body)
        if check is not None and str(form.get("confirm_safety") or "") != "1":
            return await _chat_page(request, db, user, thread=thread, filters=_list_filters(user, "", "", "", ""),
                                    draft=chat_service.clean_body(body)[:chat_service.MAX_BODY], warning=check,
                                    status_code=409)
    try:
        async with db.begin_nested():
            msg = await chat_service.post_message(db, thread=thread, sender=user, body=body, sender_role=role)
    except (ChatError, ChatPermissionError) as exc:
        return redirect(f"/chat/{thread_id}", str(exc), "error")
    if check is not None:
        await audit(db, user, "chat.safety_override", "chat_thread", thread.id, gym_id=thread.gym_id,
                    detail=trainer_safety.override_detail(check, msg.id), request=request)
    await db.commit()
    alert = getattr(msg, "safety_alert", None)
    if alert is not None:
        return redirect(f"/chat/{thread_id}#latest", alert.guidance, "error")
    return redirect(f"/chat/{thread_id}#latest")


async def _set_status(request: Request, db: DB, user: User, thread_id: int, status: ThreadStatus) -> Any:
    thread = await _thread_or_404(db, user, thread_id)
    if chat_service.viewer_role(user, thread) not in ("owner", "staff"):
        raise Forbidden("Only the gym team can close or reopen conversations.")
    if thread.status == status:
        return redirect(f"/chat/{thread_id}", "This conversation is already " + status.value + ".", "info")
    try:
        async with db.begin_nested():
            await chat_service.set_status(db, thread, user, status)
    except (ChatError, ChatPermissionError) as exc:
        return redirect(f"/chat/{thread_id}", str(exc), "error")
    action = "chat.close" if status == ThreadStatus.CLOSED else "chat.reopen"
    await audit(db, user, action, "chat_thread", thread.id, gym_id=thread.gym_id, request=request)
    await db.commit()
    msg = ("Conversation closed. The member can reopen it by writing again." if status == ThreadStatus.CLOSED
           else "Conversation reopened.")
    return redirect(f"/chat/{thread.id}", msg, "success")


@router.post("/chat/{thread_id}/close")
async def chat_close(request: Request, db: DB, user: AuthUser, thread_id: int) -> Any:
    return await _set_status(request, db, user, thread_id, ThreadStatus.CLOSED)


@router.post("/chat/{thread_id}/reopen")
async def chat_reopen(request: Request, db: DB, user: AuthUser, thread_id: int) -> Any:
    return await _set_status(request, db, user, thread_id, ThreadStatus.OPEN)


# ====================================================================== JSON API
def _api_error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": "Chat", "detail": message}, status_code=status)


@router.get("/api/chat/threads")
async def api_threads(db: DB, user: AuthUser, q: str = "", status: str = "", scope: str = "", older: str = "") -> Any:
    """One window of the conversation list ("Load more conversations"): items + the next window's links."""
    _require_chat_user(user)
    filters = _list_filters(user, q, status, scope, older)
    window = await _thread_window(db, user, filters)
    list_qs = _qs(filters, older=None)
    return {
        "items": [_card_json(card, list_qs) for card in await _cards(db, user, list(window.items))],
        "has_more": window.has_more, "next_cursor": window.next_cursor, **_more_links(window, filters),
    }


@router.post("/api/chat/{thread_id}/messages")
async def api_post_message(request: Request, db: DB, user: AuthUser, thread_id: int) -> Any:
    thread = await _thread_or_404(db, user, thread_id)
    try:
        data = await request.json()
    except Exception:
        return _api_error(400, 'Send JSON like {"body": "..."}.')
    body = data.get("body") if isinstance(data, dict) else None
    if not isinstance(body, str):
        return _api_error(400, "Type a message first.")
    role = chat_service.viewer_role(user, thread) or "owner"
    read_only = chat_service.read_only_state(thread) is not None
    check = None
    if role in ("owner", "staff") and not read_only:
        check = await trainer_safety.check_staff_message(db, thread, user, body)
        if check is not None and data.get("confirm_safety") is not True:
            return JSONResponse({"error": "Safety check", "detail": check.message, "code": "safety_check",
                                 "warning": check.payload()}, status_code=409)
    try:
        async with db.begin_nested():
            msg = await chat_service.post_message(db, thread=thread, sender=user, body=body, sender_role=role)
    except ChatPermissionError as exc:
        return _api_error(403, str(exc))
    except ChatLimitError as exc:
        return JSONResponse(exc.payload(), status_code=exc.status, headers={"Retry-After": str(exc.retry_after)})
    except ChatError as exc:
        return _api_error(409 if read_only else 400, str(exc))
    if check is not None:
        await audit(db, user, "chat.safety_override", "chat_thread", thread.id, gym_id=thread.gym_id,
                    detail=trainer_safety.override_detail(check, msg.id), request=request)
    payload = chat_service.message_payload(msg, thread)
    await db.commit()
    out: dict[str, Any] = {"message": payload, "thread_id": thread_id}
    alert = getattr(msg, "safety_alert", None)
    if alert is not None:
        out["safety"] = alert.payload()
    return JSONResponse(out, status_code=201)


@router.get("/api/chat/{thread_id}/messages")
async def api_messages(db: DB, user: AuthUser, thread_id: int, after: int | None = None, before: int | None = None,
                       limit: int = chat_service.PAGE_SIZE) -> Any:
    thread = await _thread_or_404(db, user, thread_id)
    msgs, has_more = await chat_service.list_messages(db, thread.id, after=after, before=before, limit=limit)
    ro = chat_service.read_only_state(thread, user.timezone)
    return {
        "items": [chat_service.message_payload(m, thread) for m in msgs],
        "has_more": has_more,
        "status": thread.status.value,
        "read_only": ro.message if ro else None,
    }


@router.post("/api/chat/{thread_id}/read")
async def api_mark_read(db: DB, user: AuthUser, thread_id: int) -> Any:
    thread = await _thread_or_404(db, user, thread_id)
    changed = await chat_service.mark_read(db, thread, user)
    if changed:
        await db.commit()
    return {"ok": True, "changed": changed}


# ====================================================================== privacy: health sharing with trainers
@router.post("/account/health-sharing")
async def health_sharing(request: Request, db: DB, user: CustomerUser) -> Any:
    """The member's choice "Share my health and safety information with my gym's trainers" (default ON). OFF: staff see
    only "The member chose not to share health details - ask before training"; the gym's admin team still sees it."""
    form = await request.form()
    share = "1" in [str(v) for v in form.getlist("share")]  # the checkbox (1) after its hidden default (0)
    profile = await db.get(CustomerProfile, user.id)
    if profile is None:
        profile = CustomerProfile(user_id=user.id, activity_suspended=False)
        db.add(profile)
    if profile.share_health_with_trainers is not None and bool(profile.share_health_with_trainers) == share:
        await db.commit()
        return redirect("/account#health-sharing", "Nothing changed.", "info")
    profile.share_health_with_trainers = share
    await audit(db, user, "account.health_sharing", "user", user.id, detail={"share": share}, request=request)
    title = "Health details shared with your trainers" if share else "Health details hidden from your trainers"
    body = ("Your trainers can now see your health and safety information (conditions, allergies, medication and "
            "injuries) so they can keep your training safe." if share else
            "Your trainers will no longer see your health and safety information - they'll ask you before training. "
            "Your gym's admin team still sees it for your safety. You can turn sharing back on in your account.")
    await notify(db, user, title, body, category=NotificationCategory.ACCOUNT, severity=Severity.INFO,
                 link="/account#health-sharing")
    await db.commit()
    return redirect("/account#health-sharing", "Saved - " + ("your trainers can see your health details."
                                                             if share else "your trainers will ask you before training."),
                    "success")


# ====================================================================== WebSocket: typing indicator
TYPING_INTERVAL = 2.0
_typing_last: dict[int, float] = {}


@ws_handler("chat.typing")
async def ws_typing(user: Any, message: dict[str, Any]) -> None:
    """Relay 'X is typing…' to the other participants (participants only, at most one event per 2 s per user)."""
    thread_id = _int(message.get("thread_id")) if isinstance(message, dict) else None
    if thread_id is None or user is None or getattr(user, "role", None) not in CHAT_ROLES:
        return
    now = time.monotonic()
    if now - _typing_last.get(user.id, -TYPING_INTERVAL) < TYPING_INTERVAL:
        return
    from app.db import get_sessionmaker

    async with get_sessionmaker()() as db:
        thread = await chat_service.load_thread(db, user, thread_id)
        if thread is None or chat_service.read_only_state(thread) is not None:
            return
        role = chat_service.viewer_role(user, thread)
        if role is None:
            return
        first = (user.full_name or "Someone").split(" ")[0]
        if role == "owner":
            name = f"{first} ({thread.gym.name})"
        elif role == "staff" and thread.employee is not None:
            name = thread.employee.display_name.split(" ")[0]
        else:
            name = first
        targets = chat_service.typing_targets(thread, user.id)
    _typing_last[user.id] = now
    if len(_typing_last) > 10000:
        _typing_last.clear()
    if targets:
        await hub.publish(targets, {"type": "chat.typing", "thread_id": thread_id, "name": name, "user_id": user.id})


# ====================================================================== misconduct reports (customer)
REPORT_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("rude", "Rude or disrespectful behaviour"),
    ("harassment", "Harassment"),
    ("unsafe_training", "Unsafe training"),
    ("negligence", "Negligence"),
    ("discrimination", "Discrimination"),
    ("other", "Something else"),
)
CATEGORY_LABELS = dict(REPORT_CATEGORIES)
REPORTABLE_SUB_STATUSES = (SubscriptionStatus.ACTIVE, SubscriptionStatus.EXPIRED, SubscriptionStatus.CANCELLED,
                           SubscriptionStatus.SUSPENDED)
REPORTABLE_EMP_STATUSES = (EmploymentStatus.ACTIVE, EmploymentStatus.SUSPENDED, EmploymentStatus.TERMINATED)
APOLOGY = re.compile(r"\b(sorry|apolog\w*|regret\w*|maaf\w*|kshama)\b", re.IGNORECASE)
DESC_MIN, DESC_MAX, RESPONSE_MIN, RESPONSE_MAX = 20, 4000, 10, 4000
# load safety (approved limits): a member's misconduct reports (race-safe ``caps.enforce``; ``CAPS_OVERRIDES`` tunes
# them) on top of "one open report per staff member" - every check runs under the member's report lock, held to COMMIT
OPEN_REPORTS_CAP = "open_reports_per_member"
REPORTS_DAY_CAP = "reports_per_member_day"
REPORT_LOCK = "misconduct_report"
REPORT_CAPS = (
    caps.Cap(OPEN_REPORTS_CAP, 10, "reports waiting for the gym's reply", "per member",
             "You have {limit} reports waiting for a reply. The gyms will answer those first - you can send another "
             "once one has been answered."),
    caps.Cap(REPORTS_DAY_CAP, 10, "misconduct reports", "per member per day",
             "You've sent {limit} reports in the last 24 hours. Please try again tomorrow.", window_hours=caps.DAY),
)
for _cap in REPORT_CAPS:
    caps.CAPS.setdefault(_cap.name, _cap)


def category_label(value: str | None) -> str:
    v = (value or "").strip()
    return CATEGORY_LABELS.get(v) or (v.replace("_", " ").capitalize() if v else "Other")


async def _reportable_employees(db: DB, customer: User) -> list[Employee]:
    """Staff of gyms where the customer has (or had) a membership."""
    gym_ids = select(Subscription.gym_id).where(Subscription.customer_id == customer.id,
                                                Subscription.status.in_(REPORTABLE_SUB_STATUSES))
    stmt = (
        select(Employee).options(joinedload(Employee.gym))
        .where(Employee.gym_id.in_(gym_ids), Employee.status.in_(REPORTABLE_EMP_STATUSES), Employee.deleted_at.is_(None))
        .order_by(Employee.gym_id, Employee.full_name)
    )
    return list((await db.execute(stmt)).unique().scalars().all())


def _report_form_ctx(employees: list[Employee], values: dict[str, Any], errors: dict[str, str]) -> dict[str, Any]:
    groups: dict[int, dict[str, Any]] = {}
    for e in employees:
        groups.setdefault(e.gym_id, {"gym": e.gym, "employees": []})["employees"].append(e)
    today = date.today()
    return {"groups": list(groups.values()), "employees": employees, "values": values, "errors": errors,
            "categories": REPORT_CATEGORIES, "today": today.isoformat(),
            "oldest": (today - timedelta(days=365)).isoformat(), "desc_min": DESC_MIN, "desc_max": DESC_MAX}


@router.get("/reports/new")
async def report_new(request: Request, db: DB, user: CustomerUser, employee: str = "") -> Any:
    employees = await _reportable_employees(db, user)
    emp_id = _int(employee)
    chosen = str(emp_id) if emp_id and any(e.id == emp_id for e in employees) else ""
    values = {"employee_id": chosen, "category": "", "incident_date": local_today(user.timezone).isoformat(),
              "description": ""}
    return render(request, "reports/new.html", _report_form_ctx(employees, values, {}))


@router.post("/reports/new")
async def report_submit(request: Request, db: DB, user: CustomerUser) -> Any:
    form = await request.form()
    employees = await _reportable_employees(db, user)
    values = {
        "employee_id": str(form.get("employee_id") or "").strip()[:20],
        "category": str(form.get("category") or "").strip()[:40],
        "incident_date": str(form.get("incident_date") or "").strip()[:20],
        "description": chat_service.clean_body(form.get("description"))[: DESC_MAX + 1000],
    }
    errors: dict[str, str] = {}
    emp_id = _int(values["employee_id"])
    emp = next((e for e in employees if e.id == emp_id), None)
    if emp is None:
        errors["employee_id"] = "Choose the staff member this is about."
    if values["category"] not in CATEGORY_LABELS:
        errors["category"] = "Choose what happened."
    incident: date | None = None
    today = local_today(user.timezone)
    try:
        incident = date.fromisoformat(values["incident_date"])
    except ValueError:
        errors["incident_date"] = "Enter the date it happened."
    else:
        if incident > today:
            errors["incident_date"] = "The date can't be in the future."
        elif incident < today - timedelta(days=365):
            errors["incident_date"] = "Reports must be about something from the last 12 months."
    desc = values["description"]
    if len(desc) < DESC_MIN:
        errors["description"] = f"Please describe what happened in at least {DESC_MIN} characters."
    elif len(desc) > DESC_MAX:
        errors["description"] = f"Please keep the description under {DESC_MAX:,} characters."
    status_code = 400
    duplicate = f"You already have an open report about {emp.display_name}. The gym will reply to it first." if emp else ""
    if emp is not None and not errors:
        # race-safe: the member's report lock (held until COMMIT) serialises their parallel submits (tabs, scripts), so
        # the duplicate check and the caps below always see the report a previous submit just committed
        await caps.lock(db, REPORT_LOCK, user.id)
        dup = await db.scalar(select(MisconductReport.id).where(
            MisconductReport.reporter_id == user.id, MisconductReport.employee_id == emp.id,
            MisconductReport.status == ReportStatus.OPEN).limit(1))
        if dup:
            errors["employee_id"] = duplicate
        else:
            try:
                await caps.enforce(db, REPORTS_DAY_CAP, user.id, select(MisconductReport.id).where(
                    MisconductReport.reporter_id == user.id, MisconductReport.created_at >= caps.since()))
                await caps.enforce(db, OPEN_REPORTS_CAP, user.id, select(MisconductReport.id).where(
                    MisconductReport.reporter_id == user.id, MisconductReport.status == ReportStatus.OPEN))
            except caps.CapReached as exc:
                errors["employee_id"], status_code = str(exc), exc.status_code
    if errors or emp is None or incident is None:
        return render(request, "reports/new.html", _report_form_ctx(employees, values, errors), status_code=status_code)

    report = MisconductReport(gym_id=emp.gym_id, employee_id=emp.id, reporter_id=user.id, category=values["category"],
                              description=desc, incident_date=incident, status=ReportStatus.OPEN)
    try:
        async with db.begin_nested():  # a unique "one open report" index (if present) is the last line of defence
            db.add(report)
            await db.flush()
    except IntegrityError:
        errors["employee_id"] = duplicate
        return render(request, "reports/new.html", _report_form_ctx(employees, values, errors), status_code=400)
    await notify(
        db, emp.gym.owner_id, "New misconduct report",
        f"{user.display_name} reported {emp.display_name} ({category_label(report.category).lower()}) at {emp.gym.name}. "
        "Please review it and reply with an apology or a reason.",
        category=NotificationCategory.STAFF, severity=Severity.WARNING, link=f"/owner/reports/{report.id}",
    )
    await notify(
        db, user, "Report received",
        f"Thanks for telling us. {emp.gym.name}'s admin will review your report about {emp.display_name} and reply.",
        category=NotificationCategory.ACCOUNT, severity=Severity.INFO, link="/reports",
    )
    await audit(db, user, "report.create", "misconduct_report", report.id, gym_id=emp.gym_id,
                detail={"employee_id": emp.id, "category": report.category}, request=request)
    await db.commit()
    return redirect("/reports", "Report sent. The gym's admin has been notified and must reply.", "success")


REPORTS_PER_PAGE = 30


@router.get("/reports")
async def report_list(request: Request, db: DB, user: CustomerUser) -> Any:
    page = await paginate(
        db, select(MisconductReport)
        .options(joinedload(MisconductReport.employee), joinedload(MisconductReport.gym))
        .where(MisconductReport.reporter_id == user.id)
        .order_by(MisconductReport.created_at.desc(), MisconductReport.id.desc()),
        page=parse_page(request, REPORTS_PER_PAGE, REPORTS_PER_PAGE).page, per_page=REPORTS_PER_PAGE,
        max_per_page=REPORTS_PER_PAGE,
    )
    can_report = await db.scalar(
        select(Subscription.id).where(Subscription.customer_id == user.id,
                                      Subscription.status.in_(REPORTABLE_SUB_STATUSES)).limit(1)
    ) is not None
    return render(request, "reports/list.html", {"reports": page.items, "page": page, "category_label": category_label,
                                                 "can_report": can_report})


# ====================================================================== misconduct reports (owner)
OWNER_REPORT_PAGE = 30


@router.get("/owner/reports")
async def owner_reports(request: Request, db: DB, owner: OwnerUser, status: str = "", gym: str = "", q: str = "",
                        page: int = 1) -> Any:
    gyms = list((await db.execute(select(Gym).where(Gym.owner_id == owner.id).order_by(Gym.name))).scalars().all())
    gym_id = _int(gym)
    gym_id = gym_id if gym_id in {g.id for g in gyms} else None
    status_f = status if status in {s.value for s in ReportStatus} else ""
    q = _one_line(q, 80)

    base = select(MisconductReport).join(Gym, Gym.id == MisconductReport.gym_id).where(Gym.owner_id == owner.id)
    if gym_id:
        base = base.where(MisconductReport.gym_id == gym_id)
    if q:
        # names are matched only on each of the owner's reports' own staff member / reporter (correlated primary-key
        # lookups), never by scanning the platform-wide employees and users tables
        like = _like(q)
        staff, reporter = aliased(Employee), aliased(User)
        base = base.where(or_(
            select(staff.id).where(staff.id == MisconductReport.employee_id, staff.full_name.ilike(like, escape="\\"))
            .correlate(MisconductReport).exists(),
            select(reporter.id).where(reporter.id == MisconductReport.reporter_id,
                                      reporter.full_name.ilike(like, escape="\\")).correlate(MisconductReport).exists(),
            MisconductReport.description.ilike(like, escape="\\"),
        ))
    counts = {s.value: 0 for s in ReportStatus}
    for st, n in (await db.execute(
        base.with_only_columns(MisconductReport.status, func.count(MisconductReport.id)).group_by(MisconductReport.status)
    )).all():
        counts[getattr(st, "value", st)] = n
    stmt = base.where(MisconductReport.status == ReportStatus(status_f)) if status_f else base
    total = counts.get(status_f, 0) if status_f else sum(counts.values())
    pages = max(1, -(-total // OWNER_REPORT_PAGE))
    page = max(1, min(page, pages))
    open_first = case((MisconductReport.status == ReportStatus.OPEN, 0), else_=1)
    reports = list((await db.execute(
        stmt.options(joinedload(MisconductReport.employee), joinedload(MisconductReport.reporter),
                     joinedload(MisconductReport.gym))
        .order_by(open_first, MisconductReport.created_at.desc(), MisconductReport.id.desc())
        .limit(OWNER_REPORT_PAGE).offset((page - 1) * OWNER_REPORT_PAGE)
    )).unique().scalars().all())

    def url(**over: Any) -> str:
        params: dict[str, Any] = {"status": status_f, "gym": gym_id or "", "q": q, "page": page}
        params.update(over)
        clean = {k: v for k, v in params.items() if v not in ("", None) and not (k == "page" and v == 1)}
        return "/owner/reports" + (("?" + urlencode(clean)) if clean else "")

    tabs = [{"label": label, "url": url(status=key, page=1), "active": status_f == key,
             "count": counts.get(key, 0) if key else sum(counts.values())}
            for key, label in (("", "All"), ("open", "Awaiting reply"), ("accepted", "Accepted"), ("declined", "Declined"))]
    return render(request, "owner/reports/list.html", {
        "reports": reports, "gyms": gyms, "filters": {"status": status_f, "gym": gym_id, "q": q}, "counts": counts,
        "tabs": tabs, "page": page, "pages": pages, "total": total,
        "prev_url": url(page=page - 1) if page > 1 else None, "next_url": url(page=page + 1) if page < pages else None,
        "category_label": category_label,
    })


async def _owned_report(db: DB, owner: User, report_id: int) -> MisconductReport:
    report = (await db.execute(
        select(MisconductReport)
        .options(joinedload(MisconductReport.employee), joinedload(MisconductReport.reporter),
                 joinedload(MisconductReport.gym))
        .join(Gym, Gym.id == MisconductReport.gym_id)
        .where(MisconductReport.id == report_id, Gym.owner_id == owner.id)
    )).unique().scalar_one_or_none()
    if report is None:
        raise NotFound("Report not found.")
    return report


async def _report_detail(request: Request, db: DB, report: MisconductReport, *, values: dict[str, str] | None = None,
                         errors: dict[str, str] | None = None, status_code: int = 200) -> Any:
    other_reports = {getattr(s, "value", s): n for s, n in (await db.execute(
        select(MisconductReport.status, func.count(MisconductReport.id))
        .where(MisconductReport.employee_id == report.employee_id, MisconductReport.id != report.id)
        .group_by(MisconductReport.status)
    )).all()}
    resolver = None
    if report.resolved_by_id:
        resolver = await db.scalar(select(User.full_name).where(User.id == report.resolved_by_id))
    thread_id = await db.scalar(
        select(ChatThread.id).where(ChatThread.customer_id == report.reporter_id, ChatThread.gym_id == report.gym_id)
        .order_by(ChatThread.last_message_at.desc().nulls_last()).limit(1)
    )
    return render(request, "owner/reports/detail.html", {
        "report": report, "values": values or {"decision": "", "response": ""}, "errors": errors or {},
        "other_reports": other_reports, "resolver": resolver, "thread_id": thread_id,
        "category_label": category_label, "response_min": RESPONSE_MIN, "response_max": RESPONSE_MAX,
    }, status_code=status_code)


@router.get("/owner/reports/{report_id}")
async def owner_report_detail(request: Request, db: DB, owner: OwnerUser, report_id: int) -> Any:
    report = await _owned_report(db, owner, report_id)
    return await _report_detail(request, db, report)


@router.post("/owner/reports/{report_id}")
async def owner_report_respond(request: Request, db: DB, owner: OwnerUser, report_id: int) -> Any:
    report = await _owned_report(db, owner, report_id)
    if report.status != ReportStatus.OPEN:
        return redirect(f"/owner/reports/{report.id}", "This report has already been answered.", "info")
    form = await request.form()
    decision = str(form.get("decision") or "").strip()
    response = chat_service.clean_body(form.get("response"))
    values = {"decision": decision, "response": response}
    errors: dict[str, str] = {}
    if decision not in ("accept", "decline"):
        errors["decision"] = "Choose whether you accept the report (and apologise) or decline it."
    if decision == "accept":
        if len(response) < RESPONSE_MIN:
            errors["response"] = f"Write an apology to the member (at least {RESPONSE_MIN} characters)."
        elif not APOLOGY.search(response):
            errors["response"] = "When you accept a report, include an apology to the member (for example “We're sorry…”)."
    elif decision == "decline" and len(response) < RESPONSE_MIN:
        errors["response"] = f"Explain why you're declining the report (at least {RESPONSE_MIN} characters)."
    if len(response) > RESPONSE_MAX:
        errors["response"] = f"Please keep your reply under {RESPONSE_MAX:,} characters."
    if errors:
        return await _report_detail(request, db, report, values=values, errors=errors, status_code=400)

    accepted = decision == "accept"
    report.status = ReportStatus.ACCEPTED if accepted else ReportStatus.DECLINED
    report.admin_response = response
    report.resolved_at = utcnow()
    report.resolved_by_id = owner.id
    emp_name = report.employee.display_name if report.employee is not None else "the staff member"
    if report.reporter is not None and report.reporter.deleted_at is None:
        title = "Your report was accepted" if accepted else "Your report was reviewed"
        verdict = "accepted it" if accepted else "declined it"
        await notify(db, report.reporter, title,
                     f"{report.gym.name} reviewed your report about {emp_name} and {verdict}. Their reply: {clamp(response, 600)}",
                     category=NotificationCategory.ACCOUNT, severity=Severity.SUCCESS if accepted else Severity.INFO,
                     link="/reports")
    await audit(db, owner, "report.accept" if accepted else "report.decline", "misconduct_report", report.id,
                gym_id=report.gym_id, detail={"employee_id": report.employee_id, "summary": clamp(response, 200)},
                request=request)
    await db.commit()
    msg = ("Report accepted - your apology was sent to the member." if accepted
           else "Report declined - the member has been told why.")
    return redirect(f"/owner/reports/{report.id}", msg, "success")
