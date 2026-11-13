"""``/superadmin/trust`` - Trust & safety review (super-admin only).

Four paged, filterable lists, newest first: platform account bans (lift one with a required note), staff conduct
violations (the immutable records gyms wrote when ending an employment), the network addresses a severe ban acted on
(banned through ``ip_bans``, or skipped and flagged: shared by many accounts, the acting admin's own, not public,
whitelisted) and the blocked-identity registry (keyed hashes only - look an e-mail or phone up by typing it: it is
hashed the same way and matched, never stored). Every list count stops at COUNT_CAP ("10,000+"); every change is
audited (app/services/trust_safety.py). CSP-safe: plain links and forms, no inline script or style.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Request
from sqlalchemy import false, or_, select

from app.core import caps
from app.core.deps import DB, NotFound, SuperAdminUser
from app.core.templating import redirect, render
from app.models import AccountBan, BlockedIdentity, IpBan, StaffViolation, TrustIpAction, User
from app.models.enums import Role
from app.models.trust import IDENTITY_KINDS, IDENTITY_REASONS, IP_ACTIONS
from app.routers.superadmin import COUNT_CAP, SECTIONS, Pager, nav_counts
from app.services import trust_safety as TS

router = APIRouter()

MAX_ID = 2**63 - 1
IdParam = Annotated[int, Path(ge=1, le=MAX_ID)]
PER_PAGE = 25
TABS = (("bans", "Account bans", "lock"), ("violations", "Violations", "flag"), ("ips", "Network addresses", "globe"),
        ("identities", "Blocked identities", "id-card"))
TAB_KEYS = tuple(t[0] for t in TABS)
SECTION = ("trust", "Trust & safety", "/superadmin/trust", "flag")
IP_ACTION_LABELS = {"banned": "Banned", "shared": "Skipped · shared address", "actor": "Skipped · acting admin's address",
                    "platform_admin": "Skipped · a platform admin's address",
                    "not_public": "Skipped · not a public address", "whitelisted": "Skipped · whitelisted"}
IDENTITY_KIND_LABELS = {"email": "E-mail", "phone": "Phone", "id_number": "ID number"}
IDENTITY_REASON_LABELS = {"self_deleted": "Deleted by the holder", "admin_deleted": "Deleted by an admin",
                          "banned": "Banned", "severe_violation": "Severe violation"}
SOURCE_LABELS = {"violations": "Repeated violations", "severe": "Severe violation", "platform": "Platform decision"}


def sections() -> tuple[tuple[str, str, str, str], ...]:
    return SECTIONS if any(s[0] == SECTION[0] for s in SECTIONS) else (*SECTIONS, SECTION)


def _page_no(raw: Any) -> int:
    text = str(raw or "1").strip()
    return max(1, min(100_000, int(text))) if text.isdigit() and len(text) < 7 else 1


def _clip(value: Any, n: int) -> str:
    return " ".join(str(value or "").split())[:n]


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def _capped_count(db: DB, stmt: Any) -> tuple[int, bool]:
    n = await caps.count_upto(db, stmt, COUNT_CAP + 1)
    return min(n, COUNT_CAP), n > COUNT_CAP


async def _people(db: DB, ids: set[int]) -> dict[int, User]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.id: u for u in (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all()}


async def _tab_counts(db: DB) -> dict[str, int]:
    return {
        "bans": await caps.count_upto(db, select(AccountBan.id).where(AccountBan.lifted_at.is_(None)), COUNT_CAP + 1),
        "violations": await caps.count_upto(db, select(StaffViolation.id), COUNT_CAP + 1),
        "ips": await caps.count_upto(db, select(TrustIpAction.id).where(TrustIpAction.action == "shared"), COUNT_CAP + 1),
        "identities": await caps.count_upto(db, select(BlockedIdentity.id), COUNT_CAP + 1),
    }


async def _bans(db: DB, qp: Any, page: int) -> dict[str, Any]:
    state = qp.get("state") if qp.get("state") in ("active", "lifted", "all") else "active"
    q = _clip(qp.get("q"), 80)
    stmt = select(AccountBan)
    if state == "active":
        stmt = stmt.where(AccountBan.lifted_at.is_(None))
    elif state == "lifted":
        stmt = stmt.where(AccountBan.lifted_at.is_not(None))
    if q:
        digits = q.lstrip("#")
        stmt = stmt.where(AccountBan.user_id == int(digits)) if digits.isdigit() and len(digits) < 19 else stmt.where(
            AccountBan.person_name.ilike(_like(q), escape="\\"))
    total, capped = await _capped_count(db, stmt.with_only_columns(AccountBan.id))
    pager = Pager(page, PER_PAGE, total, "/superadmin/trust", {"tab": "bans", "state": state, "q": q}, capped)
    rows = list((await db.execute(stmt.order_by(AccountBan.created_at.desc(), AccountBan.id.desc())
                                  .limit(PER_PAGE).offset(pager.offset))).scalars().all())
    ids = [b.id for b in rows]
    actions: dict[int, list[TrustIpAction]] = {i: [] for i in ids}
    if ids:
        for a in (await db.execute(select(TrustIpAction).where(TrustIpAction.account_ban_id.in_(ids))
                                   .order_by(TrustIpAction.id))).scalars().all():
            actions[a.account_ban_id].append(a)
    people = await _people(db, {b.user_id for b in rows} | {b.created_by_id or 0 for b in rows}
                           | {b.lifted_by_id or 0 for b in rows})
    return {"rows": rows, "pager": pager, "state": state, "q": q, "ip_actions": actions, "people": people}


async def _violations(db: DB, qp: Any, page: int) -> dict[str, Any]:
    category = qp.get("category") if qp.get("category") in (TS.VIOLATION, TS.SEVERE) else ""
    q = _clip(qp.get("q"), 80)
    stmt = select(StaffViolation)
    if category:
        stmt = stmt.where(StaffViolation.category == category)
    if q:
        digits = q.lstrip("#")
        if digits.isdigit() and len(digits) < 19:
            stmt = stmt.where(StaffViolation.subject_user_id == int(digits))
        else:
            like = _like(q)
            stmt = stmt.where(or_(StaffViolation.person_name.ilike(like, escape="\\"),
                                  StaffViolation.gym_name.ilike(like, escape="\\")))
    total, capped = await _capped_count(db, stmt.with_only_columns(StaffViolation.id))
    pager = Pager(page, PER_PAGE, total, "/superadmin/trust", {"tab": "violations", "category": category, "q": q}, capped)
    rows = list((await db.execute(stmt.order_by(StaffViolation.created_at.desc(), StaffViolation.id.desc())
                                  .limit(PER_PAGE).offset(pager.offset))).scalars().all())
    people = await _people(db, {v.actor_id or 0 for v in rows})
    return {"rows": rows, "pager": pager, "category": category, "q": q, "people": people}


async def _ips(db: DB, qp: Any, page: int) -> dict[str, Any]:
    action = qp.get("action") if qp.get("action") in IP_ACTIONS else ""
    q = _clip(qp.get("q"), 45)
    stmt = select(TrustIpAction, IpBan).outerjoin(IpBan, IpBan.id == TrustIpAction.ip_ban_id)
    if action:
        stmt = stmt.where(TrustIpAction.action == action)
    if q:
        stmt = stmt.where(TrustIpAction.ip.like(q.replace("%", "").replace("_", "") + "%"))
    total, capped = await _capped_count(db, stmt.with_only_columns(TrustIpAction.id))
    pager = Pager(page, PER_PAGE, total, "/superadmin/trust", {"tab": "ips", "action": action, "q": q}, capped)
    rows = list((await db.execute(stmt.order_by(TrustIpAction.created_at.desc(), TrustIpAction.id.desc())
                                  .limit(PER_PAGE).offset(pager.offset))).all())
    return {"rows": rows, "pager": pager, "action": action, "q": q}


def _lookup_hashes(q: str, country: str) -> list[tuple[str, str]]:
    """Registry keys a typed e-mail / phone / ID-number hash would have (the value itself is never stored)."""
    out: list[tuple[str, str]] = []
    if "@" in q:
        if (h := TS.email_hash(q)) is not None:
            out.append(("email", h))
    elif len(q) == 64 and all(c in "0123456789abcdef" for c in q.lower()):
        out.append(("id_number", q.lower()))
    elif any(c.isdigit() for c in q) and (h := TS.phone_hash(q, country or None)) is not None:
        out.append(("phone", h))
    return out


async def _identities(db: DB, qp: Any, page: int) -> dict[str, Any]:
    kind = qp.get("kind") if qp.get("kind") in IDENTITY_KINDS else ""
    reason = qp.get("reason") if qp.get("reason") in IDENTITY_REASONS else ""
    q = _clip(qp.get("q"), 254)
    country = _clip(qp.get("country"), 2).upper()
    stmt = select(BlockedIdentity)
    if kind:
        stmt = stmt.where(BlockedIdentity.kind == kind)
    if reason:
        stmt = stmt.where(BlockedIdentity.reason == reason)
    looked_up = None
    if q:
        keys = _lookup_hashes(q, country)
        looked_up = bool(keys)
        if keys:
            stmt = stmt.where(or_(*[(BlockedIdentity.kind == k) & (BlockedIdentity.value_hash == h) for k, h in keys]))
        else:
            digits = q.lstrip("#")
            stmt = stmt.where(BlockedIdentity.user_id == int(digits)) if digits.isdigit() and len(digits) < 19 \
                else stmt.where(false())  # nothing typed that can be looked up
    total, capped = await _capped_count(db, stmt.with_only_columns(BlockedIdentity.id))
    pager = Pager(page, PER_PAGE, total, "/superadmin/trust",
                  {"tab": "identities", "kind": kind, "reason": reason, "q": q, "country": country}, capped)
    rows = list((await db.execute(stmt.order_by(BlockedIdentity.created_at.desc(), BlockedIdentity.id.desc())
                                  .limit(PER_PAGE).offset(pager.offset))).scalars().all())
    people = await _people(db, {r.actor_id or 0 for r in rows})
    return {"rows": rows, "pager": pager, "kind": kind, "reason": reason, "q": q, "country": country,
            "looked_up": looked_up, "people": people}


LOADERS = {"bans": _bans, "violations": _violations, "ips": _ips, "identities": _identities}


async def _render(request: Request, db: DB, *, status_code: int = 200, **extra: Any) -> Any:
    qp = request.query_params
    tab = qp.get("tab") if qp.get("tab") in TAB_KEYS else "bans"
    data = await LOADERS[tab](db, qp, _page_no(qp.get("page")))
    return render(request, "superadmin/trust.html", {
        "sections": sections(), "section": "trust", "counts": await nav_counts(db), "count_cap": COUNT_CAP,
        "tab": tab, "tabs": TABS, "tab_counts": await _tab_counts(db), "data": data, "TS": TS,
        "ip_action_labels": IP_ACTION_LABELS, "kind_labels": IDENTITY_KIND_LABELS,
        "reason_labels": IDENTITY_REASON_LABELS, "source_labels": SOURCE_LABELS,
        "lift_min": TS.LIFT_NOTE_MIN, "lift_max": TS.LIFT_NOTE_MAX, "lift_errors": {}, "lift_values": {}, **extra,
    }, status_code=status_code)


@router.get("/superadmin/trust")
async def trust_page(request: Request, db: DB, admin: SuperAdminUser) -> Any:
    return await _render(request, db)


@router.post("/superadmin/trust/bans/{ban_id}/lift")
async def trust_ban_lift(ban_id: IdParam, request: Request, db: DB, admin: SuperAdminUser) -> Any:
    """Lift an account ban with a note: the account may sign in again and the IP bans this ban placed are lifted."""
    form = await request.form()
    note = TS.clean_note(form.get("note") if isinstance(form.get("note"), str) else "")
    ban = await db.scalar(select(AccountBan).where(AccountBan.id == ban_id).with_for_update())
    if ban is None:
        raise NotFound("Ban not found.")
    back = f"/superadmin/trust?tab=bans&state=all#ban-{ban.id}"
    if ban.lifted_at is not None:  # a double submit, or another admin lifted it first
        return redirect(back, "This ban was already lifted.", "info")
    if not TS.LIFT_NOTE_MIN <= len(note) <= TS.LIFT_NOTE_MAX:
        error = (f"Write why the ban is lifted ({TS.LIFT_NOTE_MIN}-{TS.LIFT_NOTE_MAX} characters) - it is kept in the "
                 "audit log.")
        return await _render(request, db, status_code=400, lift_errors={ban.id: error}, lift_values={ban.id: note})
    lifted_ips = await TS.lift_account_ban(db, ban, actor=admin, note=note, request=request)
    owner = await db.get(User, ban.user_id)
    if owner is not None and owner.role == Role.OWNER:  # the gyms an owner ban suspended open again (governance)
        from app.services.governance import restore_owner_gyms

        await restore_owner_gyms(db, admin, owner.id, request=request)
    await db.commit()
    extra = f" and {lifted_ips} network address ban{'s' if lifted_ips != 1 else ''}" if lifted_ips else ""
    return redirect(back, f"The ban on account #{ban.user_id}{extra} was lifted. The person was told they can sign in "
                          "again.", "success")
