"""``/superadmin/governance`` - the super-admin's governance actions with failsafes (app/services/governance.py).

* ``/superadmin/governance``: every gym (search, state filter) with its state - active, suspended, deletion scheduled,
  archived - and the recent governance actions. Suspended gyms are the open cases (nav badge).
* ``/superadmin/governance/gyms/{id}``: the gym's IMPACT PREVIEW (members, refunds due, staff, earned pay outstanding,
  payouts under way, open orders), the deletion steps (schedule -> cooling-off -> final) with their blockers, restore,
  and the SETTLEMENT DESK of a suspended gym: final settlements, payouts (Cashfree / TEST MODE) or payment proofs per
  statement, pay waivers with a note, pro-rata member refunds, manual (cash) refunds, open orders.
* ``/superadmin/governance/users/{id}``: ban an owner (their gyms suspended; members kept or refunded) or an employee
  (final settlements, employments ended, clients reassigned or told), or lift a ban.

Every action: preview -> typed confirmation (e-mail / gym name) + reason + note -> execute; idempotent, double-submit
protected (the forms' submit ids), rate-limited per admin, audited, notifying everyone affected. Pages run a constant
number of queries. CSP-safe: plain forms and links, no inline script or style.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, Path, Request
from fastapi.responses import Response
from sqlalchemy import Text, cast, func, or_, select

from app.config import get_settings
from app.core import caps
from app.core.deps import DB, NotFound, SuperAdminUser
from app.core.templating import redirect, render
from app.core.utils import utcnow
from app.models import AuditLog, Employee, Gym, Subscription, User
from app.models.enums import Role
from app.routers.superadmin import COUNT_CAP, SECTIONS, Pager, nav_counts
from app.services import governance as G
from app.services import payroll as pr
from app.services import storage
from app.services import trust_safety as TS

router = APIRouter()

MAX_ID = 2**63 - 1
IdParam = Annotated[int, Path(ge=1, le=MAX_ID)]
PER_PAGE = 25
STATES = (("all", "All gyms"), ("active", "Active"), ("suspended", "Suspended"),
          ("deletion_scheduled", "Deletion scheduled"), ("archived", "Deleted"))
RECENT_ACTIONS = 8


# ====================================================================== helpers
def _form_str(form: Any, key: str, cap: int = 2000) -> str:
    value = form.get(key)
    return value[:cap] if isinstance(value, str) else ""


def _note(form: Any) -> str:
    return G.clean_note(_form_str(form, "note", G.NOTE_MAX + 100))


def _page_no(raw: Any) -> int:
    text_ = str(raw or "1").strip()
    return max(1, min(100_000, int(text_))) if text_.isdigit() and len(text_) < 7 else 1


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def _render(request: Request, db: DB, template: str, ctx: dict[str, Any], *, status_code: int = 200) -> Response:
    return render(request, f"superadmin/{template}", {
        "sections": SECTIONS, "section": "governance", "counts": await nav_counts(db), "count_cap": COUNT_CAP, "G": G,
        "money_text": G.money_text, **ctx}, status_code=status_code)


def _gym_url(gym_id: int, anchor: str = "") -> str:
    return f"/superadmin/governance/gyms/{gym_id}" + (f"#{anchor}" if anchor else "")


def _user_url(user_id: int) -> str:
    return f"/superadmin/governance/users/{user_id}"


async def _gym_or_404(db: DB, gym_id: int) -> Gym:
    gym = await db.get(Gym, gym_id)
    if gym is None:
        raise NotFound("Gym not found.")
    return gym


async def _rate_limited(admin: User, back: str) -> Response | None:
    refusal = await G.rate_refusal(admin)
    return redirect(back, refusal, "error") if refusal else None


# ====================================================================== index
@router.get("/superadmin/governance")
async def governance_index(request: Request, db: DB, admin: SuperAdminUser) -> Response:
    qp = request.query_params
    q = " ".join(str(qp.get("q") or "").split())[:80]
    state = qp.get("state") if qp.get("state") in {s for s, _ in STATES} else "all"
    stmt = select(Gym, User.full_name, User.email).join(User, User.id == Gym.owner_id)
    if state == "active":
        stmt = stmt.where(Gym.is_active.is_(True))
    elif state == "suspended":
        stmt = stmt.where(Gym.is_active.is_(False), Gym.archived_at.is_(None), Gym.deletion_requested_at.is_(None))
    elif state == "deletion_scheduled":
        stmt = stmt.where(Gym.deletion_requested_at.is_not(None), Gym.archived_at.is_(None))
    elif state == "archived":
        stmt = stmt.where(Gym.archived_at.is_not(None))
    if q:
        digits = q.lstrip("#")
        conds = [Gym.name.ilike(_like(q), escape="\\"), User.full_name.ilike(_like(q), escape="\\"),
                 cast(User.email, Text).ilike(_like(q), escape="\\")]
        if digits.isdigit() and len(digits) < 19:
            conds.append(Gym.id == int(digits))
        stmt = stmt.where(or_(*conds))
    n = await caps.count_upto(db, stmt.with_only_columns(Gym.id), COUNT_CAP + 1)
    pager = Pager(_page_no(qp.get("page")), PER_PAGE, min(n, COUNT_CAP), "/superadmin/governance",
                  {"q": q, "state": state}, n > COUNT_CAP)
    rows = (await db.execute(stmt.order_by(Gym.is_active.asc(), Gym.updated_at.desc(), Gym.id.desc())
                             .limit(PER_PAGE).offset(pager.offset))).all()
    ids = [g.id for g, _, _ in rows]
    members: dict[int, int] = {}
    if ids:
        members = dict((await db.execute(
            select(Subscription.gym_id, func.count(func.distinct(Subscription.customer_id)))
            .where(Subscription.gym_id.in_(ids), G._active_membership(date.today()))
            .group_by(Subscription.gym_id))).all())
    recent = list((await db.execute(
        select(AuditLog, User.full_name).outerjoin(User, User.id == AuditLog.actor_id)
        .where(AuditLog.action.like("governance.%")).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(RECENT_ACTIONS))).all())
    return await _render(request, db, "governance.html", {
        "rows": rows, "pager": pager, "q": q, "state": state, "states": STATES, "members": members, "recent": recent,
        "now": utcnow()})


# ====================================================================== a gym
async def _gym_ctx(db: DB, gym: Gym, **extra: Any) -> dict[str, Any]:
    owner = await db.get(User, gym.owner_id)
    owner_ban = await TS.active_ban(db, gym.owner_id)
    impact = (await G.gyms_impact(db, [gym]))[gym.id]
    state = G.gym_state(gym)
    desk = await G.desk(db, gym) if state in ("suspended", "deletion_scheduled") else None
    blockers = await G.deletion_blockers(db, gym) if state == "deletion_scheduled" else None
    today = date.today()
    return {"gym": gym, "owner": owner, "owner_ban": owner_ban, "impact": impact, "state": state, "desk": desk,
            "blockers": blockers, "reasons": G.GYM_DELETE_REASONS, "errors": {}, "values": {},
            "cooling_off_days": get_settings().gym_delete_cooling_off_days, "today": today,
            "max_last_day": today + timedelta(days=pr.MAX_FUTURE_TERMINATION_DAYS),
            "statement_settled": G.statement_settled, "now": utcnow(), **extra}


@router.get("/superadmin/governance/gyms/{gym_id}")
async def governance_gym(gym_id: IdParam, request: Request, db: DB, admin: SuperAdminUser) -> Response:
    gym = await _gym_or_404(db, gym_id)
    return await _render(request, db, "governance_gym.html", await _gym_ctx(db, gym))


@router.post("/superadmin/governance/gyms/{gym_id}/delete")
async def governance_gym_schedule(gym_id: IdParam, request: Request, db: DB, admin: SuperAdminUser) -> Response:
    """Step 1: suspend at once + cooling-off (typed gym name, reason, note)."""
    form = await request.form()
    gym = await _gym_or_404(db, gym_id)
    note, reason_code, typed = _note(form), _form_str(form, "reason", 40), _form_str(form, "confirm", 200)
    reason, errors = G.validate(G.GYM_DELETE_REASONS, reason_code, note, typed, gym.name, what="the gym's name")
    if gym.archived_at is not None or gym.deletion_requested_at is not None:
        return redirect(_gym_url(gym.id), "This gym is already scheduled for deletion.", "info")
    if errors or reason is None:
        ctx = await _gym_ctx(db, gym, errors=errors, values={"reason": reason_code, "note": note, "confirm": typed},
                             open_form="delete")
        return await _render(request, db, "governance_gym.html", ctx, status_code=400)
    if (limited := await _rate_limited(admin, _gym_url(gym.id))) is not None:
        return limited
    gym = await G.lock_gym(db, gym.id)
    if gym is None:
        raise NotFound("Gym not found.")
    done = await G.schedule_gym_deletion(db, admin, gym, reason=reason, note=note, request=request)
    await db.commit()
    if not done:
        return redirect(_gym_url(gym.id), "This gym is already scheduled for deletion.", "info")
    return redirect(_gym_url(gym.id), f"{gym.name} is suspended and scheduled for deletion. It can be restored until "
                                      f"{gym.delete_after:%d %b %Y}; settle staff pay, members and orders below.",
                    "success")


@router.post("/superadmin/governance/gyms/{gym_id}/restore")
async def governance_gym_restore(gym_id: IdParam, request: Request, db: DB, admin: SuperAdminUser) -> Response:
    form = await request.form()
    gym = await _gym_or_404(db, gym_id)
    note = _note(form)
    if gym.archived_at is not None or gym.deletion_requested_at is None:
        return redirect(_gym_url(gym.id), "No deletion is scheduled for this gym.", "info")
    if not G.LIFT_NOTE_MIN <= len(note) <= G.NOTE_MAX:
        ctx = await _gym_ctx(db, gym, errors={"restore_note": f"Write why the deletion is cancelled ({G.LIFT_NOTE_MIN}-"
                                                              f"{G.NOTE_MAX} characters)."},
                             values={"restore_note": note}, open_form="restore")
        return await _render(request, db, "governance_gym.html", ctx, status_code=400)
    gym = await G.lock_gym(db, gym.id)
    if gym is None:
        raise NotFound("Gym not found.")
    state = await G.restore_gym(db, admin, gym, note=note, request=request)
    await db.commit()
    if state is None:
        return redirect(_gym_url(gym.id), "No deletion is scheduled for this gym.", "info")
    if state == "active":
        return redirect(_gym_url(gym.id), f"The deletion was cancelled - {gym.name} is open again.", "success")
    return redirect(_gym_url(gym.id), f"The deletion was cancelled. {gym.name} stays suspended while its owner is "
                                      "banned.", "success")


@router.post("/superadmin/governance/gyms/{gym_id}/finalize")
async def governance_gym_finalize(gym_id: IdParam, request: Request, db: DB, admin: SuperAdminUser) -> Response:
    """Step 2: the final deletion (archive) - typed gym name + note; blocked while anything is owed."""
    form = await request.form()
    gym = await _gym_or_404(db, gym_id)
    note, typed = _note(form), _form_str(form, "confirm", 200)
    if gym.archived_at is not None:
        return redirect(_gym_url(gym.id), "This gym was deleted already.", "info")
    errors: dict[str, str] = {}
    if not G.NOTE_MIN <= len(note) <= G.NOTE_MAX:
        errors["final_note"] = f"Write a closing note ({G.NOTE_MIN}-{G.NOTE_MAX} characters) - it is kept in the audit log."
    if not G.typed_matches(typed, gym.name):
        errors["final_confirm"] = "Type the gym's name exactly as shown to confirm."
    if errors:
        ctx = await _gym_ctx(db, gym, errors=errors, values={"final_note": note, "final_confirm": typed},
                             open_form="finalize")
        return await _render(request, db, "governance_gym.html", ctx, status_code=400)
    if (limited := await _rate_limited(admin, _gym_url(gym.id))) is not None:
        return limited
    gym = await G.lock_gym(db, gym.id)
    if gym is None:
        raise NotFound("Gym not found.")
    try:
        done = await G.finalize_gym_deletion(db, admin, gym, note=note, request=request)
    except G.GovernanceError as exc:
        # refused before anything was written: COMMIT only releases the gym's lock (a rollback would expire the
        # signed-in admin the page renders)
        await db.commit()
        ctx = await _gym_ctx(db, gym, errors={"final": str(exc)}, values={"final_note": note, "final_confirm": typed},
                             open_form="finalize")
        return await _render(request, db, "governance_gym.html", ctx, status_code=409)
    await db.commit()
    if not done:
        return redirect(_gym_url(gym.id), "This gym was deleted already.", "info")
    return redirect(_gym_url(gym.id), f"{gym.name} was deleted (archived). Its financial records are kept, read-only.",
                    "success")


# ---------------------------------------------------------------------- the settlement desk
def _confirmed(form: Any) -> bool:
    return _form_str(form, "confirm_money", 5) == "1"


@router.post("/superadmin/governance/gyms/{gym_id}/settlements")
async def governance_settlements(gym_id: IdParam, request: Request, db: DB, admin: SuperAdminUser) -> Response:
    form = await request.form()
    back = _gym_url(gym_id, "desk-staff")
    try:
        last_day = date.fromisoformat(_form_str(form, "last_day", 10))
    except ValueError:
        return redirect(back, "Choose the last working day.", "error")
    if not _confirmed(form):
        return redirect(back, "Tick the box to confirm the final settlements.", "error")
    if (limited := await _rate_limited(admin, back)) is not None:
        return limited
    gym = await G.lock_gym(db, gym_id)
    if gym is None:
        raise NotFound("Gym not found.")
    try:
        created, problems = await G.create_settlements(db, admin, gym, last_day=last_day, request=request)
    except G.GovernanceError as exc:
        await db.rollback()
        return redirect(back, str(exc), "error")
    await db.commit()
    if not created and not problems:
        return redirect(back, "Every employee still working there has a final settlement already.", "info")
    msg = f"{len(created)} final settlement{'s' if len(created) != 1 else ''} created - nothing is paid until you pay each one."
    if problems:
        msg += " Not created: " + "; ".join(problems[:3])
    return redirect(back, msg, "warning" if problems else "success")


@router.post("/superadmin/governance/statements/{statement_id}/payout")
async def governance_payout(statement_id: IdParam, request: Request, db: DB, admin: SuperAdminUser) -> Response:
    form = await request.form()
    found = await G.load_statement(db, statement_id)
    if found is None:
        raise NotFound("Salary statement not found.")
    st, emp, gym = found
    back = _gym_url(gym.id, f"emp-{emp.id}")
    if not _confirmed(form):
        return redirect(back, "Tick the box to confirm the payout - money moves at once.", "error")
    if (limited := await _rate_limited(admin, back)) is not None:
        return limited
    try:
        tx = await G.pay_statement(db, admin, st, emp, gym, request=request)
    except (pr.PayrollError, G.GovernanceError) as exc:
        await db.commit()  # a refused payout keeps its FAILED ledger row (the payroll path's record)
        return redirect(back, str(exc), "error")
    await db.commit()
    amount = G.money_text({tx.currency: tx.amount})
    if tx.provider == "test":
        return redirect(back, f"TEST MODE: payout of {amount} to {emp.full_name} simulated (reference "
                              f"{tx.provider_reference}) - no money moved. It counts as paid for the deletion check.",
                        "success")
    return redirect(back, f"Payout of {amount} to {emp.full_name} sent through Cashfree (reference "
                          f"{tx.provider_reference or '-'}). Record the bank's confirmation when it arrives.", "success")


@router.post("/superadmin/governance/statements/{statement_id}/proof")
async def governance_proof(statement_id: IdParam, request: Request, db: DB, admin: SuperAdminUser) -> Response:
    form = await request.form()
    found = await G.load_statement(db, statement_id)
    if found is None:
        raise NotFound("Salary statement not found.")
    st, emp, gym = found
    back = _gym_url(gym.id, f"emp-{emp.id}")
    reference = " ".join(_form_str(form, "reference", 80).split())
    if not pr.REFERENCE_RE.match(reference):
        return redirect(back, "Enter the payment's reference / UTR (6-40 letters and digits).", "error")
    try:
        amount = Decimal(_form_str(form, "amount", 20).replace(",", "").strip())
    except InvalidOperation:
        return redirect(back, "Enter the amount paid.", "error")
    if amount <= 0 or amount > st.net_pay * 2:
        return redirect(back, "Enter the amount that was paid.", "error")
    try:
        paid_on = date.fromisoformat(_form_str(form, "paid_on", 10)) if _form_str(form, "paid_on", 10) else None
    except ValueError:
        paid_on = None
    upload = form.get("proof")
    if upload is None or not getattr(upload, "filename", ""):
        return redirect(back, "Choose the payment proof (a screenshot or PDF of the transfer).", "error")
    try:
        await G.record_statement_proof(db, admin, st, emp, gym, upload=upload, reference=reference, amount_paid=amount,
                                       paid_on=paid_on, request=request)
    except (storage.UploadError, pr.PayrollError, G.GovernanceError) as exc:
        await db.rollback()
        return redirect(back, str(exc), "error")
    await db.commit()
    return redirect(back, f"Payment proof recorded for {emp.full_name} - it is checked automatically.", "success")


@router.post("/superadmin/governance/employees/{employee_id}/waive")
async def governance_waive(employee_id: IdParam, request: Request, db: DB, admin: SuperAdminUser) -> Response:
    form = await request.form()
    note = _note(form)
    emp = await db.scalar(select(Employee).where(Employee.id == employee_id).with_for_update())
    if emp is None:
        raise NotFound("Staff member not found.")
    gym = await _gym_or_404(db, emp.gym_id)
    back = _gym_url(gym.id, f"emp-{emp.id}")
    if (limited := await _rate_limited(admin, back)) is not None:
        return limited
    try:
        waiver = await G.waive_employee_pay(db, admin, emp, gym, note=note, request=request)
    except G.GovernanceError as exc:
        await db.rollback()
        return redirect(back, str(exc), "error")
    await db.commit()
    if waiver is None:
        return redirect(back, f"{emp.full_name}'s pay was waived already.", "info")
    return redirect(back, f"{emp.full_name}'s outstanding pay was recorded as waived ({waiver.currency} "
                          f"{waiver.amount}); their employment ended.", "success")


@router.post("/superadmin/governance/gyms/{gym_id}/refunds")
async def governance_refunds(gym_id: IdParam, request: Request, db: DB, admin: SuperAdminUser) -> Response:
    form = await request.form()
    back = _gym_url(gym_id, "desk-members")
    if not _confirmed(form):
        return redirect(back, "Tick the box to confirm the refunds - money moves at once.", "error")
    if (limited := await _rate_limited(admin, back)) is not None:
        return limited
    gym = await G.lock_gym(db, gym_id)
    if gym is None:
        raise NotFound("Gym not found.")
    try:
        await G.queue_member_refunds(db, admin, gym, request=request)
    except G.GovernanceError as exc:
        await db.rollback()
        return redirect(back, str(exc), "error")
    await db.commit()
    return redirect(back, "Pro-rata refunds are being issued in the background - members paid online are refunded to "
                          "their original payment method and told. Cash payments are listed for a manual refund.",
                    "success")


@router.post("/superadmin/governance/subscriptions/{subscription_id}/manual-refund")
async def governance_manual_refund(subscription_id: IdParam, request: Request, db: DB,
                                   admin: SuperAdminUser) -> Response:
    form = await request.form()
    gym_id = await db.scalar(select(Subscription.gym_id).where(Subscription.id == subscription_id))
    if gym_id is None:
        raise NotFound("Membership not found.")
    back = _gym_url(gym_id, "desk-members")
    if (limited := await _rate_limited(admin, back)) is not None:
        return limited
    try:
        result = await G.manual_membership_refund(db, admin, subscription_id, reference=_form_str(form, "reference", 80),
                                                  note=_note(form), request=request)
    except G.GovernanceError as exc:
        await db.rollback()
        return redirect(back, str(exc), "error")
    await db.commit()
    if result.state == "refunded":
        return redirect(back, f"Manual refund of {G.money_text({result.currency: result.amount})} recorded; the "
                              "membership ended.", "success")
    return redirect(back, result.message or "Nothing to refund for this membership.", "info")


@router.post("/superadmin/governance/gyms/{gym_id}/orders")
async def governance_orders(gym_id: IdParam, request: Request, db: DB, admin: SuperAdminUser) -> Response:
    form = await request.form()
    back = _gym_url(gym_id, "desk-orders")
    if not _confirmed(form):
        return redirect(back, "Tick the box to confirm - every open order is cancelled and refunded.", "error")
    if (limited := await _rate_limited(admin, back)) is not None:
        return limited
    gym = await G.lock_gym(db, gym_id)
    if gym is None:
        raise NotFound("Gym not found.")
    try:
        await G.queue_order_settlement(db, admin, gym, request=request)
    except G.GovernanceError as exc:
        await db.rollback()
        return redirect(back, str(exc), "error")
    await db.commit()
    return redirect(back, "Open orders are being cancelled and refunded in the background; buyers are told.", "success")


# ====================================================================== a person (ban / lift)
async def _user_ctx(db: DB, admin: User, target: User, **extra: Any) -> dict[str, Any]:
    ban = await TS.active_ban(db, target.id)
    refusal = await G.ban_refusal(db, admin, target)
    gyms: list[Gym] = []
    impacts: dict[int, G.GymImpact] = {}
    employments: list[G.EmploymentImpact] = []
    if target.role == Role.OWNER:
        gyms = await G.owner_gyms(db, target.id)
        impacts = await G.gyms_impact(db, gyms)
    elif target.role == Role.STAFF:
        employments = await G.employee_impact(db, target)
    total = G.total_impact(impacts.values())
    reasons = G.OWNER_BAN_REASONS if target.role == Role.OWNER else G.EMPLOYEE_BAN_REASONS
    return {"target": target, "ban": ban, "refusal": refusal, "gyms": gyms, "impacts": impacts, "total": total,
            "employments": employments, "reasons": reasons, "policies": G.MEMBER_POLICIES, "errors": {},
            "values": {"member_policy": "keep"}, "lift_min": G.LIFT_NOTE_MIN, "now": utcnow(), **extra}


async def _target_or_404(db: DB, user_id: int) -> User:
    target = await db.get(User, user_id)
    if target is None:
        raise NotFound("User not found.")
    return target


@router.get("/superadmin/governance/users/{user_id}")
async def governance_user(user_id: IdParam, request: Request, db: DB, admin: SuperAdminUser) -> Response:
    target = await _target_or_404(db, user_id)
    return await _render(request, db, "governance_user.html", await _user_ctx(db, admin, target))


@router.post("/superadmin/governance/users/{user_id}/ban")
async def governance_user_ban(user_id: IdParam, request: Request, db: DB, admin: SuperAdminUser) -> Response:
    form = await request.form()
    target = await _target_or_404(db, user_id)
    back = _user_url(target.id)
    refusal = await G.ban_refusal(db, admin, target)
    if refusal:
        ctx = await _user_ctx(db, admin, target, errors={"ban": refusal})
        return await _render(request, db, "governance_user.html", ctx, status_code=403)
    if await TS.active_ban(db, target.id) is not None:
        return redirect(back, f"{target.full_name}'s account is already banned.", "info")
    note, reason_code, typed = _note(form), _form_str(form, "reason", 40), _form_str(form, "confirm", 300)
    policy = _form_str(form, "member_policy", 10)
    reasons = G.OWNER_BAN_REASONS if target.role == Role.OWNER else G.EMPLOYEE_BAN_REASONS
    reason, errors = G.validate(reasons, reason_code, note, typed, target.email, what="the account's e-mail address")
    reassign: dict[int, int] = {}
    if target.role == Role.OWNER and policy not in G.MEMBER_POLICIES:
        errors["member_policy"] = "Choose what happens to the members' memberships."
    if target.role == Role.STAFF:
        for key in form:
            if key.startswith("reassign_") and key[9:].isdigit():
                value = _form_str(form, key, 20)
                if value.isdigit() and len(value) < 19:
                    reassign[int(key[9:])] = int(value)
    if errors or reason is None:
        values = {"reason": reason_code, "note": note, "confirm": typed, "member_policy": policy or "keep",
                  "reassign": reassign}
        ctx = await _user_ctx(db, admin, target, errors=errors, values=values)
        return await _render(request, db, "governance_user.html", ctx, status_code=400)
    if (limited := await _rate_limited(admin, back)) is not None:
        return limited
    try:
        if target.role == Role.OWNER:
            outcome = await G.ban_owner(db, admin, target, reason=reason, note=note, member_policy=policy,
                                        request=request)
            await db.commit()
            if not outcome.banned_now:
                return redirect(back, f"{target.full_name}'s account is already banned.", "info")
            msg = (f"{target.full_name} is banned and {outcome.gyms_suspended} gym"
                   f"{'s are' if outcome.gyms_suspended != 1 else ' is'} suspended.")
            if outcome.refunds_queued:
                msg += f" {outcome.refunds_queued} membership refund(s) are being issued."
            return redirect(back, msg + " Settle staff pay on each gym's governance page.", "success")
        result = await G.ban_employee(db, admin, target, reason=reason, note=note, reassign=reassign, request=request)
    except G.GovernanceError as exc:
        await db.commit()  # refused before anything was written: releases the locks (no rollback: see above)
        ctx = await _user_ctx(db, admin, target, errors={"ban": str(exc)})
        return await _render(request, db, "governance_user.html", ctx, status_code=409)
    await db.commit()
    if not result.banned_now:
        return redirect(back, f"{target.full_name}'s account is already banned.", "info")
    msg = (f"{target.full_name} is banned: {result.ended} employment{'s' if result.ended != 1 else ''} ended, "
           f"{len(result.settlements)} final settlement{'s' if len(result.settlements) != 1 else ''} created (pay them "
           "from Payroll or the gym's governance page).")
    if result.settlement_errors:
        msg += " Not created: " + "; ".join(result.settlement_errors[:2])
    return redirect(back, msg, "warning" if result.settlement_errors else "success")


@router.post("/superadmin/governance/users/{user_id}/lift")
async def governance_user_lift(user_id: IdParam, request: Request, db: DB, admin: SuperAdminUser) -> Response:
    form = await request.form()
    target = await _target_or_404(db, user_id)
    back = _user_url(target.id)
    note = _note(form)
    if not G.LIFT_NOTE_MIN <= len(note) <= G.NOTE_MAX:
        ctx = await _user_ctx(db, admin, target, errors={"lift_note": f"Write why the ban is lifted ({G.LIFT_NOTE_MIN}-"
                                                                      f"{G.NOTE_MAX} characters)."},
                              values={"lift_note": note})
        return await _render(request, db, "governance_user.html", ctx, status_code=400)
    lifted, reopened = await G.lift_ban(db, admin, target, note=note, request=request)
    await db.commit()
    if not lifted:
        return redirect(back, f"{target.full_name}'s account is not banned.", "info")
    msg = f"The ban on {target.full_name} was lifted - they can sign in again."
    if reopened:
        msg += f" {reopened} gym{'s' if reopened != 1 else ''} opened again."
    return redirect(back, msg, "success")
