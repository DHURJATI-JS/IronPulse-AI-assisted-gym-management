"""WP-C1 staff portal: dashboard, schedule, clients.

* ``/staff``            employments (gym, role, status, outstanding activation requirements with links), today's
                        shifts in each branch's time zone, this week, assigned clients, certification upload.
* ``/staff/schedule``   weekly Mon-Sun grid across every employment.
* ``/staff/clients``    clients of ACTIVE trainer employments (active assignment + live subscription only).
* ``/staff/clients/{id}`` goal, level, health conditions / allergies / injuries, latest metrics + BMI, active
                        workout plan, suspension status - only while an active assignment links the trainer. The
                        trainer-safety panel (what to avoid, contraindicated exercises, allergies, medication, last
                        update) sits on top; when the member keeps health details private (account settings) no health
                        data is read or shown: "The member chose not to share health details - ask before training".
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Path, Request
from sqlalchemy import and_, case, distinct, false, func, or_, select
from sqlalchemy.orm import contains_eager, joinedload, selectinload

from app.core import caps
from app.core.deps import DB, NotFound, StaffUser
from app.core.pagination import paginate, parse_page
from app.core.templating import redirect, render
from app.core.utils import bmi as calc_bmi
from app.core.utils import local_now
from app.models import (
    Employee,
    EmployeeDocument,
    Gym,
    HealthCondition,
    MetricEntry,
    Plan,
    Subscription,
    TrainerClient,
    TrainerShift,
    User,
    WorkoutPlan,
)
from app.models.enums import (
    ConditionKind,
    EmploymentStatus,
    NotificationCategory,
    Severity,
    SubscriptionStatus,
    VerificationStatus,
)
from app.services import quotas
from app.services import staffing as S
from app.services import storage
from app.services import trainer_safety
from app.services.audit import audit
from app.services.jobs import enqueue
from app.services.notify import notify

log = logging.getLogger("ironpulse.staff_portal")
router = APIRouter()
MAX_ID = 2**63 - 1  # BIGINT ids: a larger path id is a 422 page, never a driver error (a 500)
IdParam = Annotated[int, Path(ge=1, le=MAX_ID)]

CURRENT = (EmploymentStatus.INVITED, EmploymentStatus.PENDING_VERIFICATION, EmploymentStatus.ACTIVE,
           EmploymentStatus.SUSPENDED)
CLIENTS_PER_PAGE = 30
CONDITIONS_PER_KIND = 25  # safety notes shown per kind on a client page ("+N more" beyond; members add their own)
CONDITIONS_SHOWN = 100  # ... and at most this many in all, read safety-relevant first (LIMIT in SQL)
# "Safety-relevant first" - the order of app/services/health.safety_first(), spelled out here from plain columns so it
# can also rank inside a window: the oldest entry of every distinct tag (the tags are what the exercise guardrails
# read), then the other tagged entries, then injuries, conditions, allergies and medications - oldest first.
SAFETY_KINDS = (ConditionKind.INJURY, ConditionKind.CONDITION, ConditionKind.ALLERGY, ConditionKind.MEDICATION)
CONDITION_KIND_LABELS = {
    ConditionKind.CONDITION.value: "Conditions", ConditionKind.ALLERGY.value: "Allergies",
    ConditionKind.MEDICATION.value: "Medication", ConditionKind.INJURY.value: "Injuries",
}
CONDITION_KIND_ICONS = {
    ConditionKind.CONDITION.value: "heart", ConditionKind.ALLERGY.value: "alert",
    ConditionKind.MEDICATION.value: "check-badge", ConditionKind.INJURY.value: "activity",
}


def _safety_keys() -> tuple[Any, ...]:
    """(untagged, repeated tag, kind rank, id) of a client's health entry - sort ascending. The second key is a window
    over the client's entries: a statement's ORDER BY or select list only, never inside another window."""
    repeat = case((HealthCondition.tag.is_(None), false()),  # an untagged entry never repeats a tag
                  else_=func.row_number().over(partition_by=HealthCondition.tag, order_by=HealthCondition.id) > 1)
    kind = case(*((HealthCondition.kind == k, i) for i, k in enumerate(SAFETY_KINDS)), else_=len(SAFETY_KINDS))
    return HealthCondition.tag.is_(None), repeat, kind, HealthCondition.id


def _greeting(hour: int) -> str:
    if hour < 5:
        return "Good evening"
    if hour < 12:
        return "Good morning"
    if hour < 17:
        return "Good afternoon"
    return "Good evening"


def bmi_category(value: float | None) -> tuple[str, str]:
    if not value:
        return "Unknown", "zinc"
    if value < 18.5:
        return "Underweight", "amber"
    if value < 25:
        return "Healthy range", "green"
    if value < 30:
        return "Overweight", "amber"
    return "Obese range", "red"


async def _employments(db: DB, user: User) -> list[Employee]:
    order = case((Employee.status == EmploymentStatus.ACTIVE, 0), (Employee.status == EmploymentStatus.PENDING_VERIFICATION, 1),
                 (Employee.status == EmploymentStatus.INVITED, 2), (Employee.status == EmploymentStatus.SUSPENDED, 3),
                 else_=4)
    stmt = (
        select(Employee)
        .options(joinedload(Employee.gym), selectinload(Employee.documents),
                 selectinload(Employee.shifts).joinedload(TrainerShift.branch))
        .where(Employee.user_id == user.id, Employee.deleted_at.is_(None))
        .order_by(order, Employee.created_at)
    )
    return list((await db.execute(stmt)).unique().scalars().all())


def _live_link_clauses(user: User, today: date) -> list[Any]:
    """An assignment the trainer may act on: active link, ACTIVE employment of this user, live subscription."""
    return [
        TrainerClient.active.is_(True),
        Employee.user_id == user.id,
        Employee.status == EmploymentStatus.ACTIVE,
        Employee.deleted_at.is_(None),
        or_(TrainerClient.subscription_id.is_(None),
            and_(Subscription.status == SubscriptionStatus.ACTIVE,
                 or_(Subscription.ends_on.is_(None), Subscription.ends_on >= today))),
    ]


def _shift_where(shift: TrainerShift, emp: Employee) -> str:
    return shift.branch.name if shift.branch is not None else "Online"


def _shift_tz(shift: TrainerShift, user: User) -> str:
    return shift.branch.timezone if shift.branch is not None else (user.timezone or "UTC")


# ====================================================================== dashboard
@router.get("/staff")
async def portal(request: Request, db: DB, user: StaffUser) -> Any:
    employments = await _employments(db, user)
    current = [e for e in employments if e.status in CURRENT]
    former = [e for e in employments if e.status == EmploymentStatus.TERMINATED]
    working = [e for e in current if e.status != EmploymentStatus.SUSPENDED]

    client_counts: dict[int, int] = {}
    if working:
        today = local_now(user.timezone).date()
        rows = (await db.execute(
            select(TrainerClient.employee_id, func.count(TrainerClient.id))
            .join(Employee, Employee.id == TrainerClient.employee_id)
            .join(User, User.id == TrainerClient.customer_id)
            .outerjoin(Subscription, Subscription.id == TrainerClient.subscription_id)
            .where(*_live_link_clauses(user, today), User.deleted_at.is_(None))
            .group_by(TrainerClient.employee_id)
        )).all()
        client_counts = {emp_id: int(n) for emp_id, n in rows}

    today_items: list[dict[str, Any]] = []
    for emp in working:
        for s in emp.shifts:
            now = local_now(_shift_tz(s, user))
            if s.weekday != now.weekday():
                continue
            now_min = now.hour * 60 + now.minute
            start, end = S.tmin(s.start_time), S.tmin(s.end_time)
            state = "now" if start <= now_min < end else ("next" if now_min < start else "done")
            today_items.append({"shift": s, "emp": emp, "where": _shift_where(s, emp), "state": state,
                                "starts_in": start - now_min, "tz": _shift_tz(s, user)})
    today_items.sort(key=lambda x: (x["shift"].start_time, x["emp"].id))

    week_shifts = [s for e in working for s in e.shifts]
    local = local_now(user.timezone)
    pending = [e for e in current if e.status in (EmploymentStatus.INVITED, EmploymentStatus.PENDING_VERIFICATION)]
    requirements = {e.id: S.activation_requirements(e, user, e.documents) for e in current}
    return render(request, "staff/portal.html", {
        "S": S, "employments": current, "former": former, "pending": pending, "requirements": requirements,
        "client_counts": client_counts, "today_items": today_items, "week_days": S.week_segments(week_shifts),
        "week_minutes": S.weekly_minutes(week_shifts), "week_count": len(week_shifts),
        "total_clients": sum(client_counts.values()), "today_wd": local.weekday(),
        "greeting": _greeting(local.hour), "local_now": local,
    })


@router.post("/staff/employments/{employee_id}/documents")
async def upload_certification(request: Request, employee_id: IdParam, db: DB, user: StaffUser) -> Any:
    """A staff member uploads their own certification (or résumé) for an employment that is not terminated."""
    emp = (await db.execute(
        select(Employee).options(joinedload(Employee.gym), selectinload(Employee.documents))
        .where(Employee.id == employee_id, Employee.user_id == user.id, Employee.deleted_at.is_(None))
    )).unique().scalar_one_or_none()
    if emp is None:
        raise NotFound("Employment not found.")
    if emp.status == EmploymentStatus.TERMINATED:
        return redirect("/staff", "This employment has ended.", "warning")
    form = await request.form()
    doc_type = S.form_str(form, "doc_type") or "certification"
    upload = form.get("document")
    if doc_type not in ("certification", "resume"):
        return redirect("/staff", "Choose a certification or résumé.", "error")
    if not S.has_file(upload):
        return redirect(f"/staff#cert-{emp.id}", "Choose a file to upload.", "error")
    if len(emp.documents) >= caps.limit("employee_documents"):  # quick answer; the race-safe check is below
        return redirect("/staff", str(caps.CapReached("employee_documents")), "error")
    try:
        stored, _plain = await storage.save_private_file(upload, kind="staff_docs")  # type: ignore[arg-type]
    except storage.UploadError as exc:
        return redirect(f"/staff#cert-{emp.id}", str(exc), "error")
    try:  # race-safe (cap locks held until COMMIT): the record's document cap, then the gym's storage quota
        await caps.enforce(db, "employee_documents", emp.id,
                           select(EmployeeDocument.id).where(EmployeeDocument.employee_id == emp.id))
        await quotas.reserve_gym(db, emp.gym_id, stored.size)
    except caps.CapReached as exc:
        await storage.delete_key(stored.key)
        return redirect(f"/staff#cert-{emp.id}", str(exc), "error")
    db.add(EmployeeDocument(employee_id=emp.id, doc_type=doc_type, file_key=stored.key,
                            original_filename=stored.original_filename, content_type=stored.content_type,
                            sha256=stored.sha256, size_bytes=stored.size))
    label = S.DOC_TYPE_LABELS[doc_type]
    if doc_type == "certification" and emp.is_trainer and emp.verification_status != VerificationStatus.VERIFIED:
        emp.verification_status = VerificationStatus.QUEUED
    if emp.status in (EmploymentStatus.INVITED, EmploymentStatus.PENDING_VERIFICATION, EmploymentStatus.ACTIVE):
        await enqueue(db, "staff_activation_check", {"employee_id": emp.id}, dedupe_key=f"staff_activation_check:{emp.id}",
                      user_id=user.id)
    await notify(db, emp.gym.owner_id, f"{emp.full_name} uploaded a {label.lower()}",
                 f"{emp.full_name} added “{stored.original_filename}” to their staff record."
                 + (" It is being checked automatically." if doc_type == "certification" and emp.is_trainer else ""),
                 category=NotificationCategory.STAFF, severity=Severity.INFO, link=f"/owner/staff/{emp.id}?tab=documents")
    await audit(db, user, "staff.document_add", "employee", emp.id, gym_id=emp.gym_id, request=request,
                detail={"doc_type": doc_type, "summary": f"{label} uploaded by the staff member: {stored.original_filename}"})
    await db.commit()
    return redirect("/staff", f"{label} uploaded - thanks! "
                    + ("We'll check it automatically and let you know." if doc_type == "certification" else ""))


# ====================================================================== schedule
@router.get("/staff/schedule")
async def schedule(request: Request, db: DB, user: StaffUser) -> Any:
    employments = [e for e in await _employments(db, user) if e.status in CURRENT]
    shifts = [s for e in employments for s in e.shifts]
    by_emp = {e.id: e for e in employments}
    days: list[list[dict[str, Any]]] = [[] for _ in range(7)]
    for s in sorted(shifts, key=lambda x: (x.weekday, x.start_time)):
        emp = by_emp[s.employee_id]
        days[s.weekday].append({"shift": s, "emp": emp, "where": _shift_where(s, emp),
                                "minutes": S.tmin(s.end_time) - S.tmin(s.start_time)})
    local = local_now(user.timezone)
    day_minutes = [sum(x["minutes"] for x in d) for d in days]
    return render(request, "staff/schedule.html", {
        "S": S, "employments": employments, "days": days, "day_minutes": day_minutes,
        "week_days": S.week_segments([s for s in shifts if by_emp[s.employee_id].status != EmploymentStatus.SUSPENDED]),
        "week_minutes": S.weekly_minutes(shifts), "today_wd": local.weekday(), "local_now": local,
        "multi_gym": len({e.gym_id for e in employments}) > 1,
    })


# ====================================================================== clients
@router.get("/staff/clients")
async def clients(request: Request, db: DB, user: StaffUser) -> Any:
    """One page of the trainer's live clients (COUNT + one LIMIT/OFFSET query + health-flag counts of that page)."""
    today = local_now(user.timezone).date()
    live = (*_live_link_clauses(user, today), User.deleted_at.is_(None))
    stmt = (
        select(TrainerClient, Plan.name, Subscription.ends_on)
        .join(Employee, Employee.id == TrainerClient.employee_id)
        .join(Gym, Gym.id == Employee.gym_id)
        .join(User, User.id == TrainerClient.customer_id)
        .outerjoin(Subscription, Subscription.id == TrainerClient.subscription_id)
        .outerjoin(Plan, Plan.id == Subscription.plan_id)
        .options(contains_eager(TrainerClient.employee).contains_eager(Employee.gym),
                 contains_eager(TrainerClient.customer).joinedload(User.customer_profile))
        .where(*live)
        .order_by(User.full_name, TrainerClient.id)
    )
    count = (select(func.count(TrainerClient.id)).join(Employee, Employee.id == TrainerClient.employee_id)
             .join(User, User.id == TrainerClient.customer_id)
             .outerjoin(Subscription, Subscription.id == TrainerClient.subscription_id).where(*live))
    page = await paginate(db, stmt, page=parse_page(request).page, per_page=CLIENTS_PER_PAGE, count_stmt=count,
                          unique=True)
    items = [{"link": link, "customer": link.customer, "profile": link.customer.customer_profile, "emp": link.employee,
              "plan_name": plan_name, "ends_on": ends_on} for link, plan_name, ends_on in page.items]
    flags: dict[int, int] = {}
    private: set[int] = set()
    if items:
        ids = list({i["customer"].id for i in items})  # one page: at most CLIENTS_PER_PAGE ids
        # members who keep health details private: no count, not even a read of their entries
        private = {i["customer"].id for i in items if i["profile"] is not None
                   and i["profile"].share_health_with_trainers is False}
        shared = [cid for cid in ids if cid not in private]
        if shared:
            flags = {cid: int(n) for cid, n in (await db.execute(
                select(HealthCondition.customer_id, func.count(HealthCondition.id))
                .where(HealthCondition.customer_id.in_(shared)).group_by(HealthCondition.customer_id)
            )).all()}
    trainer_employments, trainer_gyms = (await db.execute(
        select(func.count(Employee.id), func.count(distinct(Employee.gym_id))).where(
            Employee.user_id == user.id, Employee.is_trainer.is_(True), Employee.deleted_at.is_(None),
            Employee.status != EmploymentStatus.TERMINATED)
    )).one()
    return render(request, "staff/clients.html", {
        "S": S, "items": items, "page": page, "flags": flags, "private": private, "is_trainer": bool(trainer_employments),
        "multi_gym": int(trainer_gyms or 0) > 1 or len({i["emp"].gym_id for i in items}) > 1,
    })


@router.get("/staff/clients/{customer_id}")
async def client_detail(request: Request, customer_id: IdParam, db: DB, user: StaffUser) -> Any:
    today = local_now(user.timezone).date()
    link = (await db.execute(
        select(TrainerClient)
        .join(Employee, Employee.id == TrainerClient.employee_id)
        .outerjoin(Subscription, Subscription.id == TrainerClient.subscription_id)
        .options(contains_eager(TrainerClient.employee).joinedload(Employee.gym))
        .where(TrainerClient.customer_id == customer_id, *_live_link_clauses(user, today))
        .order_by(TrainerClient.created_at.desc()).limit(1)
    )).unique().scalar_one_or_none()
    if link is None:
        raise NotFound("Client not found.")
    customer = (await db.execute(
        select(User).options(joinedload(User.customer_profile))
        .where(User.id == customer_id, User.deleted_at.is_(None))
    )).unique().scalar_one_or_none()
    if customer is None:
        raise NotFound("Client not found.")
    subscription = None
    if link.subscription_id:
        subscription = (await db.execute(select(Subscription).options(joinedload(Subscription.plan))
                                         .where(Subscription.id == link.subscription_id))).unique().scalar_one_or_none()
    # trainer safety: the assigned trainer sees the panel - or, when the member keeps health details private, only
    # "chose not to share - ask before training" and no health data at all (nothing below reads it)
    profile_row = customer.customer_profile
    if profile_row is not None and profile_row.share_health_with_trainers is False:
        withheld = await trainer_safety.load_brief(db, customer, trainer_safety.Access(trainer_safety.WITHHELD, False))
        return await _client_page(request, db, user, customer, link, subscription, withheld, today, rows=[])
    # at most CONDITIONS_PER_KIND per kind and CONDITIONS_SHOWN in all (LIMIT; members could add their own without
    # limit before the cap), safety-relevant first - each kind's first entry before any second one, so every kind the
    # client has shows with its total ("+N more")
    keys = (select(HealthCondition.id.label("cid"), HealthCondition.kind.label("kind"),
                   *(k.label(f"k{i}") for i, k in enumerate(_safety_keys())))
            .where(HealthCondition.customer_id == customer.id).subquery("keys"))
    order = (keys.c.k0, keys.c.k1, keys.c.k2, keys.c.k3)
    ranked = select(keys.c.cid, *order, func.row_number().over(partition_by=keys.c.kind, order_by=order).label("rn"),
                    func.count().over(partition_by=keys.c.kind).label("n")).subquery("ranked")
    rows = sorted((await db.execute(
        select(HealthCondition, ranked.c.n).join(ranked, ranked.c.cid == HealthCondition.id)
        .where(ranked.c.rn <= CONDITIONS_PER_KIND)
        .order_by(ranked.c.rn > 1, ranked.c.k0, ranked.c.k1, ranked.c.k2, ranked.c.k3)
        .limit(CONDITIONS_SHOWN)
    )).all(), key=lambda row: (str(row[0].kind.value), row[0].name.casefold(), row[0].id))  # shown by kind, then name
    safety = trainer_safety.build_brief(customer, [c for c, _n in rows], profile_row)  # the rows above: no second read
    return await _client_page(request, db, user, customer, link, subscription, safety, today, rows=rows)


async def _client_page(request: Request, db: DB, user: User, customer: User, link: TrainerClient,
                       subscription: Subscription | None, safety: trainer_safety.SafetyBrief, today: date, *,
                       rows: list[Any]) -> Any:
    shared = safety.level == trainer_safety.FULL
    conditions = [c for c, _n in rows]
    grouped: dict[str, list[HealthCondition]] = {k: [] for k in CONDITION_KIND_LABELS}
    kind_totals: dict[str, int] = {}
    for c, n in rows:
        grouped.setdefault(c.kind.value, []).append(c)
        kind_totals[c.kind.value] = int(n)
    more = {k: kind_totals[k] - len(grouped[k]) for k in kind_totals if kind_totals[k] > len(grouped[k])}
    metrics = list((await db.execute(
        select(MetricEntry).where(MetricEntry.customer_id == customer.id)
        .order_by(MetricEntry.recorded_on.desc()).limit(6)
    )).scalars().all())
    plan = (await db.execute(
        select(WorkoutPlan).where(WorkoutPlan.customer_id == customer.id, WorkoutPlan.is_active.is_(True))
        .order_by(WorkoutPlan.updated_at.desc()).limit(1)
    )).scalars().first()
    profile = customer.customer_profile
    latest = metrics[0] if metrics else None
    if latest is not None:
        height, weight, bmi_value = latest.height_cm, latest.weight_kg, latest.bmi
    elif profile is not None and profile.height_cm and profile.weight_kg:
        height, weight = profile.height_cm, profile.weight_kg
        bmi_value = calc_bmi(height, weight)
    else:
        height = weight = bmi_value = None
    bmi_label, bmi_tone = bmi_category(bmi_value)
    plan_days: list[dict[str, Any]] = []
    if plan is not None and isinstance(plan.plan, dict):
        for day in (plan.plan.get("days") or [])[:7]:
            if not isinstance(day, dict):
                continue
            items = [i for i in (day.get("items") or []) if isinstance(i, dict)]
            plan_days.append({"name": str(day.get("name") or "Session")[:60], "items": items[:6], "more": max(0, len(items) - 6)})
    weight_change = None
    if len(metrics) >= 2:
        weight_change = round(metrics[0].weight_kg - metrics[-1].weight_kg, 1)
    return render(request, "staff/client.html", {
        "S": S, "customer": customer, "profile": profile, "link": link, "emp": link.employee,
        "subscription": subscription, "grouped": grouped, "more_conditions": more, "has_conditions": bool(conditions),
        "kind_labels": CONDITION_KIND_LABELS, "kind_icons": CONDITION_KIND_ICONS, "metrics": metrics,
        "height": height, "weight": weight, "bmi_value": bmi_value, "bmi_label": bmi_label, "bmi_tone": bmi_tone,
        "weight_change": weight_change, "plan": plan, "plan_days": plan_days, "age": customer.age(today),
        "gear": _gear_for([c.name for c in conditions], customer.age(today)) if shared else None,
        "safety": safety, "health_shared": shared,
    })


def _gear_for(names: list[str], age: int | None) -> Any:
    from app.services.gear import recommend_gear

    return recommend_gear(names, age=age)
