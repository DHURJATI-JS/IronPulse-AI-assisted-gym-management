"""WP-C1 staff management, invites, shifts (6 h / 50 km rules), job postings + enquiries.

Owner routes: team list, add staff (multipart, invite e-mail), staff profile with tabs (overview, schedule,
clients, documents, profile, activity), resend invite, activation re-check, suspend/reactivate, documents
(authorised download), weekly shifts, trainer-client assignment, job postings, enquiries (+ resume download).

Employment lifecycle (app/services/trust_safety.py): ``/owner/staff/{id}/remove`` ends an employment with a reason
from the fixed dropdown (benign / violation / severe - the consequences are the service's); ``/change-position`` ends
it as 'Position change' and re-adds the person in the new role in the same transaction (a position is never edited in
place - the profile editor refuses it). Every add / invite / re-add path checks re-hire eligibility
(``rehire_refusal``) and the race-safe caps (app/core/caps.py) under the gym's cap lock.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from datetime import timedelta
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Path, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import case, delete, extract, func, or_, select, update
from sqlalchemy.orm import contains_eager, joinedload, load_only, selectinload

from app.config import get_settings
from app.core import caps
from app.core.deps import DB, NotFound, OwnerUser, get_owned_employee, get_owned_gym
from app.core.nav import refresh_nav_state
from app.core.pagination import Page, keyset, paginate, parse_keyset, parse_page
from app.core.templating import humanize, redirect, render, status_tone
from app.core.utils import local_now, local_today, utcnow
from app.models import (
    AuditLog,
    AuthToken,
    Branch,
    Employee,
    EmployeeDocument,
    Gym,
    JobEnquiry,
    JobPosting,
    Plan,
    Subscription,
    TrainerClient,
    TrainerShift,
    User,
    VerificationRun,
)
from app.models.enums import (
    EmploymentStatus,
    EmployeeRole,
    EnquiryStatus,
    NotificationCategory,
    Role,
    Severity,
    ShiftMode,
    TokenPurpose,
    VerificationStatus,
)
from app.services import quotas
from app.services import staffing as S
from app.services import storage
from app.services import trust_safety as TS
from app.services.audit import audit
from app.services.jobs import enqueue
from app.services.notify import email_address, notify

log = logging.getLogger("ironpulse.owner_staff")
router = APIRouter()
MAX_ID = 2**63 - 1  # BIGINT ids: a larger path id is a 422 page, never a driver error (a 500)
IdParam = Annotated[int, Path(ge=1, le=MAX_ID)]

MAX_DOCS_PER_UPLOAD = 10
MAX_DOCS_PER_EMPLOYEE = 40  # kept for reference: the live value is caps "employee_documents" (app/core/caps.py)
# "Re-send invitation" e-mails an outside address: at most once per INVITE_RESEND_SECONDS per staff record and at most
# STAFF_INVITE_RESENDS_PER_DAY times in any 24 hours (the first invitation, sent when the record is added, not counted)
INVITE_RESEND_SECONDS = 120
STAFF_INVITE_RESENDS_PER_DAY = 5
ACCOUNT_DISABLED = ("This person's IronPulse account is disabled, so their staff access can't be restored. Contact "
                    "IronPulse support if you have questions.")
# list sizes: every list that grows with the data is bounded in SQL (app/core/pagination.py)
STAFF_PER_PAGE = 30
CLIENTS_PER_PAGE = 30
JOBS_PER_PAGE = 20
ENQUIRIES_PER_PAGE = 25
TIMELINE_LIMIT = 50  # audit events per activity window ("Show older activity" continues)
TIMELINE_RUNS_LIMIT = 40  # certification checks merged into one window
TABS = ("overview", "schedule", "clients", "documents", "profile", "activity")
STATUS_GROUPS: dict[str, tuple[EmploymentStatus, ...]] = {
    "active": (EmploymentStatus.ACTIVE,),
    "pending": (EmploymentStatus.INVITED, EmploymentStatus.PENDING_VERIFICATION),
    "suspended": (EmploymentStatus.SUSPENDED,),
    "former": (EmploymentStatus.TERMINATED,),
}
STATUS_TAB_LABELS = (("", "Current"), ("active", "Active"), ("pending", "Awaiting approval"),
                     ("suspended", "Suspended"), ("former", "Former"))
AUDIT_LABELS: dict[str, tuple[str, str, str]] = {
    "staff.create": ("Invitation sent", "envelope", "blue"),
    "staff.invite_resent": ("Invitation re-sent", "send", "blue"),
    "staff.update": ("Profile updated", "pencil", "zinc"),
    "staff.status": ("Status changed", "refresh", "violet"),
    "staff.suspend": ("Suspended", "lock", "red"),
    "staff.reactivate": ("Reactivated", "check-circle", "green"),
    "staff.recheck": ("Activation re-check requested", "refresh", "zinc"),
    "staff.shift_add": ("Shifts added", "calendar", "blue"),
    "staff.shift_delete": ("Shift removed", "calendar", "zinc"),
    "staff.document_add": ("Document uploaded", "upload", "blue"),
    "staff.document_delete": ("Document deleted", "trash", "zinc"),
    "staff.documents_approved": ("Certification approved", "check-badge", "green"),
    "staff.client_assign": ("Client assigned", "users", "green"),
    "staff.client_remove": ("Client unassigned", "users", "zinc"),
    "staff.removed": ("Employment ended", "logout", "red"),
    "staff.terminated": ("Employment ended", "logout", "zinc"),
    "staff.position_change": ("Position changed", "refresh", "violet"),
}
CURRENT = (EmploymentStatus.INVITED, EmploymentStatus.PENDING_VERIFICATION, EmploymentStatus.ACTIVE,
           EmploymentStatus.SUSPENDED)
POSITION_CHANGEABLE = (EmploymentStatus.INVITED, EmploymentStatus.PENDING_VERIFICATION, EmploymentStatus.ACTIVE)
FIELD_LABELS = {
    "full_name": "name", "invite_email": "invite e-mail", "phone": "phone", "role": "role", "role_title": "job title",
    "employment_type": "employment type", "work_mode": "work mode", "country_code": "payroll country",
    "currency": "salary currency", "monthly_salary": "monthly salary", "hire_date": "hire date", "bio": "bio",
    "specialties": "specialties", "certifications": "certifications", "years_experience": "years of experience",
    "is_public": "public profile visibility", "photo": "photo",
}


# ====================================================================== helpers
async def _owner_gyms(db: DB, owner: User) -> list[Gym]:
    return list((await db.execute(select(Gym).where(Gym.owner_id == owner.id).order_by(Gym.name))).scalars().all())


def _detail_options() -> tuple[Any, ...]:
    return (
        joinedload(Employee.gym),
        joinedload(Employee.user),
        selectinload(Employee.documents),
        selectinload(Employee.shifts).joinedload(TrainerShift.branch),
    )


async def _load_employee(db: DB, owner: User, employee_id: int) -> Employee:
    return await get_owned_employee(db, employee_id, owner, *_detail_options())


def _linked_user(emp: Employee) -> User | None:
    return emp.user if emp.user is not None and emp.user.deleted_at is None else None


def _like(q: str) -> str:
    return "%" + re.sub(r"([\\%_])", r"\\\1", q) + "%"


def _id(value: Any) -> int | None:
    """A row id from a form / query value: a plain positive number within BIGINT, else None - never an exception
    ('²', thousands of digits) nor a driver error (beyond BIGINT)."""
    text = str(value or "").strip()
    if not (text.isascii() and text.isdigit()) or len(text) > 19:
        return None
    n = int(text)
    return n if 0 < n <= MAX_ID else None


async def account_disabled(db: Any, user: User | None) -> bool:
    """The linked account is disabled (``users.is_active`` false) or under an active platform ban: no staff access."""
    if user is None:
        return False
    if not user.is_active:
        return True
    return await TS.active_ban(db, user.id) is not None


def _private_response(data: bytes, content_type: str, name: str) -> Response:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "document").rsplit(".", 1)[0])[:80] or "document"
    ext = "pdf" if content_type == "application/pdf" else "jpg"
    return Response(
        content=data, media_type=content_type,
        headers={"Content-Disposition": f'inline; filename="{stem}.{ext}"', "Cache-Control": "no-store, private",
                 "X-Content-Type-Options": "nosniff"},
    )


def _form_ctx(gym: Gym, verified_branches: int, values: dict[str, Any], errors: dict[str, str]) -> dict[str, Any]:
    return {"S": S, "gym": gym, "verified_branches": verified_branches, "values": values, "errors": errors}


def _role_label(emp: Employee) -> str:
    return emp.role_title or S.ROLE_LABELS.get(emp.role.value, "Staff")


async def _queue_activation_check(db: DB, emp: Employee) -> None:
    await enqueue(db, "staff_activation_check", {"employee_id": emp.id}, dedupe_key=f"staff_activation_check:{emp.id}")


def _current_staff_rows(gym_id: int) -> Any:
    return select(Employee.id).where(Employee.gym_id == gym_id, Employee.deleted_at.is_(None),
                                     Employee.status != EmploymentStatus.TERMINATED)


def _pending_invite_rows(gym_id: int) -> Any:
    return select(Employee.id).where(Employee.gym_id == gym_id, Employee.deleted_at.is_(None),
                                     Employee.status == EmploymentStatus.INVITED)


async def _reserve_staff_slot(db: DB, gym_id: int) -> None:
    """The race-safe caps of one new (invited) staff record: unanswered invitations first, then all current staff -
    the narrower cap first (app/core/caps.py lock order). Raises CapReached; both locks hold until COMMIT / ROLLBACK,
    so the caller re-checks the invite e-mail and inserts under them (two identical submits can't both pass)."""
    await caps.enforce(db, "staff_invites_per_gym", gym_id, _pending_invite_rows(gym_id))
    await caps.enforce(db, "staff_per_gym", gym_id, _current_staff_rows(gym_id))


async def _delete_keys(keys: list[str]) -> None:
    for key in keys:
        await storage.delete_key(key)


DOC_FIELDS = {"certification": "certification_files", "resume": "resume_file", "contract": "contract_file"}


async def _reserve_doc_storage(db: Any, gym_id: int, docs: list[tuple[str, storage.StoredFile]]) -> tuple[str, str] | None:
    """Charge the gym's storage quota (app/services/quotas.py) for the staff documents of one form - after the narrower
    caps (the cap lock order). (form field, friendly refusal) when they would pass it, else None."""
    total = sum(int(stored.size or 0) for _, stored in docs)
    if not total:
        return None
    try:
        await quotas.reserve_gym(db, gym_id, total)
    except caps.CapReached as exc:
        return DOC_FIELDS.get(docs[0][0], "certification_files"), str(exc)
    return None


async def _lifecycle(db: DB, emp: Employee) -> dict[str, Any]:
    """How this employment ended (this gym's own record only) and the records a position change links it to."""
    out: dict[str, Any] = {"removal": None, "predecessor": None, "successor": None}
    if emp.removal_reason:
        cat = emp.removal_category or TS.BENIGN
        out["removal"] = {
            "label": TS.reason_label(emp.removal_reason) or humanize(emp.removal_reason), "category": cat,
            "category_label": TS.CATEGORY_LABELS.get(cat, ""), "tone": TS.CATEGORY_TONES.get(cat, "zinc"),
            "note": emp.removal_note, "at": emp.removed_at, "by": await TS.removal_actor_name(db, emp),
        }
    if emp.previous_employment_id:
        out["predecessor"] = await db.scalar(select(Employee).where(Employee.id == emp.previous_employment_id,
                                                                    Employee.gym_id == emp.gym_id))
    if emp.status == EmploymentStatus.TERMINATED and emp.removal_reason == TS.POSITION_CHANGE:
        out["successor"] = await db.scalar(select(Employee).where(Employee.previous_employment_id == emp.id,
                                                                  Employee.gym_id == emp.gym_id)
                                           .order_by(Employee.id.desc()).limit(1))
    return out


async def _outstanding_dues(db: DB, emp: Employee) -> list[Any] | None:
    """WP-C2 ``services/payroll.outstanding_dues`` when available (None = payroll not installed)."""
    try:
        from app.services import payroll  # type: ignore[attr-defined]
    except Exception:
        return None
    fn = getattr(payroll, "outstanding_dues", None)
    if fn is None:
        return None
    try:
        async with db.begin_nested():
            return list(await fn(db, emp))
    except Exception:  # never break the staff page because of another package
        log.warning("outstanding_dues failed for employee %s", emp.id, exc_info=True)
        return None


def _client_clauses(emp: Employee) -> list[Any]:
    return [TrainerClient.employee_id == emp.id, TrainerClient.active.is_(True), User.deleted_at.is_(None)]


async def _trainer_client_count(db: DB, emp: Employee) -> int:
    return int(await db.scalar(
        select(func.count(TrainerClient.id)).join(User, User.id == TrainerClient.customer_id).where(*_client_clauses(emp))
    ) or 0)


async def _trainer_clients(db: DB, emp: Employee, page: int) -> Page[dict[str, Any]]:
    """One page of the trainer's active clients (COUNT + one LIMIT/OFFSET query)."""
    stmt = (
        select(TrainerClient, Subscription, Plan)
        .join(User, User.id == TrainerClient.customer_id)
        .outerjoin(Subscription, Subscription.id == TrainerClient.subscription_id)
        .outerjoin(Plan, Plan.id == Subscription.plan_id)
        .options(contains_eager(TrainerClient.customer))
        .where(*_client_clauses(emp))
        .order_by(User.full_name, TrainerClient.id)
    )
    count = select(func.count(TrainerClient.id)).join(User, User.id == TrainerClient.customer_id).where(*_client_clauses(emp))
    p = await paginate(db, stmt, page=page, per_page=CLIENTS_PER_PAGE, count_stmt=count)
    return dataclasses.replace(p, items=[{"link": link, "customer": link.customer, "subscription": sub, "plan": plan}
                                         for link, sub, plan in p.items])


async def _latest_run(db: DB, emp: Employee) -> VerificationRun | None:
    return (await db.execute(
        select(VerificationRun).where(VerificationRun.employee_id == emp.id)
        .order_by(VerificationRun.created_at.desc(), VerificationRun.id.desc()).limit(1)
    )).scalars().first()


async def _timeline(db: DB, emp: Employee, before_id: int | None = None) -> dict[str, Any]:
    """One window of the activity tab, newest first: at most TIMELINE_LIMIT audit events (keyset on
    (created_at, id), served by ix_audit_logs_entity) plus the certification checks of the same time span (at most
    TIMELINE_RUNS_LIMIT). ``before_id`` (an audit id of this employee) continues below an earlier window."""
    scope = (AuditLog.entity_type == "employee", AuditLog.entity_id == emp.id)
    pivot_at = None
    if before_id is not None:  # only a cursor of this employee's own timeline counts (no probing of other rows)
        pivot_at = await db.scalar(select(AuditLog.created_at).where(AuditLog.id == before_id, *scope))
        if pivot_at is None:
            before_id = None
    window = await keyset(
        db, select(AuditLog, User.full_name).outerjoin(User, TS.audit_actor_join()).where(*scope),
        AuditLog.id, before_id=before_id, limit=TIMELINE_LIMIT, order_col=AuditLog.created_at,
    )
    audits = list(window.items)
    runs_stmt = select(VerificationRun).where(VerificationRun.employee_id == emp.id)
    if pivot_at is not None:  # checks shown in the newer window stay there
        runs_stmt = runs_stmt.where(VerificationRun.created_at < pivot_at)
    if window.has_more and audits:  # older checks belong to the next window
        runs_stmt = runs_stmt.where(VerificationRun.created_at >= audits[-1][0].created_at)
    runs = list((await db.execute(
        runs_stmt.order_by(VerificationRun.created_at.desc(), VerificationRun.id.desc()).limit(TIMELINE_RUNS_LIMIT)
    )).scalars().all())
    events: list[dict[str, Any]] = []
    created_logged = False
    for row, actor_name in audits:
        title, ico, tone = AUDIT_LABELS.get(row.action, (humanize(row.action.split(".")[-1]), "info", "zinc"))
        detail = (row.detail or {}).get("summary")
        created_logged = created_logged or row.action == "staff.create"
        actor = (TS.PLATFORM_ACTOR if TS.is_platform_row(row)  # an account ban's side effect: never who reported them
                 else actor_name or ("Automatic" if row.actor_id is None else None))
        events.append({"when": row.created_at, "title": title, "detail": detail, "icon": ico, "tone": tone,
                       "actor": actor})
    for run in runs:
        events.append({"when": run.created_at, "title": f"Certification check · {humanize(run.outcome)}",
                       "detail": run.summary, "icon": "check-badge", "tone": status_tone(run.outcome),
                       "actor": "Reviewed by the gym admin" if run.manual else ("OCR + AI" if run.ai_provider else "OCR")})
    if not created_logged and not window.has_more:  # the record's creation closes the oldest window
        events.append({"when": emp.created_at, "title": "Staff record created", "detail": None, "icon": "user",
                       "tone": "zinc", "actor": None})
    events.sort(key=lambda e: e["when"], reverse=True)
    base = f"/owner/staff/{emp.id}?tab=activity"
    return {"events": events, "older_url": f"{base}&before_id={window.next_cursor}" if window.has_more else None,
            "latest_url": base if before_id is not None else None}


async def _branch_options(db: DB, emp: Employee) -> tuple[list[dict[str, Any]], int]:
    """The gym's branches for the shift form (schedule tab only) + how many are verified."""
    branches = list(
        (await db.execute(select(Branch).options(selectinload(Branch.hours)).where(Branch.gym_id == emp.gym_id)
                          .order_by(Branch.name, Branch.id))).scalars().all()
    )
    options = []
    for b in branches:
        info = S.BranchInfo.from_model(b)
        if not info.verified:
            note = "not verified"
        elif not info.is_open:
            note = "closed"
        else:
            note = S.hours_summary(info.hours)
        options.append({"id": b.id, "name": b.name, "usable": info.verified and info.is_open, "note": note})
    return options, sum(1 for b in branches if b.verification_status == VerificationStatus.VERIFIED)


async def _render_detail(request: Request, db: DB, emp: Employee, tab: str, *, status_code: int = 200,
                         **extra: Any) -> Any:
    """Staff page (all tabs). Each tab loads only what it shows, with a constant number of queries whatever the
    number of shifts, clients, members, branches or audit events: clients are paginated, the member picker is
    limited (with a search), the activity tab is a keyset window."""
    if tab not in TABS or (tab == "clients" and not emp.is_trainer):
        tab = "overview"
    user = _linked_user(emp)
    reqs = S.activation_requirements(emp, user, emp.documents)
    viewer = request.state.user
    today_wd = local_now(getattr(viewer, "timezone", None)).weekday()
    busy: list[Any] = []
    if tab in ("overview", "schedule") and emp.user_id:
        own_ids = {s.id for s in emp.shifts}
        busy = [s for s in await S.person_shift_slots(db, emp) if s.shift_id not in own_ids]
    branch_options: list[dict[str, Any]] = []
    verified_branches = 0
    if tab == "schedule":
        branch_options, verified_branches = await _branch_options(db, emp)
    elif tab == "profile":
        verified_branches = await S.verified_branch_count(db, emp.gym_id)

    clients: Page[dict[str, Any]] | None = None
    client_total = eligible_total = 0
    eligible: list[Subscription] = []
    member_q = S.one_line(str(request.query_params.get("member_q") or ""))[:80]
    if emp.is_trainer:
        if tab == "clients":
            clients = await _trainer_clients(db, emp, parse_page(request).page)
            client_total = clients.total
            eligible = await S.eligible_subscriptions(db, emp, q=member_q)
            eligible_total = await S.count_eligible_subscriptions(db, emp, q=member_q)
        else:
            client_total = await _trainer_client_count(db, emp)
            if tab == "overview":
                eligible_total = await S.count_eligible_subscriptions(db, emp)

    timeline = await _timeline(db, emp, parse_keyset(request).before_id) if tab == "activity" else None
    latest_run = await _latest_run(db, emp) if tab == "documents" else None
    invite = dues = None
    if tab == "overview":
        token = (
            await db.execute(
                select(AuthToken.created_at, AuthToken.expires_at)
                .where(AuthToken.employee_id == emp.id, AuthToken.purpose == TokenPurpose.STAFF_INVITE,
                       AuthToken.used_at.is_(None))
                .order_by(AuthToken.created_at.desc()).limit(1)
            )
        ).first()
        invite = ({"created_at": token.created_at, "expires_at": token.expires_at,
                   "expired": token.expires_at <= utcnow()} if token else None)
        dues = await _outstanding_dues(db, emp)

    first_usable = next((str(b["id"]) for b in branch_options if b["usable"]), "")
    documents = sorted(emp.documents, key=lambda d: (d.created_at, d.id), reverse=True)
    settings = get_settings()
    ctx: dict[str, Any] = {
        **await _lifecycle(db, emp), "TS": TS, "current": emp.status in CURRENT and not emp.is_deleted,
        "can_change_position": emp.status in POSITION_CHANGEABLE and not emp.is_deleted,
        "S": S, "emp": emp, "gym": emp.gym, "staff_user": user, "linked": user is not None,
        "tab": tab, "reqs": reqs, "all_met": all(r.met for r in reqs),
        "week_days": S.week_segments(emp.shifts, busy=busy), "busy_slots": busy, "today_wd": today_wd,
        "week_minutes": S.weekly_minutes(emp.shifts),
        "shifts_by_day": [[s for s in emp.shifts if s.weekday == d] for d in range(7)],
        "branch_options": branch_options, "documents": documents,
        "clients": clients, "client_total": client_total, "clients_path": f"/owner/staff/{emp.id}",
        "eligible": eligible, "eligible_total": eligible_total, "eligible_limit": S.ELIGIBLE_LIMIT,
        "member_q": member_q,
        "timeline": timeline["events"] if timeline else [],
        "timeline_older_url": timeline["older_url"] if timeline else None,
        "timeline_latest_url": timeline["latest_url"] if timeline else None,
        "latest_run": latest_run, "invite": invite, "dues": dues, "verified_branches": verified_branches,
        "min_gap_hours": f"{settings.trainer_min_gap_hours:g}",
        "max_distance_km": f"{settings.trainer_max_branch_distance_km:g}",
        "shift_errors": [], "client_errors": [], "doc_errors": [], "errors": {}, "page_error": None,
        "ai_feature": emp.is_trainer and tab == "documents",
        "shift_values": {
            "weekdays": [], "start_time": "06:00", "end_time": "10:00",
            "mode": "offline" if S.work_mode_allows(emp.work_mode, "offline") and first_usable else "online",
            "branch_id": first_usable,
        },
        "values": S.staff_values_from_employee(emp),
        "tabs": [t for t in (
            ("overview", "Overview", 0), ("schedule", "Schedule", len(emp.shifts)),
            ("clients", "Clients", client_total) if emp.is_trainer else None,
            ("documents", "Documents", len(documents)), ("profile", "Edit profile", 0), ("activity", "Activity", 0),
        ) if t],
    }
    ctx.update(extra)
    return render(request, "owner/staff/detail.html", ctx, status_code=status_code)


# ====================================================================== team list
@router.get("/owner/staff")
async def staff_list(request: Request, db: DB, owner: OwnerUser, q: str = "", gym: str = "", role: str = "",
                     status: str = "") -> Any:
    gyms = await _owner_gyms(db, owner)
    gym_ids = {g.id for g in gyms}
    gym_id = picked if (picked := _id(gym)) in gym_ids else None
    role_f = role if role in S.ROLE_LABELS else ""
    status_f = status if status in STATUS_GROUPS else ""
    q = S.one_line(q)[:80]

    def scoped(stmt: Any) -> Any:
        stmt = stmt.where(Gym.owner_id == owner.id)
        if gym_id:
            stmt = stmt.where(Employee.gym_id == gym_id)
        if role_f:
            stmt = stmt.where(Employee.role == EmployeeRole(role_f))
        if q:
            like = _like(q)
            stmt = stmt.where(or_(Employee.full_name.ilike(like, escape="\\"), Employee.invite_email.ilike(like, escape="\\")))
        return stmt

    order = case(
        (Employee.status == EmploymentStatus.PENDING_VERIFICATION, 0), (Employee.status == EmploymentStatus.INVITED, 1),
        (Employee.status == EmploymentStatus.ACTIVE, 2), (Employee.status == EmploymentStatus.SUSPENDED, 3), else_=4,
    )
    stmt = scoped(select(Employee).join(Gym, Gym.id == Employee.gym_id).options(contains_eager(Employee.gym)))
    stmt = stmt.where(Employee.status.in_(STATUS_GROUPS[status_f])) if status_f else stmt.where(
        Employee.status != EmploymentStatus.TERMINATED)
    page = await paginate(db, stmt.order_by(order, Employee.full_name, Employee.id), page=parse_page(request).page,
                          per_page=STAFF_PER_PAGE)
    employees = list(page.items)

    # tab counts (same filters except status) + team KPIs + payroll: a constant number of aggregate queries
    tab_counts = dict(
        (await db.execute(scoped(
            select(Employee.status, func.count(Employee.id)).join(Gym, Gym.id == Employee.gym_id)
        ).group_by(Employee.status))).all()
    )
    kpi_rows = (await db.execute(
        select(Employee.status, Employee.is_trainer, func.count(Employee.id)).join(Gym, Gym.id == Employee.gym_id)
        .where(Gym.owner_id == owner.id).group_by(Employee.status, Employee.is_trainer)
    )).all()
    stats = {"active": 0, "pending": 0, "trainers": 0}
    for st, trainer, n in kpi_rows:
        if st == EmploymentStatus.ACTIVE:
            stats["active"] += n
        if st in STATUS_GROUPS["pending"]:
            stats["pending"] += n
        if trainer and st in (EmploymentStatus.ACTIVE, *STATUS_GROUPS["pending"]):
            stats["trainers"] += n
    payroll = [
        (cur, total) for cur, total in (await db.execute(
            select(Employee.currency, func.sum(Employee.monthly_salary)).join(Gym, Gym.id == Employee.gym_id)
            .where(Gym.owner_id == owner.id, Employee.status == EmploymentStatus.ACTIVE)
            .group_by(Employee.currency).order_by(func.sum(Employee.monthly_salary).desc())
        )).all()
    ]
    shift_stats: dict[int, tuple[int, float]] = {}
    client_counts: dict[int, int] = {}
    if employees:
        ids = [e.id for e in employees]
        minutes = func.sum(extract("epoch", TrainerShift.end_time) - extract("epoch", TrainerShift.start_time)) / 60
        shift_stats = {
            emp_id: (int(n), float(m or 0)) for emp_id, n, m in (await db.execute(
                select(TrainerShift.employee_id, func.count(TrainerShift.id), minutes)
                .where(TrainerShift.employee_id.in_(ids)).group_by(TrainerShift.employee_id)
            )).all()
        }
        client_counts = {
            emp_id: int(n) for emp_id, n in (await db.execute(
                select(TrainerClient.employee_id, func.count(TrainerClient.id))
                .where(TrainerClient.employee_id.in_(ids), TrainerClient.active.is_(True))
                .group_by(TrainerClient.employee_id)
            )).all()
        }

    def tab_url(key: str) -> str:
        params = {k: v for k, v in (("q", q), ("gym", gym_id or ""), ("role", role_f), ("status", key)) if v}
        return "/owner/staff" + (f"?{urlencode(params)}" if params else "")

    status_tabs = []
    for key, label in STATUS_TAB_LABELS:
        if key:
            count = sum(tab_counts.get(s, 0) for s in STATUS_GROUPS[key])
        else:
            count = sum(n for s, n in tab_counts.items() if s != EmploymentStatus.TERMINATED)
        status_tabs.append({"key": key, "label": label, "count": count, "url": tab_url(key), "active": key == status_f})

    return render(request, "owner/staff/list.html", {
        "S": S, "gyms": gyms, "employees": employees, "page": page, "stats": stats, "payroll": payroll,
        "shift_stats": shift_stats, "client_counts": client_counts, "status_tabs": status_tabs,
        "filters": {"q": q, "gym": gym_id, "role": role_f, "status": status_f},
        "page_params": {"q": q, "gym": gym_id or "", "role": role_f, "status": status_f},
    })


# ====================================================================== add staff
@router.get("/owner/staff/new")
async def staff_new_pick(request: Request, db: DB, owner: OwnerUser) -> Any:
    gyms = await _owner_gyms(db, owner)
    if not gyms:
        return redirect("/owner/gyms/new", "Create a gym first - staff always belong to a gym.", "info")
    if len(gyms) == 1:
        return RedirectResponse(f"/owner/gyms/{gyms[0].id}/staff/new", status_code=303)
    return render(request, "owner/staff/choose_gym.html", {
        "gyms": gyms, "heading": "Add a staff member", "eyebrow": "Staff", "back": "/owner/staff",
        "path_suffix": "staff/new",
    })


@router.get("/owner/gyms/{gym_id}/staff/new")
async def staff_new_form(request: Request, gym_id: IdParam, db: DB, owner: OwnerUser, name: str = "", email: str = "",
                         role: str = "") -> Any:
    gym = await get_owned_gym(db, gym_id, owner)
    vb = await S.verified_branch_count(db, gym.id)
    values = S.staff_defaults(gym, verified_branches=vb)
    if name:  # pre-filled from a hired job applicant
        values["full_name"] = S.one_line(name)[:120]
    if email:
        values["invite_email"] = email.strip()[:254]
    if role in S.ROLE_LABELS:
        values["role"] = role
    return render(request, "owner/staff/new.html", _form_ctx(gym, vb, values, {}))


@router.post("/owner/gyms/{gym_id}/staff/new")
async def staff_create(request: Request, gym_id: IdParam, db: DB, owner: OwnerUser) -> Any:
    gym = await get_owned_gym(db, gym_id, owner)
    form = await request.form()
    vb = await S.verified_branch_count(db, gym.id)
    data, errors, values = S.validate_staff_form(form, verified_branches=vb, default_currency=gym.currency)
    photo = form.get("photo")
    if not S.has_file(photo):
        errors["photo"] = "A staff photo is required."
    if data is not None and "invite_email" not in errors:
        problem = (await S.invite_email_problem(db, data.invite_email, gym.id)
                   or await TS.rehire_refusal(db, owner_id=owner.id, email=data.invite_email))
        if problem:
            errors["invite_email"] = problem
    if sum(len(S.form_files(form, k)) for k in ("certification_files", "resume_file", "contract_file")) > MAX_DOCS_PER_UPLOAD:
        errors["certification_files"] = f"Upload at most {MAX_DOCS_PER_UPLOAD} documents at once."
    if errors or data is None:
        return render(request, "owner/staff/new.html", _form_ctx(gym, vb, values, errors), status_code=400)

    saved: list[str] = []
    docs: list[tuple[str, storage.StoredFile]] = []
    current = "photo"
    try:
        stored_photo = await storage.save_public_image(photo, kind="staff", max_side=1200)  # type: ignore[arg-type]
        saved.append(stored_photo.key)
        for field_name, doc_type in (("certification_files", "certification"), ("resume_file", "resume"),
                                     ("contract_file", "contract")):
            current = field_name
            for upload in S.form_files(form, field_name):
                stored, _plain = await storage.save_private_file(upload, kind="staff_docs")
                saved.append(stored.key)
                docs.append((doc_type, stored))
    except storage.UploadError as exc:
        await _delete_keys(saved)
        errors[current] = str(exc)
        return render(request, "owner/staff/new.html", _form_ctx(gym, vb, values, errors), status_code=400)

    # under the person's lock and then the gym's cap locks (held until COMMIT; the lock order of every staff path:
    # person, then row, then cap): the caps, then the same checks again - a double submit, a second admin adding the
    # same person, or a removal of this person at another of your gyms at the same moment is seen here
    await TS.lock_person(db, await TS.person_of_email(db, data.invite_email))
    try:
        await _reserve_staff_slot(db, gym.id)
    except caps.CapReached as exc:
        await _delete_keys(saved)
        errors["invite_email"] = str(exc)
        return render(request, "owner/staff/new.html", _form_ctx(gym, vb, values, errors), status_code=exc.status_code)
    if (over := await _reserve_doc_storage(db, gym.id, docs)) is not None:  # the gym's storage quota
        await _delete_keys(saved)
        errors[over[0]] = over[1]
        return render(request, "owner/staff/new.html", _form_ctx(gym, vb, values, errors), status_code=409)
    problem = (await S.invite_email_problem(db, data.invite_email, gym.id)
               or await TS.rehire_refusal(db, owner_id=owner.id, email=data.invite_email))
    if problem:  # nothing was written: the request's session rolls back (and releases the locks) when it closes
        await _delete_keys(saved)
        errors["invite_email"] = problem
        return render(request, "owner/staff/new.html", _form_ctx(gym, vb, values, errors), status_code=400)

    is_trainer = data.role == EmployeeRole.TRAINER
    has_cert = any(t == "certification" for t, _ in docs)
    emp = Employee(
        gym_id=gym.id, invite_email=data.invite_email, full_name=data.full_name, phone=data.phone, role=data.role,
        role_title=data.role_title, is_trainer=is_trainer, work_mode=data.work_mode,
        employment_type=data.employment_type, country_code=data.country_code, currency=data.currency,
        monthly_salary=data.monthly_salary, hire_date=data.hire_date, status=EmploymentStatus.INVITED,
        photo_key=stored_photo.key, bio=data.bio, specialties=data.specialties, certifications=data.certifications,
        years_experience=data.years_experience, is_public=data.is_public,
        verification_status=VerificationStatus.QUEUED if (is_trainer and has_cert) else VerificationStatus.DRAFT,
    )
    db.add(emp)
    await db.flush()
    for doc_type, stored in docs:
        db.add(EmployeeDocument(
            employee_id=emp.id, doc_type=doc_type, file_key=stored.key, original_filename=stored.original_filename,
            content_type=stored.content_type, sha256=stored.sha256, size_bytes=stored.size,
        ))
    await S.send_staff_invite(db, emp, gym)
    if is_trainer and has_cert:  # read the certificate now; activation still waits for the account link
        await _queue_activation_check(db, emp)
    existing = await db.scalar(select(User).where(User.email == data.invite_email, User.deleted_at.is_(None)))
    if existing is not None and existing.role == Role.STAFF:
        await notify(db, existing, f"New staff invitation from {gym.name}",
                     f"{gym.name} added you as {_role_label(emp)}. Open the invitation e-mail we sent to "
                     f"{data.invite_email} to link this job to your account.",
                     category=NotificationCategory.STAFF, severity=Severity.INFO, link="/staff")
    await audit(db, owner, "staff.create", "employee", emp.id, gym_id=gym.id, request=request, detail={
        "summary": f"Invited {emp.invite_email} as {_role_label(emp)}"
                   + (f" with {len(docs)} document{'s' if len(docs) != 1 else ''}" if docs else ""),
        "email": emp.invite_email, "role": emp.role.value,
    })
    await db.commit()
    return redirect(f"/owner/staff/{emp.id}",
                    f"Invitation sent to {emp.invite_email}. {emp.full_name} is approved automatically once they "
                    "link their own account and pass verification.")


# ====================================================================== staff page
@router.get("/owner/staff/{employee_id}")
async def staff_detail(request: Request, employee_id: IdParam, db: DB, owner: OwnerUser, tab: str = "overview") -> Any:
    emp = await _load_employee(db, owner, employee_id)
    return await _render_detail(request, db, emp, tab)


@router.post("/owner/staff/{employee_id}")
async def staff_update(request: Request, employee_id: IdParam, db: DB, owner: OwnerUser) -> Any:
    emp = await _load_employee(db, owner, employee_id)
    if emp.is_deleted or emp.status == EmploymentStatus.TERMINATED:
        return redirect(f"/owner/staff/{emp.id}", "Former staff records can't be edited.", "warning")
    form = await request.form()
    user = _linked_user(emp)
    vb = await S.verified_branch_count(db, emp.gym_id)
    unchanged_mode = S.form_str(form, "work_mode") == emp.work_mode.value  # keep a mode that was valid when set
    data, errors, values = S.validate_staff_form(
        form, verified_branches=max(vb, 1) if unchanged_mode else vb, default_currency=emp.gym.currency,
        locked_email=emp.invite_email if user is not None else None,
        locked_position=(emp.role.value, emp.role_title),  # a role / title change is "Change position", never here
    )
    if data is not None:
        modes_in_use = {s.mode.value for s in emp.shifts}
        if "offline" in modes_in_use and not S.work_mode_allows(data.work_mode, "offline"):
            errors["work_mode"] = "Remove their branch shifts first (Schedule tab) - they still have shifts at a branch."
        elif "online" in modes_in_use and not S.work_mode_allows(data.work_mode, "online"):
            errors["work_mode"] = "Remove their online shifts first (Schedule tab) before switching to branch-only work."
        if user is None and data.invite_email != emp.invite_email:
            problem = (await S.invite_email_problem(db, data.invite_email, emp.gym_id, exclude_employee_id=emp.id)
                       or await TS.rehire_refusal(db, owner_id=owner.id, email=data.invite_email))
            if problem:
                errors["invite_email"] = problem
    photo = form.get("photo")
    if errors or data is None:
        return await _render_detail(request, db, emp, "profile", values=values, errors=errors, status_code=400)
    old_photo: str | None = None
    if S.has_file(photo):
        try:
            stored = await storage.save_public_image(photo, kind="staff", max_side=1200)  # type: ignore[arg-type]
        except storage.UploadError as exc:
            return await _render_detail(request, db, emp, "profile", values=values, errors={"photo": str(exc)},
                                        status_code=400)
        old_photo, emp.photo_key = emp.photo_key, stored.key
    was_trainer, old_name = emp.is_trainer, emp.full_name
    changes = S.apply_staff_input(emp, data)
    changes.pop("is_trainer", None)
    if old_photo:
        changes["photo"] = ("old", "new")
    if not changes:
        return redirect(f"/owner/staff/{emp.id}?tab=profile", "Nothing to save - no changes.", "info")
    recheck = False
    if emp.is_trainer and (not was_trainer or emp.full_name != old_name):
        if any(d.doc_type == "certification" for d in emp.documents):
            emp.verification_status = VerificationStatus.QUEUED  # name/role changed -> check the certificate again
        recheck = True
    if recheck or "photo" in changes:
        await _queue_activation_check(db, emp)
    labels = [FIELD_LABELS.get(k, k) for k in changes]
    detail: dict[str, Any] = {"fields": list(changes), "summary": "Changed " + ", ".join(labels)}
    if "monthly_salary" in changes:
        old, new = changes["monthly_salary"]
        detail["salary"] = {"from": str(old), "to": str(new), "currency": emp.currency}
        detail["summary"] += f" (salary {old} → {new} {emp.currency})"
    await audit(db, owner, "staff.update", "employee", emp.id, gym_id=emp.gym_id, detail=detail, request=request)
    if user is not None:
        body = f"{emp.gym.name} updated your staff profile: " + ", ".join(labels) + "."
        if "monthly_salary" in changes:
            body += f" Your monthly salary is now {emp.monthly_salary:,.2f} {emp.currency}."
        await notify(db, user, "Your staff profile was updated", body, category=NotificationCategory.ACCOUNT,
                     severity=Severity.INFO, link="/staff")
    reinvited = user is None and "invite_email" in changes and emp.status == EmploymentStatus.INVITED
    if reinvited:  # earlier links were bound to the old address -> invalidate them and invite the new one
        await S.send_staff_invite(db, emp, emp.gym)
    await db.commit()
    if old_photo:
        await storage.delete_key(old_photo)
    if reinvited:
        return redirect(f"/owner/staff/{emp.id}", f"Profile saved. A fresh invitation was sent to {emp.invite_email}.")
    return redirect(f"/owner/staff/{emp.id}", "Profile saved.")


@router.post("/owner/staff/{employee_id}/resend-invite")
async def staff_resend_invite(request: Request, employee_id: IdParam, db: DB, owner: OwnerUser) -> Any:
    emp = await get_owned_employee(db, employee_id, owner, joinedload(Employee.gym), joinedload(Employee.user))
    back = f"/owner/staff/{emp.id}"
    if emp.is_deleted or emp.status == EmploymentStatus.TERMINATED:
        return redirect(back, "Former staff can't be invited again - add them as new staff.", "warning")
    if _linked_user(emp) is not None:
        return redirect(back, "Their account is already linked - no invitation needed.", "info")
    # the person, then the row (the trust & safety lock order; both held until COMMIT): parallel clicks from several
    # tabs or admins run one after the other, so the throttle below sees the re-send the first one made, and a removal
    # of this person at another of your gyms at this moment is seen by the re-hire check
    await TS.lock_person(db, await TS.person_of_email(db, emp.invite_email))
    row = await TS.lock_employee(db, emp.id)
    if row is None or row.deleted_at is not None or row.status == EmploymentStatus.TERMINATED:
        return redirect(back, "Former staff can't be invited again - add them as new staff.", "warning")
    if row.user_id is not None and row.user_id != emp.user_id:  # they accepted meanwhile
        return redirect(back, "Their account is already linked - no invitation needed.", "info")
    refusal = await TS.rehire_refusal(db, owner_id=owner.id, email=emp.invite_email)
    if refusal:  # banned / blocked since the first invitation, or removed for a violation at another of your gyms
        return redirect(back, refusal, "warning")
    last, recent = (await db.execute(
        select(func.max(AuditLog.created_at), func.count(AuditLog.id).filter(AuditLog.created_at >= caps.since()))
        .where(AuditLog.entity_type == "employee", AuditLog.entity_id == emp.id,
               AuditLog.action == "staff.invite_resent")
    )).one()
    if last is not None and (utcnow() - last).total_seconds() < INVITE_RESEND_SECONDS:
        return redirect(back, "An invitation was re-sent a moment ago - please wait a little before sending another.",
                        "info")
    if int(recent or 0) >= STAFF_INVITE_RESENDS_PER_DAY:
        return redirect(back, f"This invitation has been re-sent {STAFF_INVITE_RESENDS_PER_DAY} times in the last 24 "
                              "hours - the most for one day. Ask them to check their spam folder, or try again "
                              "tomorrow.", "warning")
    await S.send_staff_invite(db, emp, emp.gym, resend=True)
    await audit(db, owner, "staff.invite_resent", "employee", emp.id, gym_id=emp.gym_id, request=request, detail={
        "summary": f"New link sent to {emp.invite_email}; earlier links no longer work."})
    await db.commit()
    return redirect(f"/owner/staff/{emp.id}", f"A fresh invitation was sent to {emp.invite_email}. Earlier links no longer work.")


@router.post("/owner/staff/{employee_id}/recheck")
async def staff_recheck(request: Request, employee_id: IdParam, db: DB, owner: OwnerUser) -> Any:
    emp = await _load_employee(db, owner, employee_id)
    if emp.is_deleted or emp.status in (EmploymentStatus.SUSPENDED, EmploymentStatus.TERMINATED):
        return redirect(f"/owner/staff/{emp.id}", "Reactivate the staff member before re-checking.", "warning")
    await audit(db, owner, "staff.recheck", "employee", emp.id, gym_id=emp.gym_id, request=request,
                detail={"summary": "Requested by the gym admin"})
    needs_ocr = emp.is_trainer and emp.verification_status != VerificationStatus.VERIFIED and any(
        d.doc_type == "certification" for d in emp.documents)
    if needs_ocr:
        # OCR again what did not produce a match (engines may have recovered) - bounded per record and charged to the
        # owner's AI 'vision' quota (app/services/ocr.py reread_documents); refused: nothing is cleared or queued
        from app.services.ocr import reread_documents

        reread = await reread_documents([d for d in emp.documents if d.doc_type == "certification"
                                         and not (d.assessment or {}).get("name_match")], noun="certificate")
        if reread.refusal is not None:
            return redirect(f"/owner/staff/{emp.id}", reread.refusal, "warning")
        await _queue_activation_check(db, emp)
        await db.commit()
        return redirect(f"/owner/staff/{emp.id}",
                        "Re-check queued - the certificate is being read again. Results appear here shortly.", "info")
    result = await S.run_activation_check(db, emp.id)
    await db.commit()
    if result is not None and result.new_status == EmploymentStatus.ACTIVE:
        return redirect(f"/owner/staff/{emp.id}", f"{emp.full_name} is approved and active.")
    missing = ", ".join(r.label.lower() for r in (result.missing if result else []))
    return redirect(f"/owner/staff/{emp.id}", f"Still waiting for: {missing}." if missing else "Checked.", "info")


@router.post("/owner/staff/{employee_id}/suspend")
async def staff_suspend(request: Request, employee_id: IdParam, db: DB, owner: OwnerUser) -> Any:
    emp = await get_owned_employee(db, employee_id, owner, joinedload(Employee.gym), joinedload(Employee.user))
    if emp.is_deleted or emp.status not in (EmploymentStatus.ACTIVE, EmploymentStatus.PENDING_VERIFICATION,
                                            EmploymentStatus.INVITED):
        return redirect(f"/owner/staff/{emp.id}", "Only current staff can be suspended.", "warning")
    form = await request.form()
    reason = S.one_line(S.form_str(form, "reason"))[:255]
    old = emp.status
    emp.status = EmploymentStatus.SUSPENDED
    user = _linked_user(emp)
    if user is not None:
        await notify(db, user, f"Your staff access at {emp.gym.name} is suspended",
                     (f"Reason given: {reason}. " if reason else "") + "Your record and schedule are kept. "
                     "Please contact your gym admin if you have questions.",
                     category=NotificationCategory.STAFF, severity=Severity.WARNING, link="/staff")
    await audit(db, owner, "staff.suspend", "employee", emp.id, gym_id=emp.gym_id, request=request, detail={
        "from": old.value, "reason": reason, "summary": reason or "No reason given"})
    await db.commit()
    return redirect(f"/owner/staff/{emp.id}", f"{emp.full_name} is suspended.", "warning")


@router.post("/owner/staff/{employee_id}/reactivate")
async def staff_reactivate(request: Request, employee_id: IdParam, db: DB, owner: OwnerUser) -> Any:
    emp = await _load_employee(db, owner, employee_id)
    if emp.is_deleted or emp.status != EmploymentStatus.SUSPENDED:
        return redirect(f"/owner/staff/{emp.id}", "Only suspended staff can be reactivated.", "info")
    user = _linked_user(emp)
    # the person, then the row (the trust & safety lock order): a ban being applied to this person at this moment
    # commits first and is seen below - a disabled / banned account's job is never reactivated (its owner is told
    # only that the account is disabled, never by whom or why)
    await TS.lock_person(db, await TS.person_of_employee(db, emp))
    row = await TS.lock_employee(db, emp.id)
    if row is None or row.deleted_at is not None or row.status != EmploymentStatus.SUSPENDED:
        return redirect(f"/owner/staff/{emp.id}", "Only suspended staff can be reactivated.", "info")
    if user is not None:
        await db.refresh(user, ["is_active"])
    if await account_disabled(db, user):
        return redirect(f"/owner/staff/{emp.id}", ACCOUNT_DISABLED, "warning")
    missing = [r for r in S.activation_requirements(emp, user, emp.documents) if not r.met]
    emp.status = S.target_status(user, missing)
    emp.verification_summary = S.requirements_summary(missing)
    if missing:
        await _queue_activation_check(db, emp)
    if user is not None:
        body = ("Your access is restored." if not missing else
                "Your record is restored. Still needed before you can work: "
                + ", ".join(r.label.lower() for r in missing) + ".")
        await notify(db, user, f"Your staff access at {emp.gym.name} is restored", body,
                     category=NotificationCategory.STAFF, severity=Severity.SUCCESS, link="/staff")
    await audit(db, owner, "staff.reactivate", "employee", emp.id, gym_id=emp.gym_id, request=request, detail={
        "to": emp.status.value, "summary": f"Now {emp.status.value.replace('_', ' ')}"})
    await db.commit()
    msg = (f"{emp.full_name} is active again." if not missing
           else f"{emp.full_name} is reactivated and pending verification.")
    return redirect(f"/owner/staff/{emp.id}", msg)


# ---------------------------------------------------------------------- ending an employment (trust & safety)
def _removal_ctx(emp: Employee, gym: Gym, *, values: dict[str, Any] | None = None,
                 errors: dict[str, str] | None = None, blockers: list[str] | None = None) -> dict[str, Any]:
    return {"S": S, "TS": TS, "emp": emp, "gym": gym, "values": values or {"reason": "", "note": ""},
            "errors": errors or {}, "blockers": blockers or [], "note_min": TS.NOTE_MIN, "note_max": TS.NOTE_MAX}


@router.get("/owner/staff/{employee_id}/remove")
async def staff_remove_form(request: Request, employee_id: IdParam, db: DB, owner: OwnerUser) -> Any:
    emp = await get_owned_employee(db, employee_id, owner, joinedload(Employee.gym))
    if emp.is_deleted or emp.status not in CURRENT:
        return redirect(f"/owner/staff/{emp.id}", "This employment has already ended.", "info")
    return render(request, "owner/staff/remove.html",
                  _removal_ctx(emp, emp.gym, blockers=await TS.payroll_blockers(db, emp)))


@router.post("/owner/staff/{employee_id}/remove")
async def staff_remove(request: Request, employee_id: IdParam, db: DB, owner: OwnerUser) -> Any:
    """End the employment with a reason from the fixed list. Access ends at once; a BENIGN reason waits for payroll
    (salary + final settlement paid, as before), a VIOLATION / SEVERE one never waits - their pay stays owed in Payroll."""
    emp = await get_owned_employee(db, employee_id, owner, joinedload(Employee.gym))
    gym = emp.gym
    if emp.is_deleted or emp.status not in CURRENT:
        return redirect(f"/owner/staff/{emp.id}", "This employment has already ended.", "info")
    form = await request.form()
    reason = S.form_str(form, "reason")[:40]
    note = TS.clean_note(S.form_str(form, "note"))
    values = {"reason": reason, "note": note[: TS.NOTE_MAX + 50]}
    errors = TS.validate_removal(reason, note)
    if S.form_str(form, "confirm") != "1":
        errors["confirm"] = "Tick the box to confirm - ending an employment can't be undone."
    r = TS.REMOVAL_REASONS.get(reason)
    blockers = await TS.payroll_blockers(db, emp)
    if "reason" not in errors and r is not None and r.category == TS.BENIGN and blockers:
        errors["reason"] = ("Their pay isn't settled yet, so this employment can't end for this reason. Pay what is owed "
                            "first (Settlement & removal)" + (" - or use Change position to move them to a new role."
                                                              if r.code == TS.POSITION_CHANGE else "."))
    if errors or r is None:
        return render(request, "owner/staff/remove.html",
                      _removal_ctx(emp, gym, values=values, errors=errors, blockers=blockers), status_code=400)
    person = await TS.person_of_employee(db, emp)
    await TS.lock_person(db, person)  # the person first, then the row (app/services/trust_safety.py lock order)
    row = await TS.lock_employee(db, emp.id)
    if row is None or row.deleted_at is not None or row.status not in CURRENT:  # a double submit lands here
        return redirect(f"/owner/staff/{employee_id}", "This employment has already ended.", "info")
    # a BENIGN ending after payroll was settled keeps the last working day the final settlement paid up to
    last_day = await _settled_last_day(db, emp) if r.category == TS.BENIGN else None
    outcome = await TS.end_employment(db, emp, reason=r.code, note=note, actor=owner, gym=gym, person=person,
                                      request=request, termination_date=last_day)
    await db.commit()
    name = emp.full_name
    if r.category == TS.BENIGN:
        msg, tone = f"{name}'s employment has ended ({r.label}). They can be added again later.", "success"
    elif r.category == TS.VIOLATION:
        msg, tone = (f"{name}'s employment has ended for a conduct violation ({r.label}). Your gyms can't add them "
                     "again, and the record is shared with the IronPulse platform team."), "warning"
    else:
        msg, tone = (f"{name}'s employment has ended for a severe violation ({r.label}). "
                     + ("Their IronPulse account is banned and " if outcome.ban is not None else "")
                     + "the platform team has been alerted."), "warning"
    if blockers and r.category != TS.BENIGN:
        msg += " Salary they are still owed must be paid from Payroll."
    return redirect(f"/owner/staff/{employee_id}", msg, tone)


async def _settled_last_day(db: Any, emp: Employee) -> Any:
    """The last working day recorded by the employee's final settlement (payroll is paid up to it) - None without one."""
    from datetime import date

    from app.services import payroll as pr

    fs = await pr.final_settlement_of(db, emp.id)
    settlement = (fs.rules or {}).get("settlement") if fs is not None else None
    try:
        return date.fromisoformat(settlement["termination_date"]) if settlement else None
    except (KeyError, TypeError, ValueError):
        return None


def _position_email(emp: Employee) -> str:
    """The invitation of the new position goes to the address of the account they already use (if linked)."""
    user = _linked_user(emp)
    return (user.email if user is not None and user.email else emp.invite_email or "").strip().lower()


def _position_ctx(emp: Employee, gym: Gym, vb: int, values: dict[str, Any], errors: dict[str, str]) -> dict[str, Any]:
    return {"S": S, "TS": TS, "emp": emp, "gym": gym, "verified_branches": vb, "values": values, "errors": errors,
            "current_label": _role_label(emp)}


async def _copy_public_image(key: str) -> storage.StoredFile:
    """A new public file with the same picture (a record never shares a stored file with another one: deleting or
    replacing one record's photo must not break the other's)."""
    import asyncio

    data = await asyncio.to_thread(storage.path_for(key).read_bytes)
    return await storage.save_public_image(data, kind="staff", max_side=1200)


@router.get("/owner/staff/{employee_id}/change-position")
async def staff_change_position_form(request: Request, employee_id: IdParam, db: DB, owner: OwnerUser) -> Any:
    emp = await _load_employee(db, owner, employee_id)
    if emp.is_deleted or emp.status not in POSITION_CHANGEABLE:
        return redirect(f"/owner/staff/{emp.id}", "Only current staff who aren't suspended can change position - "
                        "reactivate them first, or end the employment.", "warning")
    vb = await S.verified_branch_count(db, emp.gym_id)
    values = S.staff_values_from_employee(emp)
    values.update(invite_email=_position_email(emp), hire_date=local_today(owner.timezone).isoformat())
    return render(request, "owner/staff/change_position.html", _position_ctx(emp, emp.gym, vb, values, {}))


@router.post("/owner/staff/{employee_id}/change-position")
async def staff_change_position(request: Request, employee_id: IdParam, db: DB, owner: OwnerUser) -> Any:
    """End this employment ('Position change' - benign, they may always rejoin) and re-add the same person in the new
    role, in ONE transaction: a new record, the normal invitation and the verification that role needs (certifications
    may differ). The old record stays in the history; the person is told to accept the new invitation."""
    emp = await _load_employee(db, owner, employee_id)
    gym = emp.gym
    if emp.is_deleted or emp.status not in POSITION_CHANGEABLE:
        return redirect(f"/owner/staff/{emp.id}", "Only current staff who aren't suspended can change position - "
                        "reactivate them first, or end the employment.", "warning")
    form = await request.form()
    vb = await S.verified_branch_count(db, gym.id)
    email = _position_email(emp)
    data, errors, values = S.validate_staff_form(form, verified_branches=vb, default_currency=gym.currency,
                                                 locked_email=email)
    if data is not None:
        if data.role == emp.role and (data.role_title or None) == (emp.role_title or None):
            errors["role"] = "Choose the new position - this is their current one."
        if data.hire_date < emp.hire_date:
            errors["hire_date"] = "The new position can't start before the current one did."
        refusal = await TS.rehire_refusal(db, owner_id=owner.id, email=email)
        if refusal:
            errors["invite_email"] = refusal
    if sum(len(S.form_files(form, k)) for k in ("certification_files", "resume_file", "contract_file")) > MAX_DOCS_PER_UPLOAD:
        errors["certification_files"] = f"Upload at most {MAX_DOCS_PER_UPLOAD} documents at once."
    if errors or data is None:
        return render(request, "owner/staff/change_position.html", _position_ctx(emp, gym, vb, values, errors),
                      status_code=400)

    saved: list[str] = []
    docs: list[tuple[str, storage.StoredFile]] = []
    current = "photo"
    try:
        photo = form.get("photo")
        stored_photo = (await storage.save_public_image(photo, kind="staff", max_side=1200)  # type: ignore[arg-type]
                        if S.has_file(photo) else await _copy_public_image(emp.photo_key))
        saved.append(stored_photo.key)
        for field_name, doc_type in (("certification_files", "certification"), ("resume_file", "resume"),
                                     ("contract_file", "contract")):
            current = field_name
            for upload in S.form_files(form, field_name):
                stored, _plain = await storage.save_private_file(upload, kind="staff_docs")
                saved.append(stored.key)
                docs.append((doc_type, stored))
    except (storage.UploadError, OSError) as exc:
        await _delete_keys(saved)
        errors[current] = (str(exc) if isinstance(exc, storage.UploadError)
                           else "Their current photo couldn't be copied - please upload a staff photo.")
        return render(request, "owner/staff/change_position.html", _position_ctx(emp, gym, vb, values, errors),
                      status_code=400)

    person = await TS.person_of_employee(db, emp)
    await TS.lock_person(db, person)
    row = await TS.lock_employee(db, emp.id)
    if row is None or row.deleted_at is not None or row.status not in POSITION_CHANGEABLE:  # double submit
        await _delete_keys(saved)
        return redirect(f"/owner/staff/{employee_id}", "This employment has already ended - the position change was "
                        "not applied again.", "info")
    # the re-hire check again, now that the person's lock is held: a violation / ban committed while this request
    # waited for it (another of their jobs ended for a violation at the same moment) refuses the re-add
    refusal = await TS.rehire_refusal(db, owner_id=owner.id, email=email)
    if refusal:  # nothing was written: the request's session rolls back (and releases the locks) when it closes
        await _delete_keys(saved)
        errors["invite_email"] = refusal
        return render(request, "owner/staff/change_position.html", _position_ctx(emp, gym, vb, values, errors),
                      status_code=400)
    try:  # net zero current staff; one more unanswered invitation unless the old record was one
        await caps.enforce(db, "staff_invites_per_gym", gym.id, _pending_invite_rows(gym.id),
                           adding=0 if row.status == EmploymentStatus.INVITED else 1)
    except caps.CapReached as exc:
        await _delete_keys(saved)
        errors["role"] = str(exc)
        return render(request, "owner/staff/change_position.html", _position_ctx(emp, gym, vb, values, errors),
                      status_code=exc.status_code)
    if (over := await _reserve_doc_storage(db, gym.id, docs)) is not None:  # the gym's storage quota
        await _delete_keys(saved)
        errors[over[0]] = over[1]
        return render(request, "owner/staff/change_position.html", _position_ctx(emp, gym, vb, values, errors),
                      status_code=409)

    old_label = _role_label(emp)
    new_label = data.role_title or S.ROLE_LABELS.get(data.role.value, "Staff")
    today = local_today(owner.timezone)
    last_day = max(emp.hire_date, min(today, data.hire_date - timedelta(days=1)))
    await TS.end_employment(db, emp, reason=TS.POSITION_CHANGE, note=f"Re-added as {new_label}", actor=owner, gym=gym,
                            person=person, request=request, termination_date=last_day, notify_person=False)
    is_trainer = data.role == EmployeeRole.TRAINER
    has_cert = any(t == "certification" for t, _ in docs)
    new = Employee(
        gym_id=gym.id, invite_email=data.invite_email, full_name=data.full_name, phone=data.phone, role=data.role,
        role_title=data.role_title, is_trainer=is_trainer, work_mode=data.work_mode,
        employment_type=data.employment_type, country_code=data.country_code, currency=data.currency,
        monthly_salary=data.monthly_salary, hire_date=data.hire_date, status=EmploymentStatus.INVITED,
        photo_key=stored_photo.key, bio=data.bio, specialties=data.specialties, certifications=data.certifications,
        years_experience=data.years_experience, is_public=data.is_public,
        verification_status=VerificationStatus.QUEUED if (is_trainer and has_cert) else VerificationStatus.DRAFT,
        previous_employment_id=emp.id,
    )
    db.add(new)
    await db.flush()
    for doc_type, stored in docs:
        db.add(EmployeeDocument(
            employee_id=new.id, doc_type=doc_type, file_key=stored.key, original_filename=stored.original_filename,
            content_type=stored.content_type, sha256=stored.sha256, size_bytes=stored.size,
        ))
    await S.send_staff_invite(db, new, gym)
    if is_trainer and has_cert:
        await _queue_activation_check(db, new)
    user = _linked_user(emp)
    if user is not None:
        await notify(db, user, f"Your position at {gym.name} is changing to {new_label}",
                     f"Your position at {gym.name} is changing from {old_label} to {new_label}. Accept the new "
                     f"invitation we e-mailed to {data.invite_email} to start in the new role - it goes through the "
                     "usual checks for that role. Your previous position has ended.",
                     category=NotificationCategory.STAFF, severity=Severity.INFO, link="/staff")
    await audit(db, owner, "staff.position_change", "employee", emp.id, gym_id=gym.id, request=request, detail={
        "summary": f"{old_label} → {new_label} (new record #{new.id})", "new_employee_id": new.id,
        "from_role": emp.role.value, "to_role": data.role.value})
    await audit(db, owner, "staff.create", "employee", new.id, gym_id=gym.id, request=request, detail={
        "summary": f"Re-added as {new_label} after a position change (previous record #{emp.id})",
        "email": new.invite_email, "role": new.role.value, "previous_employment_id": emp.id})
    await db.commit()
    return redirect(f"/owner/staff/{new.id}",
                    f"Position changed: {old_label} → {new_label}. Positions are never edited in place, so the previous "
                    f"employment ended (they can always rejoin) and {data.full_name} was invited to accept the new "
                    "one - it goes through the verification the new role needs. The old record stays in the history.")


# ---------------------------------------------------------------------- documents
@router.get("/owner/staff/{employee_id}/documents")
async def staff_documents_redirect(employee_id: IdParam) -> RedirectResponse:
    return RedirectResponse(f"/owner/staff/{employee_id}?tab=documents", status_code=303)


@router.post("/owner/staff/{employee_id}/documents")
async def staff_document_upload(request: Request, employee_id: IdParam, db: DB, owner: OwnerUser) -> Any:
    emp = await _load_employee(db, owner, employee_id)
    if emp.is_deleted or emp.status == EmploymentStatus.TERMINATED:
        return redirect(f"/owner/staff/{emp.id}?tab=documents", "Former staff records are read-only.", "warning")
    form = await request.form()
    doc_type = S.form_str(form, "doc_type")
    upload = form.get("document")
    errors: list[str] = []
    if doc_type not in S.DOC_TYPE_LABELS:
        errors.append("Choose the document type.")
    if not S.has_file(upload):
        errors.append("Choose a file to upload.")
    if len(emp.documents) >= caps.limit("employee_documents"):  # quick answer; the race-safe check is below
        errors.append(str(caps.CapReached("employee_documents")))
    stored = None
    if not errors:
        try:
            stored, _plain = await storage.save_private_file(upload, kind="staff_docs")  # type: ignore[arg-type]
        except storage.UploadError as exc:
            errors.append(str(exc))
    if not errors and stored is not None:
        try:  # under the record's cap lock until COMMIT: two uploads at once can't both pass the last free slot
            await caps.enforce(db, "employee_documents", emp.id,
                               select(EmployeeDocument.id).where(EmployeeDocument.employee_id == emp.id))
            await quotas.reserve_gym(db, emp.gym_id, stored.size)  # then the gym's storage quota (wider scope)
        except caps.CapReached as exc:
            await storage.delete_key(stored.key)
            errors.append(str(exc))
    if errors or stored is None:
        return await _render_detail(request, db, emp, "documents", doc_errors=errors, status_code=400)
    db.add(EmployeeDocument(employee_id=emp.id, doc_type=doc_type, file_key=stored.key,
                            original_filename=stored.original_filename, content_type=stored.content_type,
                            sha256=stored.sha256, size_bytes=stored.size))
    label = S.DOC_TYPE_LABELS[doc_type]
    if doc_type == "certification" and emp.is_trainer and emp.verification_status != VerificationStatus.VERIFIED:
        emp.verification_status = VerificationStatus.QUEUED
        await _queue_activation_check(db, emp)
    await audit(db, owner, "staff.document_add", "employee", emp.id, gym_id=emp.gym_id, request=request, detail={
        "doc_type": doc_type, "summary": f"{label}: {stored.original_filename}"})
    user = _linked_user(emp)
    if user is not None:
        await notify(db, user, f"{label} added to your staff record",
                     f"{emp.gym.name} added a document ({label.lower()}) to your staff record.",
                     category=NotificationCategory.ACCOUNT, severity=Severity.INFO, link="/staff")
    await db.commit()
    extra = " It is being checked automatically." if doc_type == "certification" and emp.is_trainer else ""
    return redirect(f"/owner/staff/{emp.id}?tab=documents", f"{label} uploaded.{extra}")


@router.get("/owner/staff/{employee_id}/documents/{doc_id}")
async def staff_document_download(employee_id: IdParam, doc_id: IdParam, db: DB, owner: OwnerUser) -> Response:
    emp = await get_owned_employee(db, employee_id, owner)
    doc = await db.scalar(select(EmployeeDocument).where(EmployeeDocument.id == doc_id,
                                                         EmployeeDocument.employee_id == emp.id))
    if doc is None:
        raise NotFound("Document not found.")
    try:
        data = await storage.load_private(doc.file_key)
    except (storage.UploadError, OSError):
        raise NotFound("This file is no longer available.") from None
    return _private_response(data, doc.content_type, doc.original_filename)


@router.post("/owner/staff/{employee_id}/documents/{doc_id}/delete")
async def staff_document_delete(request: Request, employee_id: IdParam, doc_id: IdParam, db: DB, owner: OwnerUser) -> Any:
    emp = await _load_employee(db, owner, employee_id)
    doc = next((d for d in emp.documents if d.id == doc_id), None)
    if doc is None:
        raise NotFound("Document not found.")
    await db.execute(delete(EmployeeDocument).where(EmployeeDocument.id == doc.id))
    if doc.doc_type == "certification" and emp.is_trainer:
        remaining = [d for d in emp.documents if d.doc_type == "certification" and d.id != doc.id]
        if not remaining:
            emp.verification_status = VerificationStatus.DRAFT
            emp.verified_at = None
        elif (emp.verification_status == VerificationStatus.VERIFIED and (doc.assessment or {}).get("name_match")
              and not any((d.assessment or {}).get("name_match") for d in remaining)):
            emp.verification_status = VerificationStatus.QUEUED
        if emp.status in (EmploymentStatus.ACTIVE, EmploymentStatus.PENDING_VERIFICATION):
            await _queue_activation_check(db, emp)
    await audit(db, owner, "staff.document_delete", "employee", emp.id, gym_id=emp.gym_id, request=request, detail={
        "doc_type": doc.doc_type,
        "summary": f"{S.DOC_TYPE_LABELS.get(doc.doc_type, doc.doc_type)}: {doc.original_filename}"})
    await db.commit()
    await storage.delete_key(doc.file_key)
    return redirect(f"/owner/staff/{emp.id}?tab=documents", "Document deleted.")


@router.post("/owner/staff/{employee_id}/verify-documents")
async def staff_documents_approve(request: Request, employee_id: IdParam, db: DB, owner: OwnerUser) -> Any:
    emp = await _load_employee(db, owner, employee_id)
    certs = [d for d in emp.documents if d.doc_type == "certification"]
    if not emp.is_trainer or not certs:
        return redirect(f"/owner/staff/{emp.id}?tab=documents", "Upload a certification first.", "warning")
    if emp.verification_status == VerificationStatus.VERIFIED:
        return redirect(f"/owner/staff/{emp.id}?tab=documents", "The certification is already verified.", "info")
    now = utcnow()
    for d in certs:
        assessment = dict(d.assessment or {})
        if not assessment.get("name_match"):
            assessment.update({"name_match": True, "method": "manual", "reviewer_id": owner.id,
                               "checked_at": now.isoformat()})
            d.assessment = assessment
    emp.verification_status = VerificationStatus.VERIFIED
    emp.verified_at = now
    db.add(VerificationRun(employee_id=emp.id, outcome=VerificationStatus.VERIFIED, manual=True, reviewer_id=owner.id,
                           score=1.0, summary="Approved by the gym admin after reviewing the certificate.",
                           checks=[{"check": "Manual review", "passed": True, "detail": f"Approved by {owner.full_name}"}]))
    await audit(db, owner, "staff.documents_approved", "employee", emp.id, gym_id=emp.gym_id, request=request,
                detail={"summary": f"{len(certs)} certification document(s) approved manually"})
    result = None
    if emp.status in (EmploymentStatus.INVITED, EmploymentStatus.PENDING_VERIFICATION, EmploymentStatus.ACTIVE):
        result = await S.run_activation_check(db, emp.id)
    await db.commit()
    if result is not None and result.new_status == EmploymentStatus.ACTIVE and result.old_status != EmploymentStatus.ACTIVE:
        return redirect(f"/owner/staff/{emp.id}", f"Certification approved - {emp.full_name} is now active.")
    return redirect(f"/owner/staff/{emp.id}?tab=documents", "Certification approved.")


# ---------------------------------------------------------------------- shifts
@router.get("/owner/staff/{employee_id}/shifts")
async def staff_shifts_redirect(employee_id: IdParam) -> RedirectResponse:
    return RedirectResponse(f"/owner/staff/{employee_id}?tab=schedule", status_code=303)


@router.post("/owner/staff/{employee_id}/shifts")
async def staff_shift_add(request: Request, employee_id: IdParam, db: DB, owner: OwnerUser) -> Any:
    emp = await _load_employee(db, owner, employee_id)
    if emp.is_deleted or emp.status == EmploymentStatus.TERMINATED:
        return redirect(f"/owner/staff/{emp.id}?tab=schedule", "Shifts can't be added to former staff.", "warning")
    form = await request.form()
    req, errors, values = S.parse_shift_form(form)
    rows: list[TrainerShift] = []
    if req is not None:
        rows, errors = await S.validate_shift_request(db, emp, req)
    if not errors and rows:
        try:  # race-safe: under the record's cap lock until COMMIT
            await caps.enforce(db, "shifts_per_employee", emp.id,
                               select(TrainerShift.id).where(TrainerShift.employee_id == emp.id), adding=len(rows))
        except caps.CapReached as exc:
            errors = [str(exc)]
    if errors or req is None:
        return await _render_detail(request, db, emp, "schedule", shift_errors=errors, shift_values=values,
                                    status_code=400)
    db.add_all(rows)
    where = "online"
    if req.mode == ShiftMode.OFFLINE and req.branch_id is not None:
        branch = await db.get(Branch, req.branch_id)
        where = branch.name if branch is not None else "branch"
    days = ", ".join(S.WEEKDAYS_SHORT[d] for d in req.weekdays)
    summary = f"{days} {req.start:%H:%M}–{req.end:%H:%M} · {where}"
    await audit(db, owner, "staff.shift_add", "employee", emp.id, gym_id=emp.gym_id, request=request,
                detail={"summary": summary, "weekdays": req.weekdays})
    user = _linked_user(emp)
    if user is not None:
        await notify(db, user, f"New shifts at {emp.gym.name}", f"Added to your weekly schedule: {summary}.",
                     category=NotificationCategory.STAFF, severity=Severity.INFO, link="/staff/schedule")
    await db.commit()
    return redirect(f"/owner/staff/{emp.id}?tab=schedule",
                    f"Added {len(rows)} shift{'s' if len(rows) != 1 else ''}: {summary}.")


@router.post("/owner/staff/{employee_id}/shifts/{shift_id}/delete")
async def staff_shift_delete(request: Request, employee_id: IdParam, shift_id: IdParam, db: DB, owner: OwnerUser) -> Any:
    emp = await get_owned_employee(db, employee_id, owner, joinedload(Employee.gym), joinedload(Employee.user))
    shift = await db.scalar(select(TrainerShift).options(joinedload(TrainerShift.branch))
                            .where(TrainerShift.id == shift_id, TrainerShift.employee_id == emp.id))
    if shift is None:
        raise NotFound("Shift not found.")
    summary = (f"{S.WEEKDAYS[shift.weekday]} {shift.start_time:%H:%M}–{shift.end_time:%H:%M} · "
               f"{shift.branch.name if shift.branch else 'online'}")
    await db.execute(delete(TrainerShift).where(TrainerShift.id == shift.id))
    await audit(db, owner, "staff.shift_delete", "employee", emp.id, gym_id=emp.gym_id, request=request,
                detail={"summary": summary})
    user = _linked_user(emp)
    if user is not None:
        await notify(db, user, f"Schedule change at {emp.gym.name}",
                     f"This shift was removed from your schedule: {summary}.",
                     category=NotificationCategory.STAFF, severity=Severity.INFO, link="/staff/schedule")
    await db.commit()
    return redirect(f"/owner/staff/{emp.id}?tab=schedule", f"Removed {summary}.")


# ---------------------------------------------------------------------- trainer clients
@router.get("/owner/staff/{employee_id}/clients")
async def staff_clients_redirect(employee_id: IdParam) -> RedirectResponse:
    return RedirectResponse(f"/owner/staff/{employee_id}?tab=clients", status_code=303)


@router.post("/owner/staff/{employee_id}/clients")
async def staff_client_assign(request: Request, employee_id: IdParam, db: DB, owner: OwnerUser) -> Any:
    emp = await _load_employee(db, owner, employee_id)
    form = await request.form()
    errors: list[str] = []
    sub = None
    mode: str | None = None
    if not emp.is_trainer:
        errors.append("Only trainers can be assigned clients.")
    elif emp.status != EmploymentStatus.ACTIVE or emp.is_deleted:
        errors.append(f"{emp.full_name} must be approved (Active) before clients can be assigned.")
    elif await account_disabled(db, _linked_user(emp)):
        errors.append("This person's IronPulse account is disabled, so they can't be given clients. Contact IronPulse "
                      "support if you have questions.")
    else:
        sub_id = _id(S.form_str(form, "subscription_id"))
        sub = await S.eligible_subscription(db, emp, sub_id) if sub_id is not None else None
        if sub is None:
            errors.append("Choose a member with an active trainer-included plan at this gym.")
        else:
            modes = S.compatible_modes(sub.plan.mode, emp.work_mode)
            wanted = S.form_str(form, "mode")
            if not modes:
                errors.append(f"The member's {sub.plan.mode.value} plan doesn't match this trainer's work mode "
                              f"({S.WORK_MODE_LABELS[emp.work_mode.value].lower()}).")
            elif wanted and wanted not in modes:
                errors.append(f"A {sub.plan.mode.value} plan with this trainer can only be coached "
                              + " or ".join(S.SHIFT_MODE_LABELS[m].lower() for m in modes) + ".")
            else:
                mode = wanted or modes[0]
    if errors or sub is None or mode is None:
        if not emp.is_trainer:  # there is no clients tab for this role
            return await _render_detail(request, db, emp, "overview", page_error=errors[0], status_code=400)
        return await _render_detail(request, db, emp, "clients", client_errors=errors, status_code=400)

    links = list((await db.execute(select(TrainerClient).where(TrainerClient.subscription_id == sub.id))).scalars().all())
    mine = next((lk for lk in links if lk.employee_id == emp.id), None)
    previous_ids = [lk.employee_id for lk in links if lk.employee_id != emp.id and lk.active]
    for lk in links:
        if lk.employee_id != emp.id:
            lk.active = False
    if mine is None:
        db.add(TrainerClient(employee_id=emp.id, customer_id=sub.customer_id, subscription_id=sub.id,
                             mode=ShiftMode(mode), active=True))
    else:
        mine.active = True
        mine.mode = ShiftMode(mode)
    sub.trainer_id = emp.id
    customer = sub.customer
    mode_text = "at the branch" if mode == ShiftMode.OFFLINE.value else "online"
    await notify(db, customer, f"Meet your trainer: {emp.full_name}",
                 f"{emp.full_name} ({_role_label(emp)}) at {emp.gym.name} is now your trainer - coaching {mode_text}. "
                 "You can message them in Chat.", category=NotificationCategory.STAFF, severity=Severity.SUCCESS,
                 link="/chat")
    trainer_user = _linked_user(emp)
    if trainer_user is not None:
        await notify(db, trainer_user, f"New client: {customer.display_name}",
                     f"{customer.display_name} ({sub.plan.name}) was assigned to you - coaching {mode_text}. "
                     "Check their goals and health notes before the first session.",
                     category=NotificationCategory.STAFF, severity=Severity.INFO, link=f"/staff/clients/{customer.id}")
    if previous_ids:
        prev = (await db.execute(select(Employee).options(joinedload(Employee.user))
                                 .where(Employee.id.in_(previous_ids)))).unique().scalars().all()
        for p in prev:
            pu = _linked_user(p)
            if pu is not None:
                await notify(db, pu, f"Client reassigned: {customer.display_name}",
                             f"{customer.display_name} is now coached by another trainer at {emp.gym.name}.",
                             category=NotificationCategory.STAFF, severity=Severity.INFO, link="/staff/clients")
    await audit(db, owner, "staff.client_assign", "employee", emp.id, gym_id=emp.gym_id, request=request, detail={
        "customer_id": customer.id, "subscription_id": sub.id, "mode": mode,
        "summary": f"{customer.display_name} · {sub.plan.name} · {mode_text}"})
    await db.commit()
    return redirect(f"/owner/staff/{emp.id}?tab=clients", f"{customer.display_name} is now coached by {emp.full_name}.")


@router.post("/owner/staff/{employee_id}/clients/{link_id}/remove")
async def staff_client_remove(request: Request, employee_id: IdParam, link_id: IdParam, db: DB, owner: OwnerUser) -> Any:
    emp = await get_owned_employee(db, employee_id, owner, joinedload(Employee.gym), joinedload(Employee.user))
    link = await db.scalar(select(TrainerClient).options(joinedload(TrainerClient.customer))
                           .where(TrainerClient.id == link_id, TrainerClient.employee_id == emp.id))
    if link is None:
        raise NotFound("Client not found.")
    if not link.active:
        return redirect(f"/owner/staff/{emp.id}?tab=clients", "That client is already unassigned.", "info")
    link.active = False
    if link.subscription_id:
        await db.execute(update(Subscription).where(Subscription.id == link.subscription_id,
                                                    Subscription.trainer_id == emp.id).values(trainer_id=None))
    customer = link.customer
    await notify(db, customer, "Trainer change", f"{emp.full_name} is no longer your trainer at {emp.gym.name}. "
                 "The gym will assign a new trainer for your plan.", category=NotificationCategory.STAFF,
                 severity=Severity.INFO, link="/chat")
    trainer_user = _linked_user(emp)
    if trainer_user is not None:
        await notify(db, trainer_user, f"Client unassigned: {customer.display_name}",
                     f"{customer.display_name} is no longer assigned to you.", category=NotificationCategory.STAFF,
                     severity=Severity.INFO, link="/staff/clients")
    await audit(db, owner, "staff.client_remove", "employee", emp.id, gym_id=emp.gym_id, request=request,
                detail={"customer_id": customer.id, "summary": customer.display_name})
    await db.commit()
    return redirect(f"/owner/staff/{emp.id}?tab=clients", f"{customer.display_name} was unassigned.")


# ====================================================================== hiring: job postings
async def _owned_job(db: DB, owner: User, job_id: int, *options: Any) -> JobPosting:
    stmt = (select(JobPosting).join(Gym, Gym.id == JobPosting.gym_id).options(contains_eager(JobPosting.gym), *options)
            .where(JobPosting.id == job_id, Gym.owner_id == owner.id))
    job = (await db.execute(stmt)).unique().scalar_one_or_none()
    if job is None:
        raise NotFound("Job posting not found.")
    return job


def _open_job_rows(gym_id: int) -> Any:
    return select(JobPosting.id).where(JobPosting.gym_id == gym_id, JobPosting.is_open.is_(True))


async def _gym_branches(db: DB, gym_id: int) -> list[Branch]:
    """The job form's branch choices: only id, name and city are read (not the whole branch rows)."""
    return list((await db.execute(
        select(Branch).options(load_only(Branch.id, Branch.name, Branch.city))
        .where(Branch.gym_id == gym_id).order_by(Branch.name, Branch.id)
    )).scalars().all())


def _job_form_ctx(gym: Gym, branches: list[Branch], values: dict[str, Any], errors: dict[str, str],
                  job: JobPosting | None = None) -> dict[str, Any]:
    return {"S": S, "gym": gym, "branches": branches, "values": values, "errors": errors, "job": job,
            "enquiry_total": 0, "enquiry_new": 0}


@router.get("/owner/jobs")
async def jobs_list(request: Request, db: DB, owner: OwnerUser, gym: str = "", state: str = "") -> Any:
    gyms = await _owner_gyms(db, owner)
    gym_id = picked if (picked := _id(gym)) in {g.id for g in gyms} else None
    state = state if state in ("open", "closed") else ""
    total = (select(func.count(JobEnquiry.id)).where(JobEnquiry.job_id == JobPosting.id)
             .correlate(JobPosting).scalar_subquery())
    fresh = (select(func.count(JobEnquiry.id)).where(JobEnquiry.job_id == JobPosting.id,
                                                     JobEnquiry.status == EnquiryStatus.NEW)
             .correlate(JobPosting).scalar_subquery())
    stmt = (select(JobPosting, total, fresh).join(Gym, Gym.id == JobPosting.gym_id)
            .options(contains_eager(JobPosting.gym), joinedload(JobPosting.branch))
            .where(Gym.owner_id == owner.id))
    count = select(func.count(JobPosting.id)).join(Gym, Gym.id == JobPosting.gym_id).where(Gym.owner_id == owner.id)
    if gym_id:
        stmt = stmt.where(JobPosting.gym_id == gym_id)
        count = count.where(JobPosting.gym_id == gym_id)
    if state:
        stmt = stmt.where(JobPosting.is_open.is_(state == "open"))
        count = count.where(JobPosting.is_open.is_(state == "open"))
    page = await paginate(db, stmt.order_by(JobPosting.is_open.desc(), JobPosting.created_at.desc(), JobPosting.id.desc()),
                          page=parse_page(request).page, per_page=JOBS_PER_PAGE, count_stmt=count)
    postings = [{"job": job, "total": int(t or 0), "new": int(n or 0)} for job, t, n in page.items]
    owned = select(JobPosting.id).join(Gym, Gym.id == JobPosting.gym_id).where(Gym.owner_id == owner.id)
    open_count = await db.scalar(select(func.count(JobPosting.id)).where(JobPosting.id.in_(owned),
                                                                         JobPosting.is_open.is_(True)))
    enq = dict((await db.execute(select(JobEnquiry.status, func.count(JobEnquiry.id))
                                 .where(JobEnquiry.job_id.in_(owned)).group_by(JobEnquiry.status))).all())
    stats = {"open": int(open_count or 0), "enquiries": int(sum(enq.values())),
             "new": int(enq.get(EnquiryStatus.NEW, 0)), "hired": int(enq.get(EnquiryStatus.HIRED, 0))}
    return render(request, "owner/jobs/list.html", {
        "S": S, "gyms": gyms, "postings": postings, "page": page, "stats": stats,
        "filters": {"gym": gym_id, "state": state},
    })


@router.get("/owner/jobs/new")
async def job_new_pick(request: Request, db: DB, owner: OwnerUser) -> Any:
    gyms = await _owner_gyms(db, owner)
    if not gyms:
        return redirect("/owner/gyms/new", "Create a gym first - job postings belong to a gym.", "info")
    if len(gyms) == 1:
        return RedirectResponse(f"/owner/gyms/{gyms[0].id}/jobs/new", status_code=303)
    return render(request, "owner/staff/choose_gym.html", {
        "gyms": gyms, "heading": "New job posting", "eyebrow": "Hiring", "back": "/owner/jobs", "path_suffix": "jobs/new",
    })


@router.get("/owner/gyms/{gym_id}/jobs/new")
async def job_new_form(request: Request, gym_id: IdParam, db: DB, owner: OwnerUser) -> Any:
    gym = await get_owned_gym(db, gym_id, owner)
    branches = await _gym_branches(db, gym.id)
    return render(request, "owner/jobs/form.html", _job_form_ctx(gym, branches, S.job_defaults(gym), {}))


@router.post("/owner/gyms/{gym_id}/jobs/new")
async def job_create(request: Request, gym_id: IdParam, db: DB, owner: OwnerUser) -> Any:
    gym = await get_owned_gym(db, gym_id, owner)
    branches = await _gym_branches(db, gym.id)
    form = await request.form()
    data, errors, values = S.validate_job_form(form, branches={b.id: b.city for b in branches},
                                               default_currency=gym.currency, creating=True)
    if errors or data is None:
        return render(request, "owner/jobs/form.html", _job_form_ctx(gym, branches, values, errors), status_code=400)
    try:  # race-safe caps (locks held until COMMIT): open postings first, then postings created today
        if data.is_open:
            await caps.enforce(db, "open_jobs_per_gym", gym.id, _open_job_rows(gym.id))
        await caps.enforce(db, "jobs_per_gym_day", gym.id, select(JobPosting.id).where(
            JobPosting.gym_id == gym.id, JobPosting.created_at >= caps.since()))
    except caps.CapReached as exc:
        return render(request, "owner/jobs/form.html", _job_form_ctx(gym, branches, values, {"form": str(exc)}),
                      status_code=exc.status_code)
    job = JobPosting(gym_id=gym.id, branch_id=data.branch_id, title=data.title, role=data.role,
                     employment_type=data.employment_type, location=data.location, salary_min=data.salary_min,
                     salary_max=data.salary_max, currency=data.currency, salary_period=data.salary_period,
                     requirements=data.requirements, description=data.description, is_open=data.is_open)
    db.add(job)
    await db.flush()
    await audit(db, owner, "job.create", "job_posting", job.id, gym_id=gym.id, request=request,
                detail={"title": job.title, "open": job.is_open})
    await db.commit()
    await refresh_nav_state(db)
    msg = ("Job posted - it's live on the public Jobs page." if job.is_open
           else "Job saved as closed - open it when you're ready to receive enquiries.")
    return redirect(f"/owner/jobs/{job.id}", msg)


@router.get("/owner/jobs/{job_id}")
async def job_edit_form(request: Request, job_id: IdParam, db: DB, owner: OwnerUser) -> Any:
    job = await _owned_job(db, owner, job_id)
    branches = await _gym_branches(db, job.gym_id)
    counts = dict((await db.execute(select(JobEnquiry.status, func.count(JobEnquiry.id))
                                    .where(JobEnquiry.job_id == job.id).group_by(JobEnquiry.status))).all())
    ctx = _job_form_ctx(job.gym, branches, S.job_values_from_posting(job), {}, job)
    ctx["enquiry_total"] = sum(counts.values())
    ctx["enquiry_new"] = counts.get(EnquiryStatus.NEW, 0)
    return render(request, "owner/jobs/form.html", ctx)


@router.post("/owner/jobs/{job_id}")
async def job_update(request: Request, job_id: IdParam, db: DB, owner: OwnerUser) -> Any:
    job = await _owned_job(db, owner, job_id)
    branches = await _gym_branches(db, job.gym_id)
    form = await request.form()
    data, errors, values = S.validate_job_form(form, branches={b.id: b.city for b in branches},
                                               default_currency=job.currency, creating=False)
    if errors or data is None:
        return render(request, "owner/jobs/form.html", _job_form_ctx(job.gym, branches, values, errors, job),
                      status_code=400)
    for key in ("branch_id", "title", "role", "employment_type", "location", "salary_min", "salary_max", "currency",
                "salary_period", "requirements", "description"):
        setattr(job, key, getattr(data, key))
    await audit(db, owner, "job.update", "job_posting", job.id, gym_id=job.gym_id, request=request,
                detail={"title": job.title})
    await db.commit()
    await refresh_nav_state(db)
    return redirect(f"/owner/jobs/{job.id}", "Job posting saved.")


@router.post("/owner/jobs/{job_id}/toggle")
async def job_toggle(request: Request, job_id: IdParam, db: DB, owner: OwnerUser) -> Any:
    job = await _owned_job(db, owner, job_id)
    form = await request.form()
    target = "/owner/jobs" if S.form_str(form, "next") == "/owner/jobs" else f"/owner/jobs/{job.id}"
    if not job.is_open:
        try:
            await caps.enforce(db, "open_jobs_per_gym", job.gym_id, _open_job_rows(job.gym_id))
        except caps.CapReached as exc:
            return redirect(target, str(exc), "warning")
    job.is_open = not job.is_open
    await audit(db, owner, "job.open" if job.is_open else "job.close", "job_posting", job.id, gym_id=job.gym_id,
                request=request, detail={"title": job.title})
    await db.commit()
    await refresh_nav_state(db)
    if job.is_open:
        return redirect(target, f"“{job.title}” is open - “Enquire for jobs” now shows in the navigation.")
    return redirect(target, f"“{job.title}” is closed and hidden from the public Jobs page.", "info")


# ---------------------------------------------------------------------- enquiries
async def _owned_enquiry(db: DB, owner: User, enquiry_id: int) -> JobEnquiry:
    stmt = (select(JobEnquiry).join(JobPosting, JobPosting.id == JobEnquiry.job_id).join(Gym, Gym.id == JobPosting.gym_id)
            .options(contains_eager(JobEnquiry.job).contains_eager(JobPosting.gym))
            .where(JobEnquiry.id == enquiry_id, Gym.owner_id == owner.id))
    enq = (await db.execute(stmt)).unique().scalar_one_or_none()
    if enq is None:
        raise NotFound("Enquiry not found.")
    return enq


async def _render_enquiries(request: Request, db: DB, job: JobPosting, status: str, *, error: str | None = None,
                            status_code: int = 200) -> Any:
    status = status if status in S.ENQUIRY_STATUS_LABELS else ""
    counts = {s.value: n for s, n in (await db.execute(
        select(JobEnquiry.status, func.count(JobEnquiry.id)).where(JobEnquiry.job_id == job.id)
        .group_by(JobEnquiry.status))).all()}
    stmt = select(JobEnquiry).where(JobEnquiry.job_id == job.id)
    if status:
        stmt = stmt.where(JobEnquiry.status == EnquiryStatus(status))
    page = await paginate(db, stmt.order_by(JobEnquiry.created_at.desc(), JobEnquiry.id.desc()),
                          page=parse_page(request).page, per_page=ENQUIRIES_PER_PAGE)
    enquiries = list(page.items)
    hire_links = {
        e.id: f"/owner/gyms/{job.gym_id}/staff/new?" + urlencode({"name": e.name, "email": e.email, "role": job.role.value})
        for e in enquiries
    }
    return render(request, "owner/jobs/enquiries.html", {
        "S": S, "job": job, "enquiries": enquiries, "page": page, "counts": counts, "total": sum(counts.values()),
        "status": status, "error": error, "hire_links": hire_links,
        "page_path": f"/owner/jobs/{job.id}/enquiries", "page_params": {"status": status},
    }, status_code=status_code)


@router.get("/owner/jobs/{job_id}/enquiries")
async def job_enquiries(request: Request, job_id: IdParam, db: DB, owner: OwnerUser, status: str = "") -> Any:
    job = await _owned_job(db, owner, job_id, joinedload(JobPosting.branch))
    return await _render_enquiries(request, db, job, status)


@router.post("/owner/enquiries/{enquiry_id}/status")
async def enquiry_status(request: Request, enquiry_id: IdParam, db: DB, owner: OwnerUser) -> Any:
    enq = await _owned_enquiry(db, owner, enquiry_id)
    form = await request.form()
    try:
        new = EnquiryStatus(S.form_str(form, "status"))
    except ValueError:
        job = await _owned_job(db, owner, enq.job_id, joinedload(JobPosting.branch))
        return await _render_enquiries(request, db, job, "", error="Choose a valid status for the enquiry.",
                                       status_code=400)
    back = f"/owner/jobs/{enq.job_id}/enquiries"
    if new == enq.status:
        return redirect(back, "Status unchanged.", "info")
    old = enq.status
    enq.status = new
    mail = S.enquiry_status_email(new, job_title=enq.job.title, gym_name=enq.job.gym.name)
    if mail is not None:
        await email_address(db, enq.email, mail[0], mail[1], recipient=enq.name)
    await audit(db, owner, "job.enquiry_status", "job_enquiry", enq.id, gym_id=enq.job.gym_id, request=request,
                detail={"from": old.value, "to": new.value, "job_id": enq.job_id})
    await db.commit()
    note = f" {enq.name} was e-mailed." if mail else ""
    return redirect(back, f"Marked {S.ENQUIRY_STATUS_LABELS[new.value].lower()}.{note}")


@router.get("/owner/enquiries/{enquiry_id}/resume")
async def enquiry_resume(enquiry_id: IdParam, db: DB, owner: OwnerUser) -> Response:
    enq = await _owned_enquiry(db, owner, enquiry_id)
    if not enq.resume_key:
        raise NotFound("This applicant did not attach a résumé.")
    try:
        data = await storage.load_private(enq.resume_key)
    except (storage.UploadError, OSError):
        raise NotFound("This file is no longer available.") from None
    ctype = "application/pdf" if data.startswith(b"%PDF-") else "image/jpeg"
    return _private_response(data, ctype, f"resume-{enq.name}")
