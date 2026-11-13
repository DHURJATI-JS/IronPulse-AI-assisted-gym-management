"""WP-F member management for gym owners.

GET  /owner/customers                    members related to the owner's gyms (filters, search, pagination)
GET  /owner/customers/new                registration form: manual, or pre-filled by the AI intake
POST /owner/customers/new                (multipart) create the member (ID card mandatory) + invite e-mail
POST /owner/customers/intake             JSON {"text"} -> AI / deterministic prefill ('ai' rate-limit bucket)
GET  /owner/customers/{id}               member profile (related members only, else 404)
POST /owner/customers/{id}/health        edit conditions / allergies / medications / injuries / guardian
POST /owner/customers/{id}/reactivate    lift a safety pause (note >= 10 chars) via WP-E1's health service
POST /owner/customers/{id}/invite        re-send the invite e-mail (member never set a password)
GET  /owner/audit                        audit log of the owner's gyms + the owner's own actions
"""

from __future__ import annotations

import asyncio
import io
import re
from datetime import date, timedelta
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse
from sqlalchemy import and_, case, distinct, false, func, or_, select, true, union
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.core import caps
from app.core.deps import DB, NotFound, OwnerUser
from app.core.pagination import paginate, parse_page
from app.core.templating import redirect, render
from app.core.utils import clamp, local_today, utcnow
from app.models import (
    AuditLog,
    AuthToken,
    ChatThread,
    CustomerProfile,
    Gym,
    HealthAlert,
    HealthCondition,
    IdentityDocument,
    MetricEntry,
    MisconductReport,
    Subscription,
    SuspensionEvent,
    User,
)
from app.models.enums import ConditionKind, Goal, IdStatus, ReportStatus, Role, SubscriptionStatus, TokenPurpose
from app.services import intake as svc
from app.services import storage
from app.services import trainer_safety
from app.services import trust_safety as TS
from app.services.audit import audit
from app.services.conditions import TAG_LABELS
from app.services.identity_lookup import member_id, parse_member_id

router = APIRouter()
MAX_ID = 2**63 - 1  # BIGINT ids: a larger path id is a 422 page, never a driver error (a 500)
IdParam = Annotated[int, Path(ge=1, le=MAX_ID)]

PAGE_SIZE = 24
AUDIT_PAGE_SIZE = 40
AUDIT_COUNT_CAP = 10_000  # the audit log counts at most this many rows ("10,000+"); filters narrow it down
SUBS_PAGE_SIZE = 12  # memberships listed per page on a member's profile
HEALTH_LABELS_SHOWN = 3  # health chips per member on the list (+N)
CONDITIONS_PER_KIND = 50  # per kind on a member's profile; members add their own without limit (owner lists hold <= 20)
# Health entries read per member in SQL, safety-relevant first: a member's profile shows at most this many (each kind
# still says how many it holds) and the list's chips come from each member's first this-many. Self-added entries are
# capped at 50 and owner intake at 4 x 20, so only entries from before those caps can ever be cut.
CONDITIONS_SHOWN = 100
THREADS_SHOWN = 20  # a member's conversations with the owner's gyms listed on their profile (newest activity first)
NOTE_MIN, NOTE_MAX = 10, 1000
# Invite e-mails to one member (they go to an outside address): the owner's "Re-send invite" at most once per
# INVITE_RESEND_INTERVAL and at most INVITES_PER_MEMBER_DAY in any 24 hours, the registration's own invite included.
INVITE_RESEND_INTERVAL = timedelta(minutes=2)
INVITES_PER_MEMBER_DAY = 5
PENDING_STATUSES = (SubscriptionStatus.PENDING_OTP, SubscriptionStatus.PENDING_PAYMENT)
STATUS_TABS = (("", "All"), ("active", "Active"), ("expired", "Expired"), ("none", "No membership"))
# "Safety-relevant first" - the order of app/services/health.safety_first(), spelled out here from plain columns so it
# can also rank inside a window: the oldest entry of every distinct tag (the tags are what the exercise guardrails
# read), then the other tagged entries, then injuries, conditions, allergies and medications - oldest first. A padded
# profile (untagged entries, or one tag over and over) can't push a safety tag out of what a page reads.
SAFETY_KINDS = (ConditionKind.INJURY, ConditionKind.CONDITION, ConditionKind.ALLERGY, ConditionKind.MEDICATION)


def _safety_keys() -> tuple[Any, ...]:
    """(untagged, repeated tag, kind rank, id) of a member's health entry - sort ascending. The second key is a window
    over the member's entries: a statement's ORDER BY or select list only, never inside another window."""
    repeat = case((HealthCondition.tag.is_(None), false()),  # an untagged entry never repeats a tag
                  else_=func.row_number().over(partition_by=HealthCondition.tag, order_by=HealthCondition.id) > 1)
    kind = case(*((HealthCondition.kind == k, i) for i, k in enumerate(SAFETY_KINDS)), else_=len(SAFETY_KINDS))
    return HealthCondition.tag.is_(None), repeat, kind, HealthCondition.id


# ====================================================================== small helpers
def _int(value: Any) -> int | None:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if 0 < n < 2**62 else None


def _page(value: Any) -> int:
    n = _int(value)
    return n if n is not None and n < 100000 else 1


def _like(q: str) -> str:
    return "%" + re.sub(r"([\\%_])", r"\\\1", q) + "%"


def _years_ago(today: date, years: int) -> date:
    try:
        return today.replace(year=today.year - years)
    except ValueError:  # 29 Feb
        return today.replace(year=today.year - years, day=28)


async def _owned_gyms(db: DB, owner: User) -> list[Gym]:
    return list((await db.execute(select(Gym).where(Gym.owner_id == owner.id).order_by(Gym.name))).scalars().all())


def _default_country(gyms: list[Gym], owner: User) -> str:
    if gyms and gyms[0].country_code:
        return gyms[0].country_code
    return owner.country_code or "IN"


def _active_sub_clause(today: date) -> list[Any]:
    return [Subscription.status == SubscriptionStatus.ACTIVE,
            or_(Subscription.ends_on.is_(None), Subscription.ends_on >= today)]


def membership_summary(subs: list[Subscription], today: date) -> dict[str, Any]:
    """State of a member's relationship with the owner's gyms: active | pending | expired | none.
    ``subs`` must have ``plan`` and ``gym`` eager-loaded (newest first)."""
    active = [s for s in subs if s.status == SubscriptionStatus.ACTIVE and (s.ends_on is None or s.ends_on >= today)]
    pending = [s for s in subs if s.status in PENDING_STATUSES]
    if active:
        current = max(active, key=lambda s: (s.ends_on or date.max))
        state = "active"
    elif pending:
        current, state = pending[0], "pending"
    elif subs:
        current, state = subs[0], "expired"
    else:
        current, state = None, "none"
    days_left = None
    if current is not None and state == "active" and current.ends_on is not None:
        days_left = (current.ends_on - today).days
    return {
        "state": state,
        "current": current,
        "plan": current.plan.name if current is not None else None,
        "gym": current.gym.name if current is not None else None,
        "ends_on": current.ends_on if current is not None else None,
        "days_left": days_left,
        "gyms": sorted({s.gym.name for s in subs}),
        "count": len(subs),
    }


async def _open_reports(db: DB, owner: User) -> int:
    return int(await db.scalar(
        select(func.count(MisconductReport.id)).join(Gym, Gym.id == MisconductReport.gym_id)
        .where(Gym.owner_id == owner.id, MisconductReport.status == ReportStatus.OPEN)
    ) or 0)


def _related_clause(owner_id: int, gym_id: int | None = None) -> Any:
    """Same members as ``intake.related_customers_clause`` - customers with a current, consented subscription at the
    owner's gyms (or at ``gym_id``) or registered by the owner - but as ONE ``IN (... UNION ...)`` sub-select, which PostgreSQL turns
    into a semi-join driven by the owner's own subscriptions/registrations. (Two OR'ed IN-sublinks cannot be
    pulled up: every customer of the platform would be filtered on each summary/tag/count/page query.)"""
    owned = select(Gym.id).where(Gym.owner_id == owner_id)
    consented = svc.consented_subscription_clause()  # current or member-started, or ended within FORMER_MEMBER_DAYS
    if gym_id is not None:
        ids: Any = select(Subscription.customer_id).where(Subscription.gym_id == gym_id, Subscription.gym_id.in_(owned),
                                                          consented)
    else:
        ids = union(select(Subscription.customer_id).where(Subscription.gym_id.in_(owned), consented),
                    select(CustomerProfile.user_id).where(CustomerProfile.registered_by_id == owner_id))
    return and_(User.role == Role.CUSTOMER, User.id.in_(ids))


def _current_subscriptions(customer_ids: Any, gym_ids: list[int], today: date) -> Any:
    """Per member, the one subscription ``membership_summary`` picks - the active one ending last, else the newest
    pending, else the newest - with plan and gym: DISTINCT ON (customer_id), one row per member, never the history."""
    active = and_(*_active_sub_clause(today))
    prio = case((active, 0), (Subscription.status.in_(PENDING_STATUSES), 1), else_=2)
    ends = case((active, func.coalesce(Subscription.ends_on, date.max)))
    return (
        select(Subscription).options(joinedload(Subscription.plan), joinedload(Subscription.gym))
        .where(Subscription.customer_id.in_(customer_ids), Subscription.gym_id.in_(gym_ids or [-1]))
        .distinct(Subscription.customer_id)
        .order_by(Subscription.customer_id, prio, ends.desc(), Subscription.created_at.desc(), Subscription.id.desc())
    )


def _health_labels(customer_ids: Any) -> Any:
    """Per member: the first HEALTH_LABELS_SHOWN distinct health labels (a tag's label, else the entry's name, in
    (kind, name) order - as the list shows them) and how many distinct labels the member has. At most
    HEALTH_LABELS_SHOWN rows per member come back, and each member's entries are read through a LATERAL
    ``... ORDER BY safety LIMIT CONDITIONS_SHOWN``: however many entries a member recorded, at most that many are
    read, the safety-relevant ones first."""
    label = case(TAG_LABELS, value=HealthCondition.tag, else_=HealthCondition.name)
    members = select(User.id.label("mid")).where(User.id.in_(customer_ids)).subquery("m")
    picked = (select(HealthCondition.customer_id.label("cid"), label.label("label"), HealthCondition.kind.label("kind"),
                     HealthCondition.name.label("name"))
              .where(HealthCondition.customer_id == members.c.mid)
              .order_by(*_safety_keys()).limit(CONDITIONS_SHOWN).lateral("hc"))
    raw = select(picked.c.cid, picked.c.label, picked.c.kind, picked.c.name).select_from(members).join(picked, true()).subquery()
    firsts = (select(raw.c.cid, raw.c.label, raw.c.kind, raw.c.name)
              .distinct(raw.c.cid, raw.c.label).order_by(raw.c.cid, raw.c.label, raw.c.kind, raw.c.name).subquery())
    ranked = select(firsts.c.cid, firsts.c.label,
                    func.row_number().over(partition_by=firsts.c.cid, order_by=(firsts.c.kind, firsts.c.name)).label("rn"),
                    func.count().over(partition_by=firsts.c.cid).label("n")).subquery()
    return (select(ranked.c.cid, ranked.c.label, ranked.c.n).where(ranked.c.rn <= HEALTH_LABELS_SHOWN)
            .order_by(ranked.c.cid, ranked.c.rn))


# ====================================================================== members list
@router.get("/owner/customers")
async def customers_list(request: Request, db: DB, owner: OwnerUser, q: str = "", gym: str = "", status: str = "",
                         minors: str = "", paused: str = "", tag: str = "", sort: str = "", page: str = "1") -> Any:
    """Constant number of queries regardless of how many members are shown. Every query is driven by the owner's own
    members (one semi-join sub-select, ``_related_clause``), and the page's members bring only their current
    subscription and their first health labels + a count - never their whole history."""
    gyms = await _owned_gyms(db, owner)
    gym_ids = [g.id for g in gyms]
    gym_id = _int(gym)
    gym_id = gym_id if gym_id in gym_ids else None
    today = local_today(owner.timezone)
    minor_cutoff = _years_ago(today, svc.MINOR_AGE)
    q = svc.clean_line(q, 80)
    status = status if status in {k for k, _ in STATUS_TABS} else ""
    only_minors = minors == "1"
    only_paused = paused == "1"
    tag = tag if tag in TAG_LABELS else ""
    sort = sort if sort in ("name", "oldest") else ""

    sub_gyms = [gym_id] if gym_id else (gym_ids or [-1])
    active_subq = select(Subscription.customer_id).where(Subscription.gym_id.in_(sub_gyms), *_active_sub_clause(today))
    any_subq = select(Subscription.customer_id).where(Subscription.gym_id.in_(sub_gyms))
    base = [_related_clause(owner.id, gym_id), User.deleted_at.is_(None)]

    # ---- summary tiles (one query)
    stats_row = (await db.execute(
        select(
            func.count(User.id),
            func.count(User.id).filter(User.id.in_(active_subq)),
            func.count(User.id).filter(User.date_of_birth > minor_cutoff),
            func.count(User.id).filter(CustomerProfile.activity_suspended.is_(True)),
        ).select_from(User).outerjoin(CustomerProfile, CustomerProfile.user_id == User.id).where(*base)
    )).one()
    stats = {"total": stats_row[0], "active": stats_row[1], "minors": stats_row[2], "paused": stats_row[3]}

    # ---- condition tags present among these members (filter options, one query)
    member_count = func.count(distinct(HealthCondition.customer_id))
    tag_rows = (await db.execute(
        select(HealthCondition.tag, member_count)
        .where(HealthCondition.tag.is_not(None), HealthCondition.customer_id.in_(select(User.id).where(*base)))
        .group_by(HealthCondition.tag).order_by(member_count.desc(), HealthCondition.tag)
    )).all()
    tag_options = [(t, TAG_LABELS.get(t, t), n) for t, n in tag_rows]

    clauses = list(base)
    if status == "active":
        clauses.append(User.id.in_(active_subq))
    elif status == "expired":
        clauses += [User.id.in_(any_subq), User.id.not_in(active_subq)]
    elif status == "none":
        clauses.append(User.id.not_in(any_subq))
    if only_minors:
        clauses.append(User.date_of_birth > minor_cutoff)
    if only_paused:
        clauses.append(User.id.in_(select(CustomerProfile.user_id).where(CustomerProfile.activity_suspended.is_(True))))
    if tag:
        clauses.append(User.id.in_(select(HealthCondition.customer_id).where(HealthCondition.tag == tag)))
    if q:
        like = _like(q)
        conds = [User.full_name.ilike(like, escape="\\"), User.email.ilike(like, escape="\\"),
                 User.username.ilike(like, escape="\\"), User.phone.ilike(like, escape="\\")]
        digits = re.sub(r"\D", "", q)
        if len(digits) >= 4:
            conds.append(User.phone_normalized.like(f"%{digits}%"))
        mid = parse_member_id(q)
        if mid is not None:
            conds.append(User.id == mid)
        clauses.append(or_(*conds))

    order = {"name": (User.full_name.asc(), User.id.asc()), "oldest": (User.created_at.asc(), User.id.asc())}.get(
        sort, (User.created_at.desc(), User.id.desc()))
    p = await paginate(db, select(User).options(joinedload(User.customer_profile)).where(*clauses).order_by(*order),
                       page=_page(page), per_page=PAGE_SIZE, count_stmt=select(func.count(User.id)).where(*clauses),
                       unique=True)
    users, total, page_n, pages = list(p.items), p.total, p.page, p.pages

    ids = [u.id for u in users] or [-1]  # one page: at most PAGE_SIZE ids
    current = {s.customer_id: s for s in (await db.execute(
        _current_subscriptions(ids, gym_ids, today))).unique().scalars().all()}
    health: dict[int, tuple[list[str], int]] = {}
    for cid, label, n in (await db.execute(_health_labels(ids))).all():
        health.setdefault(cid, ([], int(n)))[0].append(label)

    rows = []
    for u in users:
        labels, n_labels = health.get(u.id, ([], 0))
        sub = current.get(u.id)
        age = u.age(today)
        rows.append({
            "user": u, "profile": u.customer_profile, "member_id": member_id(u), "age": age,
            "minor": age is not None and age < svc.MINOR_AGE,
            "membership": membership_summary([sub] if sub is not None else [], today),
            "health": labels, "health_more": max(0, n_labels - len(labels)),
            "paused": bool(u.customer_profile is not None and u.customer_profile.activity_suspended),
        })

    filters = {"q": q, "gym": gym_id or "", "status": status, "minors": "1" if only_minors else "",
               "paused": "1" if only_paused else "", "tag": tag, "sort": sort}

    def url(**over: Any) -> str:
        params: dict[str, Any] = dict(filters, page=page_n)
        params.update(over)
        clean = {k: v for k, v in params.items() if v not in ("", None) and not (k == "page" and v == 1)}
        return "/owner/customers" + (("?" + urlencode(clean)) if clean else "")

    tabs = [{"label": label, "url": url(status=key, page=1), "active": status == key} for key, label in STATUS_TABS]
    return render(request, "owner/customers/list.html", {
        "gyms": gyms, "rows": rows, "stats": stats, "tag_options": tag_options, "filters": filters, "tabs": tabs,
        "total": total, "page": page_n, "pages": pages,
        "prev_url": url(page=page_n - 1) if page_n > 1 else None,
        "next_url": url(page=page_n + 1) if page_n < pages else None,
        "toggle_minors": url(minors="" if only_minors else "1", page=1),
        "toggle_paused": url(paused="" if only_paused else "1", page=1),
        "has_filters": bool(q or gym_id or status or only_minors or only_paused or tag),
        "open_reports": await _open_reports(db, owner),
    })


# ====================================================================== registration (manual + AI intake)
def _form_defaults(gyms: list[Gym], owner: User) -> dict[str, str]:
    country = _default_country(gyms, owner)
    return {"country_code": country, "id_country": country, "goal": Goal.GENERAL_FITNESS.value,
            "gym_id": str(gyms[0].id) if gyms else "", "intake_source": "owner_manual",
            "id_doc_type": "aadhaar" if country == "IN" else "national_id"}


def _new_ctx(gyms: list[Gym], values: dict[str, str], errors: dict[str, str]) -> dict[str, Any]:
    return {
        "gyms": gyms, "values": values, "errors": errors, "ai_feature": True,
        "genders": svc.GENDERS, "goals": list(svc.GOAL_LABELS.items()), "relations": svc.RELATIONS,
        "list_labels": svc.LIST_LABELS, "text_max": 4000, "text_min": 10, "today": date.today().isoformat(),
    }


@router.get("/owner/customers/new")
async def customer_new(request: Request, db: DB, owner: OwnerUser) -> Any:
    gyms = await _owned_gyms(db, owner)
    return render(request, "owner/customers/new.html", _new_ctx(gyms, _form_defaults(gyms, owner), {}))


def _has_file(value: Any) -> bool:
    return bool(getattr(value, "filename", None)) and hasattr(value, "read")


def _probe_image(data: bytes) -> str | None:
    """Cheap pre-check so a bad ID photo is reported before anything is written."""
    from PIL import Image

    if data.startswith(b"%PDF-"):
        return "Please upload a photo (JPG, PNG or WEBP) of the ID card, not a PDF."
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
    except Exception:
        return "That file is not a valid image."
    return None


async def _read_id_image(value: Any, *, required: bool) -> tuple[bytes | None, str | None]:
    if not _has_file(value):
        return None, ("Upload a photo of the front of the member's ID card - it's mandatory." if required else None)
    try:
        data = await storage.read_upload(value)
    except storage.UploadError as exc:
        return None, str(exc)
    problem = await asyncio.to_thread(_probe_image, data)
    return (None, problem) if problem else (data, None)


@router.post("/owner/customers/new")
async def customer_create(request: Request, db: DB, owner: OwnerUser) -> Any:
    gyms = await _owned_gyms(db, owner)
    if not gyms:
        return redirect("/owner/gyms/new", "Create your gym first - members are registered to one of your gyms.", "warning")
    form = await request.form()
    values, errors, data = svc.parse_registration(form, gyms=gyms, default_country=_default_country(gyms, owner),
                                                  today=local_today(owner.timezone))
    front, err = await _read_id_image(form.get("id_front"), required=True)
    if err:
        errors["id_front"] = err
    back, err = await _read_id_image(form.get("id_back"), required=False)
    if err:
        errors["id_back"] = err
    if data is not None and not errors and front is not None:
        try:  # members an owner registers per day (approved cap; held until this request commits the member)
            await caps.enforce(db, "members_added_per_owner_day", owner.id, select(CustomerProfile.user_id).where(
                CustomerProfile.registered_by_id == owner.id, CustomerProfile.created_at >= caps.since()))
        except caps.CapReached as exc:
            errors["form"] = str(exc)
    if data is not None and not errors and front is not None:
        gym = next((g for g in gyms if g.id == data.gym_id), gyms[0])
        try:
            async with db.begin_nested():  # a failure leaves nothing behind (and keeps the session usable)
                result = await svc.register_customer(db, owner=owner, data=data, gym=gym, id_front=front, id_back=back,
                                                     request=request)
        except svc.IntakeError as exc:
            errors[exc.field if exc.field in ("email", "phone") else "email"] = str(exc)
        except storage.UploadError as exc:
            errors["id_front"] = str(exc)
        except IntegrityError:
            errors["email"] = "An account with this e-mail address or phone number already exists."
        else:
            await db.commit()
            first = result.user.full_name.split(" ")[0]
            if result.invite_sent:
                msg = f"{first} is registered. We e-mailed an invite to set their password (valid 7 days)."
            else:
                msg = f"{first} is registered. No e-mail address was given, so no invite was sent."
            return redirect(f"/owner/customers/{result.user.id}", msg, "success")
    return render(request, "owner/customers/new.html", _new_ctx(gyms, values, errors), status_code=400)


@router.post("/owner/customers/intake")
async def customer_intake(request: Request, db: DB, owner: OwnerUser) -> Any:
    """AI intake: free text -> validated prefill for the registration form (nothing is saved here).

    Always JSON. An AI guard / queue refusal (quota, cooldown, too_many, duplicate, spam) answers with the refusal
    JSON of docs/ai-guard-and-queue.md section 7 (429/409/422 + Retry-After); a full AI queue ('busy') and AI being
    offline fall back to the deterministic parser; an owner who left gets 503 'cancelled' (nobody is waiting).
    The request's DB session is closed before the AI call, so no pooled connection is held while the owner waits in
    the AI queue or for the provider."""
    from app.ai.errors import AICancelled, AIRejected
    from app.ai.intake import TEXT_MAX, TEXT_MIN, run_intake
    from app.core.errors import AI_CANCELLED_MESSAGE

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid input", "detail": 'Send JSON like {"text": "..."}.'}, status_code=400)
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str) or len(text.strip()) < TEXT_MIN:
        return JSONResponse({"error": "Invalid input",
                             "detail": f"Describe the member in at least {TEXT_MIN} characters."}, status_code=400)
    if len(text) > TEXT_MAX:
        return JSONResponse({"error": "Invalid input",
                             "detail": f"Keep the description under {TEXT_MAX:,} characters."}, status_code=400)
    gyms = await _owned_gyms(db, owner)
    country, today = _default_country(gyms, owner), local_today(owner.timezone)
    await db.close()  # everything the prefill needs is read and nothing is written: no pooled connection while it waits
    try:
        outcome = await run_intake(text, country=country, today=today)
    except AIRejected as exc:  # JSON whatever the Accept header says: this endpoint only ever answers JSON
        return JSONResponse(exc.payload(), status_code=exc.status, headers={**exc.headers(), "Cache-Control": "no-store"})
    except AICancelled:  # the owner left (tab closed, connection lost, logged out): the AI work was stopped
        return JSONResponse({"error": "Cancelled", "detail": AI_CANCELLED_MESSAGE, "code": "cancelled"}, status_code=503,
                            headers={"Cache-Control": "no-store"})
    return JSONResponse(outcome.as_dict(), headers={"Cache-Control": "no-store"})


# ====================================================================== member detail
async def _load_member(db: DB, owner: User, customer_id: int) -> User:
    user = (await db.execute(
        select(User).options(joinedload(User.customer_profile))
        .where(User.id == customer_id, User.deleted_at.is_(None), svc.related_customers_clause(owner.id))
    )).unique().scalar_one_or_none()
    if user is None:
        raise NotFound("Member not found.")
    return user


def _sparkline(values: list[float], width: int = 320, height: int = 72, pad: int = 6) -> dict[str, Any] | None:
    if len(values) < 2:
        return None
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    step = (width - 2 * pad) / (len(values) - 1)
    pts = [(pad + i * step, pad + (height - 2 * pad) * (1 - (v - lo) / span)) for i, v in enumerate(values)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"{pts[0][0]:.1f},{height - pad} " + line + f" {pts[-1][0]:.1f},{height - pad}"
    return {"points": line, "area": area, "width": width, "height": height,
            "last": (round(pts[-1][0], 1), round(pts[-1][1], 1))}


def _metric_trend(entries: list[MetricEntry]) -> dict[str, Any] | None:
    if not entries:
        return None
    first, last = entries[0], entries[-1]
    return {
        "entries": entries, "latest": last, "first": first, "count": len(entries),
        "weight_spark": _sparkline([e.weight_kg for e in entries]), "bmi_spark": _sparkline([e.bmi for e in entries]),
        "weight_delta": round(last.weight_kg - first.weight_kg, 1) if len(entries) > 1 else None,
        "bmi_delta": round(last.bmi - first.bmi, 1) if len(entries) > 1 else None,
        "flagged": sum(1 for e in entries if getattr(e.anomaly_level, "value", e.anomaly_level) != "none"),
    }


async def _detail_ctx(db: DB, owner: User, user: User, *, health_values: dict[str, str] | None = None,
                      health_errors: dict[str, str] | None = None, note: str = "",
                      note_error: str | None = None, subs_page: int = 1) -> dict[str, Any]:
    today = local_today(owner.timezone)
    gym_ids = [gid for (gid,) in (await db.execute(select(Gym.id).where(Gym.owner_id == owner.id))).all()] or [-1]
    profile = user.customer_profile
    # memberships: one page (SUBS_PAGE_SIZE, newest first) + the current one for the status badge
    subs_where = (Subscription.customer_id == user.id, Subscription.gym_id.in_(gym_ids))
    subs = await paginate(
        db,
        select(Subscription)
        .options(joinedload(Subscription.plan), joinedload(Subscription.gym), joinedload(Subscription.branch),
                 joinedload(Subscription.trainer))
        .where(*subs_where).order_by(Subscription.created_at.desc(), Subscription.id.desc()),
        page=subs_page, per_page=SUBS_PAGE_SIZE, count_stmt=select(func.count(Subscription.id)).where(*subs_where),
        unique=True,
    )
    current = (await db.execute(_current_subscriptions([user.id], gym_ids, today))).unique().scalars().first()
    # health entries: at most CONDITIONS_PER_KIND per kind and CONDITIONS_SHOWN in all (LIMIT), safety-relevant first -
    # each kind's first entry before any second one, so every kind the member has shows with its total ("+N more")
    keys = (select(HealthCondition.id.label("cid"), HealthCondition.kind.label("kind"),
                   *(k.label(f"k{i}") for i, k in enumerate(_safety_keys())))
            .where(HealthCondition.customer_id == user.id).subquery("keys"))
    order = (keys.c.k0, keys.c.k1, keys.c.k2, keys.c.k3)
    ranked = select(keys.c.cid, *order, func.row_number().over(partition_by=keys.c.kind, order_by=order).label("rn"),
                    func.count().over(partition_by=keys.c.kind).label("n")).subquery("ranked")
    condition_rows = sorted((await db.execute(
        select(HealthCondition, ranked.c.n).join(ranked, ranked.c.cid == HealthCondition.id)
        .where(ranked.c.rn <= CONDITIONS_PER_KIND)
        .order_by(ranked.c.rn > 1, ranked.c.k0, ranked.c.k1, ranked.c.k2, ranked.c.k3)
        .limit(CONDITIONS_SHOWN)
    )).all(), key=lambda row: (str(row[0].kind.value), row[0].name.casefold(), row[0].id))  # shown by kind, then name
    conditions = [c for c, _n in condition_rows]
    kind_totals = {svc.KIND_TO_FIELD.get(c.kind, "conditions"): int(n) for c, n in condition_rows}
    metrics = list((await db.execute(
        select(MetricEntry).where(MetricEntry.customer_id == user.id, MetricEntry.recorded_on >= today - timedelta(days=30))
        .order_by(MetricEntry.recorded_on)
    )).scalars().all())
    alerts = list((await db.execute(
        select(HealthAlert).where(HealthAlert.customer_id == user.id)
        .order_by(HealthAlert.created_at.desc(), HealthAlert.id.desc()).limit(6)
    )).scalars().all())
    events = (await db.execute(
        select(SuspensionEvent, User.full_name, User.deleted_at)
        .outerjoin(User, User.id == SuspensionEvent.actor_user_id)
        .where(SuspensionEvent.customer_id == user.id)
        .order_by(SuspensionEvent.created_at.desc(), SuspensionEvent.id.desc()).limit(10)
    )).all()
    history = [{"event": ev, "actor_name": ("Deleted user" if deleted else name) if name else None}
               for ev, name, deleted in events]
    id_doc = (await db.execute(
        select(IdentityDocument).where(IdentityDocument.user_id == user.id)
        .order_by(IdentityDocument.created_at.desc(), IdentityDocument.id.desc()).limit(1)
    )).scalar_one_or_none()
    threads = list((await db.execute(
        select(ChatThread).options(joinedload(ChatThread.employee), joinedload(ChatThread.gym))
        .where(ChatThread.customer_id == user.id, ChatThread.gym_id.in_(gym_ids))
        .order_by(func.coalesce(ChatThread.last_message_at, ChatThread.created_at).desc(), ChatThread.id.desc())
        .limit(THREADS_SHOWN)
    )).unique().scalars().all())
    # the trainer-safety panel (what to avoid, contraindicated exercises, last update): the owner sees it in full -
    # _load_member already proved the member belongs to one of their gyms; built from the entries read above
    safety = trainer_safety.build_brief(user, conditions, profile)
    registrar = None
    if profile is not None and profile.registered_by_id:
        registrar = await db.scalar(select(User.full_name).where(User.id == profile.registered_by_id))
    invite_at = await svc.last_invite_at(db, user.id) if user.password_hash is None else None
    reports = int(await db.scalar(
        select(func.count(MisconductReport.id)).where(MisconductReport.reporter_id == user.id,
                                                      MisconductReport.gym_id.in_(gym_ids))
    ) or 0)

    grouped: dict[str, list[HealthCondition]] = {name: [] for name in svc.LIST_FIELDS}
    for c in conditions:
        grouped[svc.KIND_TO_FIELD.get(c.kind, "conditions")].append(c)
    more_conditions = {k: n - len(grouped.get(k, [])) for k, n in kind_totals.items() if n > len(grouped.get(k, []))}
    age = user.age(today)
    goal = getattr(getattr(profile, "goal", None), "value", "") if profile is not None else ""
    return {
        "member": user, "profile": profile, "member_id": member_id(user), "age": age,
        "minor": age is not None and age < svc.MINOR_AGE,
        "membership": membership_summary([current] if current is not None else [], today), "subs": subs,
        "subs_path": f"/owner/customers/{user.id}", "grouped": grouped, "more_conditions": more_conditions,
        "conditions_count": sum(kind_totals.values()), "trend": _metric_trend(metrics), "alerts": alerts,
        "history": history,
        "id_doc": id_doc, "threads": threads, "registrar": registrar, "invite_at": invite_at, "reports": reports,
        "goal_label": svc.GOAL_LABELS.get(goal, "General fitness"),
        "gender_label": svc.GENDER_LABELS.get(user.gender or "", user.gender or None),
        # one entry per line + ``health_seen``, the signed snapshot of exactly the entries shown: the save is applied as
        # changes against it (intake.update_health), so entries added meanwhile - by the member or "Remember this" -
        # and entries the form does not list are kept
        "health_values": (health_values if health_values is not None
                          else svc.health_form_values(user.id, conditions, profile)),
        "health_errors": health_errors or {}, "open_health": bool(health_errors),
        "note": note, "note_error": note_error, "note_min": NOTE_MIN, "relations": svc.RELATIONS,
        "list_labels": svc.LIST_LABELS, "tag_labels": TAG_LABELS, "id_verified": user.id_status == IdStatus.VERIFIED,
        "safety": safety, "sharing": bool(profile is None or profile.share_health_with_trainers is not False),
    }


@router.get("/owner/customers/{customer_id}")
async def customer_detail(request: Request, db: DB, owner: OwnerUser, customer_id: IdParam) -> Any:
    user = await _load_member(db, owner, customer_id)
    return render(request, "owner/customers/detail.html",
                  await _detail_ctx(db, owner, user, subs_page=parse_page(request).page))


@router.post("/owner/customers/{customer_id}/health")
async def customer_health(request: Request, db: DB, owner: OwnerUser, customer_id: IdParam) -> Any:
    user = await _load_member(db, owner, customer_id)
    form = await request.form()
    gyms = await _owned_gyms(db, owner)
    country = user.country_code or _default_country(gyms, owner)
    values, errors, data = svc.parse_health(form, minor=user.is_minor, country=country)
    if errors:
        ctx = await _detail_ctx(db, owner, user, health_values=values, health_errors=errors)
        return render(request, "owner/customers/detail.html", ctx, status_code=400)
    profile = user.customer_profile
    if profile is None:
        profile = CustomerProfile(user_id=user.id, activity_suspended=False)
        db.add(profile)
        await db.flush()
    try:
        # SAVEPOINT: a save that cannot be applied undoes nothing else and keeps the loaded rows usable for the form
        async with db.begin_nested():
            changed = await svc.update_health(db, owner=owner, customer=user, profile=profile, data=data,
                                              request=request)
    except svc.HealthEditRefused as exc:  # too many new entries, past the member's total, an invalid entry
        ctx = await _detail_ctx(db, owner, user, health_values=values, health_errors={exc.field: str(exc)})
        return render(request, "owner/customers/detail.html", ctx, status_code=400)
    notes = _health_save_notes(data)
    if not changed:
        return redirect(f"/owner/customers/{user.id}#health", " ".join(["Nothing changed.", *notes]), "info")
    await db.commit()
    who = user.full_name.split(" ")[0]
    extra = " and their guardian" if user.is_minor and profile.guardian_email else ""
    return redirect(f"/owner/customers/{user.id}#health",
                    " ".join([f"Health profile updated - {who}{extra} was notified.", *notes]), "success")


def _health_save_notes(data: Any) -> list[str]:
    """What the owner's save left as it was, in words: entries the member added themselves (a form without a snapshot
    never removes them) and entries the member removed after the page was opened (never added back)."""
    notes: list[str] = []
    kept = list(dict.fromkeys(getattr(data, "kept", None) or []))
    skipped = list(dict.fromkeys(getattr(data, "skipped", None) or []))
    if kept:
        notes.append(f"Kept {len(kept)} {'entry' if len(kept) == 1 else 'entries'} the member added themselves - only "
                     "they can remove them.")
    if skipped:
        shown = ", ".join(clamp(s, 40) for s in skipped[:3]) + (f" (+{len(skipped) - 3} more)" if len(skipped) > 3 else "")
        notes.append(f"Not added back: {shown} - the member removed {'it' if len(skipped) == 1 else 'them'} after you "
                     "opened the page.")
    return notes


@router.post("/owner/customers/{customer_id}/reactivate")
async def customer_reactivate(request: Request, db: DB, owner: OwnerUser, customer_id: IdParam) -> Any:
    user = await _load_member(db, owner, customer_id)
    profile = user.customer_profile
    if profile is None or not profile.activity_suspended:
        return redirect(f"/owner/customers/{user.id}", "This member's gym activities are not paused.", "info")
    form = await request.form()
    raw = form.get("note")
    note = svc.clean_block(raw if isinstance(raw, str) else "", NOTE_MAX + 200)
    error: str | None = None
    if len(note) < NOTE_MIN:
        error = f"Explain why it's safe to reactivate (at least {NOTE_MIN} characters), e.g. a doctor's clearance."
    elif len(note) > NOTE_MAX:
        error = f"Keep the note under {NOTE_MAX:,} characters."
    if error is None:
        try:
            from app.services.health import reactivate_customer
        except ImportError:
            return redirect(f"/owner/customers/{user.id}", "Reactivation isn't available yet - please try again later.", "error")
        try:
            async with db.begin_nested():
                await reactivate_customer(db, user, owner, note)
        except PermissionError as exc:
            return redirect(f"/owner/customers/{user.id}", str(exc), "error")
        except ValueError as exc:
            error = str(exc)
        else:
            await db.commit()
            first = user.full_name.split(" ")[0]
            return redirect(f"/owner/customers/{user.id}", f"{first}'s gym activities are active again - they were notified.",
                            "success")
    ctx = await _detail_ctx(db, owner, user, note=note, note_error=error)
    return render(request, "owner/customers/detail.html", ctx, status_code=400)


@router.post("/owner/customers/{customer_id}/invite")
async def customer_invite(request: Request, db: DB, owner: OwnerUser, customer_id: IdParam) -> Any:
    user = await _load_member(db, owner, customer_id)
    if user.password_hash is not None:
        return redirect(f"/owner/customers/{user.id}", "This member has already activated their account.", "info")
    if not user.email:
        return redirect(f"/owner/customers/{user.id}", "This member has no e-mail address, so an invite can't be sent.",
                        "warning")
    # One invite decision per member at a time (a transaction-scoped lock, held until COMMIT): parallel clicks from
    # several tabs, a second admin or a script wait here and then see the invite the first one sent - so the
    # throttle and the daily cap hold, and exactly one link stays valid.
    await caps.lock(db, "member_invite", user.id)
    if await db.scalar(select(User.password_hash).where(User.id == user.id)) is not None:  # activated meanwhile
        return redirect(f"/owner/customers/{user.id}", "This member has already activated their account.", "info")
    last, recent = (await db.execute(
        select(func.max(AuthToken.created_at), func.count(AuthToken.id).filter(AuthToken.created_at >= caps.since()))
        .where(AuthToken.purpose == TokenPurpose.INVITE, AuthToken.user_id == user.id)
    )).one()
    if last is not None and utcnow() - last < INVITE_RESEND_INTERVAL:
        return redirect(f"/owner/customers/{user.id}", "An invite was sent a moment ago - please wait a little.", "info")
    if int(recent or 0) >= INVITES_PER_MEMBER_DAY:
        return redirect(f"/owner/customers/{user.id}",
                        f"This member has already been sent {INVITES_PER_MEMBER_DAY} invites in the last 24 hours - the "
                        "most for one day. Ask them to check their spam folder, or try again tomorrow.", "warning")
    gym_name = await db.scalar(select(Gym.name).where(Gym.owner_id == owner.id).order_by(Gym.id).limit(1)) or "your gym"
    await svc.send_invite(db, user, owner=owner, gym_name=gym_name)
    await audit(db, owner, "customer.invite", "user", user.id, request=request)
    await db.commit()
    return redirect(f"/owner/customers/{user.id}", f"Invite e-mailed to {user.email}.", "success")


# ====================================================================== audit log
ACTION_LABELS = {
    "customer.register": "Registered a member", "customer.health_update": "Updated a member's health profile",
    "customer.invite": "Re-sent a member invite", "health.reactivate": "Reactivated a paused member",
    "health.suspend": "Member paused for safety", "report.create": "Misconduct report filed",
    "report.accept": "Accepted a misconduct report", "report.decline": "Declined a misconduct report",
    "chat.close": "Closed a conversation", "chat.reopen": "Reopened a conversation",
    "account.soft_delete": "Account deleted", "account.invite_accepted": "Member activated their account",
}
AREA_ICONS = {
    "customer": "user-group", "health": "heart", "report": "flag", "chat": "chat", "staff": "users", "employee": "users",
    "payroll": "banknotes", "finance": "banknotes", "salary": "banknotes", "gym": "building", "branch": "building",
    "plan": "sparkles", "plans": "sparkles", "subscription": "receipt", "checkout": "receipt", "account": "user",
    "security": "shield", "identity": "id-card", "shop": "credit-card", "job": "briefcase", "jobs": "briefcase",
}
ENTITY_LINKS = {
    "user": "/profile/{id}", "misconduct_report": "/owner/reports/{id}", "chat_thread": "/chat/{id}",
    "employee": "/owner/staff/{id}", "gym": "/owner/gyms/{id}", "branch": "/owner/branches/{id}",
    "salary_statement": "/owner/payroll/{id}", "job_posting": "/owner/jobs/{id}",
}
# the area filter's options: the action prefixes the app writes (a fixed list - no DISTINCT over the whole log)
AUDIT_AREAS = ("account", "ai", "branch", "chat", "customer", "gym", "health", "identity", "job", "live", "payroll",
               "plans", "report", "security", "shop", "staff", "subscriptions")


def action_label(action: str) -> str:
    if action in ACTION_LABELS:
        return ACTION_LABELS[action]
    return action.replace(".", " · ").replace("_", " ").capitalize()


def _detail_pairs(detail: Any) -> list[tuple[str, str]]:
    if not isinstance(detail, dict):
        return []
    pairs = []
    for k, v in list(detail.items())[:6]:
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        elif isinstance(v, dict):
            v = ", ".join(f"{a}: {b}" for a, b in v.items())
        pairs.append((str(k).replace("_", " "), clamp(str(v), 120)))
    return pairs


@router.get("/owner/audit")
async def owner_audit(request: Request, db: DB, owner: OwnerUser, gym: str = "", area: str = "", who: str = "",
                      q: str = "", page: str = "1") -> Any:
    gyms = await _owned_gyms(db, owner)
    gym_ids = [g.id for g in gyms]
    gym_id = _int(gym)
    gym_id = gym_id if gym_id in gym_ids else None
    # what an owner may read: every row of their gyms, and their own actions that belong to no gym (account, invites)
    # - never a row of another tenant's gym (a side effect of their action there), nor the platform's own trust &
    # safety records of the account they reported (bans, the person's banned network addresses)
    # "no gym" as coalesce(gym_id, 0) = 0 (ids start at 1): this arm is read through ix_audit_logs_actor_created (the
    # owner's own rows) - never through the gym index's NULL range, which holds every account-level row of the platform
    own_actions = and_(AuditLog.actor_id == owner.id, func.coalesce(AuditLog.gym_id, 0) == 0,
                       ~AuditLog.action.like("trust.%"))
    scope = or_(AuditLog.gym_id.in_(gym_ids or [-1]), own_actions)
    area_col = func.split_part(AuditLog.action, ".", 1)
    areas = list(AUDIT_AREAS)
    area = area if area in areas else ""
    who = who if who in ("me", "others") else ""
    q = svc.clean_line(q, 80)

    # a gym of this owner / the owner's own actions imply the scope: the filter alone lets ix_audit_logs_gym_created /
    # ix_audit_logs_actor_created read the page newest first (no bitmap scan of all the owner's rows + sort)
    clauses = [] if (gym_id or who == "me") else [scope]
    if gym_id:
        clauses.append(AuditLog.gym_id == gym_id)
    if area:
        clauses.append(area_col == area)
    if who == "me":
        # the owner's rows newest first straight from ix_audit_logs_actor_created. The scope is ONE opaque CASE: an OR / IN
        # on gym_id looks selective to the planner (it can't know that an owner's rows are almost all at their own gyms),
        # which then reads every row of theirs and sorts them
        clauses.append(AuditLog.actor_id == owner.id)
        clauses.append(case((AuditLog.gym_id.is_(None), ~AuditLog.action.like("trust.%")),
                            else_=AuditLog.gym_id.in_(gym_ids or [-1])))
    elif who == "others":
        clauses.append(or_(AuditLog.actor_id.is_(None), AuditLog.actor_id != owner.id))
    if q:  # the actor's name is matched on the joined actor of the owner's own rows (no scan of every user)
        like = _like(q)
        clauses.append(or_(AuditLog.action.ilike(like, escape="\\"), AuditLog.entity_type.ilike(like, escape="\\"),
                           User.full_name.ilike(like, escape="\\")))
    # one page (LIMIT/OFFSET) + a COUNT that stops after AUDIT_COUNT_CAP rows ("10,000+": filters narrow it down)
    counted = (select(AuditLog.id).outerjoin(User, TS.audit_actor_join()).where(*clauses)
               .limit(AUDIT_COUNT_CAP + 1).subquery())
    p = await paginate(
        db,
        select(AuditLog, User.full_name, User.deleted_at, User.role, Gym.name)
        .outerjoin(User, TS.audit_actor_join()).outerjoin(Gym, Gym.id == AuditLog.gym_id)
        .where(*clauses).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()),
        page=_page(page), per_page=AUDIT_PAGE_SIZE, count_stmt=select(func.count()).select_from(counted),
        count_cap=AUDIT_COUNT_CAP,
    )
    rows, page_n, pages = p.items, p.page, p.pages
    total = f"{p.total:,}+" if p.total_capped else p.total
    entries = []
    for log, name, deleted, role, gym_name in rows:
        area_key = log.action.split(".", 1)[0]
        link_tpl = ENTITY_LINKS.get(log.entity_type)
        platform = TS.is_platform_row(log)
        is_me = log.actor_id == owner.id and not platform
        actor = TS.PLATFORM_ACTOR if platform else "System"
        if name:
            actor = "You" if is_me else ("Deleted user" if deleted else name)
        entries.append({
            "log": log, "label": action_label(log.action), "area": area_key, "icon": AREA_ICONS.get(area_key, "activity"),
            "actor": actor, "actor_role": getattr(role, "value", role) if name else "system", "is_me": is_me,
            "entity": (f"{log.entity_type.replace('_', ' ')} #{log.entity_id}" if log.entity_id
                       else log.entity_type.replace("_", " ")),
            "link": link_tpl.format(id=log.entity_id) if link_tpl and log.entity_id else None,
            "gym": gym_name, "detail": _detail_pairs(log.detail), "ip": log.ip if is_me else None,
        })
    filters = {"gym": gym_id or "", "area": area, "who": who, "q": q}

    def url(**over: Any) -> str:
        params: dict[str, Any] = dict(filters, page=page_n)
        params.update(over)
        clean = {k: v for k, v in params.items() if v not in ("", None) and not (k == "page" and v == 1)}
        return "/owner/audit" + (("?" + urlencode(clean)) if clean else "")

    return render(request, "owner/audit.html", {
        "entries": entries, "gyms": gyms, "areas": areas, "filters": filters, "total": total, "page": page_n,
        "pages": pages, "prev_url": url(page=page_n - 1) if page_n > 1 else None,
        "next_url": url(page=page_n + 1) if page_n < pages else None,
        "who_tabs": [{"label": label, "url": url(who=key, page=1), "active": who == key}
                     for key, label in (("", "Everyone"), ("me", "My actions"), ("others", "Staff, members & system"))],
        "has_filters": bool(gym_id or area or who or q), "open_reports": await _open_reports(db, owner),
    })


__all__ = ["action_label", "membership_summary", "router"]
