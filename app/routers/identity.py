"""WP-H identity cards: self upload, live status, authorised image viewing and the owner review flow.

Routes
* ``GET/POST /account/identity``           status card (stepper + engines + checks), history, upload form
* ``POST /account/identity/{doc_id}/recheck`` re-run the automatic check of the latest NEEDS_REVIEW card
* ``GET /api/identity/status``              JSON for the live refresh while a card is being checked
* ``GET /identity/{doc_id}/{side}``         decrypted image (front|back) for authorised viewers only (else 404)
* ``GET /owner/identity/{doc_id}``          review page (owner who uploaded a customer's card, or super-admin)
* ``POST /owner/identity/{doc_id}/review``  approve / reject a NEEDS_REVIEW card
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import aliased, joinedload
from starlette.datastructures import UploadFile

from app.config import get_settings
from app.core import caps
from app.core.deps import DB, AuthUser, NotFound
from app.core.templating import redirect, render
from app.core.utils import is_safe_redirect, utcnow
from app.models import Employee, Gym, IdentityDocument, Notification, Subscription, User
from app.models.enums import IdDocType, IdStatus, NotificationCategory, Role, Severity
from app.services import id_verification as idv
from app.services import ocr
from app.services import storage
from app.services.audit import audit
from app.services.notify import notify

log = logging.getLogger("ironpulse.identity")
router = APIRouter()

HISTORY_LIMIT = 12
MAX_UPLOADS_PER_DAY = 5
STUCK_AFTER = timedelta(minutes=15)  # a PENDING/PROCESSING card older than this no longer blocks a new upload
MAX_RECHECKS = 3
MAX_ID = 2**63 - 1
DOC_LABELS = {
    "national_id": "National ID card", "passport": "Passport", "driving_license": "Driving licence",
    "aadhaar": "Aadhaar", "pan": "PAN card", "voter_id": "Voter ID", "residence_permit": "Residence permit",
    "student_id": "Student ID", "other": "Government ID",
}
STATUS_COPY = {
    "missing": ("Verify your identity", "A government ID card is mandatory for memberships, staff accounts and gym admins. "
                "Upload a clear photo - several OCR engines cross-check it in about a minute.", "id-card", "blue"),
    "pending": ("Queued for verification", "Your card is encrypted and waiting for the OCR engines. This page updates "
                "by itself.", "clock", "blue"),
    "processing": ("Reading your card", "Several OCR engines are reading your card at the same time and cross-checking "
                   "what they find.", "cpu", "blue"),
    "verified": ("Your identity is verified", "Memberships, staff features and gym publishing are unlocked. Only the "
                 "last digits of your ID number are ever shown.", "check-badge", "green"),
    "needs_review": ("A person will double-check your card", "The automatic checks couldn't confirm everything, so a "
                     "reviewer will look at it. We'll e-mail you as soon as it's done.", "eye", "amber"),
    "rejected": ("We couldn't verify this card", "Please upload a new, clearer photo of a valid government ID in your "
                 "own name.", "x-circle", "red"),
}


SHORT_FAIL = {
    "duplicate": "Number already used on another account", "expiry": "The card has expired",
    "checksum": "The ID number is not valid", "name": "Name doesn't match the account",
    "review": "Rejected by a reviewer", "legible": "The photo was unreadable",
}


# ====================================================================== presentation helpers
def doc_label(value: Any) -> str:
    return DOC_LABELS.get(str(getattr(value, "value", value) or ""), "Government ID")


def normalise_checks(raw: list | None) -> list[dict[str, Any]]:
    """Checks are stored as ``{key, check, status, critical, detail, passed}``; older rows only have ``passed``."""
    rows: list[dict[str, Any]] = []
    for c in raw or []:
        if not isinstance(c, dict):
            continue
        status = c.get("status")
        if status not in ("pass", "fail", "warn", "skip"):
            status = "pass" if c.get("passed") else "fail"
        rows.append({
            "key": c.get("key") or "",
            "check": str(c.get("check") or c.get("key") or "Check"),
            "status": status,
            "critical": bool(c.get("critical")),
            "detail": c.get("detail") or "",
        })
    return rows


def engine_rows(doc: IdentityDocument | None, catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows for ``idm.engines``: per-engine results of a processed card, or the live engine line-up."""
    if doc is not None and doc.engine_results and doc.status not in (IdStatus.PENDING, IdStatus.PROCESSING):
        rows = []
        for e in doc.engine_results:
            if not isinstance(e, dict):
                continue
            name = str(e.get("engine") or "")
            conf = e.get("confidence")
            rows.append({
                "name": name,
                "label": e.get("label") or ocr.engine_label(name),
                "sub": ocr.ENGINE_DESCRIPTIONS.get(name, ""),
                "state": "done",
                "ok": bool(e.get("ok")) and bool(e.get("chars")),
                "agrees": bool(e.get("agrees")),
                "number_masked": e.get("number_masked"),
                "name_found": bool(e.get("name_found")),
                "chars": int(e.get("chars") or 0),
                "ms": e.get("ms"),
                "confidence": int(round(float(conf) * 100)) if isinstance(conf, (int, float)) else None,
                "error": e.get("error"),
            })
        return rows
    waiting = doc is not None and doc.status in (IdStatus.PENDING, IdStatus.PROCESSING)
    rows = []
    for c in catalog:
        state = ("waiting" if waiting else "ready") if c["available"] else "unavailable"
        rows.append({"name": c["name"], "label": c["label"], "sub": c["sub"], "state": state, "ok": False,
                     "agrees": False, "number_masked": None, "name_found": False, "chars": 0, "ms": None,
                     "confidence": None, "error": c.get("reason")})
    return rows


def stepper(doc: IdentityDocument | None, status: str) -> list[dict[str, Any]]:
    """Uploaded -> OCR engines -> Checks -> Verified."""
    steps = [
        {"key": "uploaded", "label": "Uploaded", "icon": "upload", "state": "todo",
         "detail": "Add a clear photo of your card", "when": None},
        {"key": "ocr", "label": "OCR engines", "icon": "cpu", "state": "todo",
         "detail": "Several engines read it independently", "when": None},
        {"key": "checks", "label": "Checks", "icon": "shield", "state": "todo",
         "detail": "Number, name, birth date, expiry, duplicates", "when": None},
        {"key": "verified", "label": "Verified", "icon": "check-badge", "state": "todo",
         "detail": "Unlocks memberships and staff features", "when": None},
    ]
    if doc is None:
        if status == "verified":  # verified without a stored card (e.g. migrated account)
            for s in steps:
                s["state"] = "done"
        return steps
    up, eng, chk, ver = steps
    up.update(state="done", detail=f"{doc_label(doc.doc_type)} · {doc.country_code}", when=doc.created_at)
    results = [e for e in (doc.engine_results or []) if isinstance(e, dict)]
    read = sum(1 for e in results if e.get("ok") and e.get("chars"))
    agreeing = sum(1 for e in results if e.get("agrees"))
    checks = normalise_checks(doc.checks)
    failed = [c for c in checks if c["status"] == "fail"]
    warned = [c for c in checks if c["status"] == "warn"]
    st = doc.status
    if st == IdStatus.PENDING:
        eng.update(state="current", detail="Queued - starting in a moment")
    elif st == IdStatus.PROCESSING:
        eng.update(state="current", detail="Reading your card now")
        chk.update(detail="Up next")
    else:
        if results:
            eng.update(state="done" if agreeing >= 2 else "warn",
                       detail=f"{agreeing} of {read} engine{'s' if read != 1 else ''} agreed on the number" if read
                       else "No engine could read the photo")
        else:
            eng.update(state="done", detail="Checked by a person")
        if st == IdStatus.VERIFIED:
            chk.update(state="done", detail="All checks passed" if not (failed or warned) else "Confirmed by a reviewer")
            ver.update(state="done", detail="Identity confirmed", when=doc.verified_at or doc.reviewed_at)
        elif st == IdStatus.NEEDS_REVIEW:
            n = len(failed) + len(warned)
            chk.update(state="warn", detail=f"{n} check{'s' if n != 1 else ''} need a person" if n else "Needs a person")
            ver.update(state="current", detail="Waiting for a reviewer")
        elif st == IdStatus.REJECTED:
            key = next((c["key"] for c in failed if c["key"] in SHORT_FAIL), None)
            chk.update(state="fail", detail=SHORT_FAIL.get(key or "", "A critical check failed"))
            ver.update(state="fail", detail="Not verified - upload a new card")
    return steps


def doc_view(doc: IdentityDocument, uploader: User | None = None) -> dict[str, Any]:
    checks = normalise_checks(doc.checks)
    results = [e for e in (doc.engine_results or []) if isinstance(e, dict)]
    demo = list(doc.engines or []) == ["seed"]  # demo cards from scripts/seed_demo.py carry a plain note, not AI output
    return {
        "id": doc.id,
        "status": doc.status.value,
        "doc_type": doc.doc_type.value,
        "doc_label": doc_label(doc.doc_type),
        "country": doc.country_code,
        "number_masked": doc.id_number_masked,
        "name_on_id": doc.name_on_id,
        "dob_on_id": doc.dob_on_id,
        "expires_on": doc.expires_on,
        "created_at": doc.created_at,
        "verified_at": doc.verified_at,
        "reviewed_at": doc.reviewed_at,
        "consensus_pct": int(round(doc.consensus_score * 100)) if doc.consensus_score is not None else None,
        "checks": checks,
        "checks_passed": sum(1 for c in checks if c["status"] == "pass"),
        "checks_total": sum(1 for c in checks if c["status"] != "skip"),
        "engines_ran": len(results),
        "engines_read": sum(1 for e in results if e.get("ok") and e.get("chars")),
        "agreeing": sum(1 for e in results if e.get("agrees")),
        "reason": doc.rejection_reason,
        "ai_summary": None if demo else doc.ai_summary,
        "demo": demo,
        "has_back": bool(doc.back_key),
        "attempts": doc.attempts,
        "uploaded_by_other": doc.uploaded_by_id is not None and doc.user_id is not None and doc.uploaded_by_id != doc.user_id,
        "uploader_name": uploader.display_name if uploader is not None else None,
    }


async def _engine_catalog() -> list[dict[str, Any]]:
    """Engine line-up for the page. The first call probes Tesseract / Vision, so it runs in a worker thread."""
    try:
        return await asyncio.to_thread(ocr.engine_catalog)
    except Exception:  # noqa: BLE001 - the page must render even if detection breaks
        log.exception("OCR engine catalog failed")
        return []


async def _unread_count(db: Any, user_id: int) -> int:
    return int(await db.scalar(
        select(func.count(Notification.id)).where(Notification.user_id == user_id, Notification.read_at.is_(None))
    ) or 0)


def _own_documents_clause(user: User) -> Any:
    """The user's cards, including cards an owner uploaded for their staff record before the account was linked."""
    return or_(
        IdentityDocument.user_id == user.id,
        IdentityDocument.employee_id.in_(select(Employee.id).where(Employee.user_id == user.id)),
    )


async def _load_history(db: Any, user: User) -> list[tuple[IdentityDocument, User | None]]:
    uploader = aliased(User)
    rows = (await db.execute(
        select(IdentityDocument, uploader)
        .outerjoin(uploader, uploader.id == IdentityDocument.uploaded_by_id)
        .where(_own_documents_clause(user))
        .order_by(IdentityDocument.id.desc())
        .limit(HISTORY_LIMIT)
    )).all()
    return [(r[0], r[1]) for r in rows]


def _upload_block(docs: list[IdentityDocument]) -> str | None:
    """Why a new upload is not allowed right now (None = allowed)."""
    now = utcnow()
    for d in docs:
        if d.status in (IdStatus.PENDING, IdStatus.PROCESSING) and d.created_at and now - d.created_at < STUCK_AFTER:
            return "Your card is being checked right now - wait for the result (usually under a minute) before uploading another."
    recent = sum(1 for d in docs if d.created_at and now - d.created_at < timedelta(hours=24))
    if recent >= MAX_UPLOADS_PER_DAY:
        return f"You've uploaded {MAX_UPLOADS_PER_DAY} cards in the last 24 hours. Please try again tomorrow or contact your gym."
    return None


async def _identity_page(request: Request, db: Any, user: User, *, form: dict[str, str] | None = None,
                         errors: dict[str, str] | None = None, status_code: int = 200):
    pairs = await _load_history(db, user)
    docs = [d for d, _u in pairs]
    latest = docs[0] if docs else None
    status = user.id_status.value if user.id_status else "missing"
    catalog = await _engine_catalog()
    title, body, hero_icon, tone = STATUS_COPY.get(status, STATUS_COPY["missing"])
    block = _upload_block(docs)
    needs_action = status in ("missing", "rejected") or (latest is None)
    latest_view = doc_view(latest, pairs[0][1]) if latest is not None else None
    can_recheck = (latest is not None and latest.status == IdStatus.NEEDS_REVIEW and latest.user_id == user.id
                   and (latest.attempts or 0) < MAX_RECHECKS and not latest.reviewed_by_id)
    settings = get_settings()
    ctx = {
        "status": status,
        "hero": {"title": title, "body": body, "icon": hero_icon, "tone": tone},
        "latest": latest_view,
        "latest_doc": latest,
        "history": [doc_view(d, u) for d, u in pairs],
        "steps": stepper(latest, status),
        "engine_rows": engine_rows(latest, catalog),
        "check_rows": latest_view["checks"] if latest_view else [],
        "catalog": catalog,
        "available_count": sum(1 for c in catalog if c["available"]),
        "min_engines": settings.ocr_min_engines,
        "upload_block": block,
        "upload_primary": needs_action,
        "can_recheck": can_recheck,
        "live": latest is not None and latest.status in (IdStatus.PENDING, IdStatus.PROCESSING),
        "form": form or {"id_doc_type": (latest.doc_type.value if latest is not None else ""),
                         "id_country": (latest.country_code if latest is not None else (user.country_code or "IN"))},
        "errors": errors or {},
        "unread_count": await _unread_count(db, user.id),
        "optional": user.role == Role.SUPERADMIN,
        "ai_feature": True,
    }
    return render(request, "identity/index.html", ctx, status_code=status_code)


# ====================================================================== account pages
@router.get("/account/identity")
async def identity_page(request: Request, db: DB, user: AuthUser):
    return await _identity_page(request, db, user)


def _valid_id(value: int) -> bool:
    return 0 < value <= MAX_ID  # larger path integers would overflow BIGINT -> 404, never a 500


def _upload(form: Any, name: str) -> UploadFile | None:
    value = form.get(name)
    if isinstance(value, UploadFile) and (value.filename or "").strip():
        return value
    return None


@router.post("/account/identity")
async def identity_upload(request: Request, db: DB, user: AuthUser):
    form = await request.form()
    doc_type = str(form.get("id_doc_type") or "").strip().lower()[:40]
    country = str(form.get("id_country") or "").strip().upper()[:4]
    values = {"id_doc_type": doc_type, "id_country": country}
    errors: dict[str, str] = {}
    if doc_type not in {t.value for t in IdDocType}:
        errors["id_doc_type"] = "Choose the type of ID document."
    if len(country) != 2 or not country.isascii() or not country.isalpha():
        errors["id_country"] = "Enter the 2-letter code of the country that issued the ID (e.g. IN, US, GB)."
    front = _upload(form, "id_front")
    back = _upload(form, "id_back")
    if front is None:
        errors["id_front"] = "Upload a photo of the front of your ID card."
    # one upload of this person at a time: the checks below (one card being checked, MAX_UPLOADS_PER_DAY, the
    # account's total) see every upload committed before - parallel requests can't slip past them together
    await caps.lock(db, "id_uploads", user.id)
    pairs = await _load_history(db, user)
    block = _upload_block([d for d, _u in pairs])
    if block:
        errors["upload"] = block
    elif not errors:
        try:
            await caps.enforce(db, "id_documents_per_user", user.id,
                               select(IdentityDocument.id).where(IdentityDocument.user_id == user.id))
        except caps.CapReached as exc:
            errors["id_front"] = str(exc)  # shown next to the upload field
    if errors:
        return await _identity_page(request, db, user, form=values, errors=errors, status_code=400)
    try:
        doc = await idv.submit_identity_document(
            db, user=user, employee=None, doc_type=IdDocType(doc_type), country_code=country,
            front=front, back=back, uploaded_by=user,  # type: ignore[arg-type]
        )
    except storage.UploadError as exc:
        # raised while validating/encrypting the images - before anything was added to the session, so no
        # rollback (it would expire the signed-in user that the error page still needs)
        msg = str(exc)
        errors["id_back" if back is not None and "front" not in msg.lower() and "back" in msg.lower() else "id_front"] = msg
        return await _identity_page(request, db, user, form=values, errors=errors, status_code=400)
    # the person's staff records follow their account's ID status (the verifier updates them again when done)
    await db.execute(update(Employee).where(Employee.user_id == user.id).values(id_status=IdStatus.PENDING))
    await notify(
        db, user, "ID card received",
        f"We received your {doc_label(doc.doc_type).lower()} and our OCR engines are checking it now - this usually "
        "takes under a minute. We'll let you know the result.",
        category=NotificationCategory.VERIFICATION, severity=Severity.INFO, link="/account/identity",
    )
    await db.commit()
    return redirect("/account/identity", "ID card uploaded - our OCR engines are reading it now.", "success")


@router.post("/account/identity/{doc_id}/recheck")
async def identity_recheck(request: Request, db: DB, user: AuthUser, doc_id: int):
    if not _valid_id(doc_id):
        raise NotFound("ID card not found.")
    doc = await db.scalar(select(IdentityDocument).where(IdentityDocument.id == doc_id, IdentityDocument.user_id == user.id))
    if doc is None:
        raise NotFound("ID card not found.")
    latest_id = await db.scalar(select(func.max(IdentityDocument.id)).where(_own_documents_clause(user)))
    if doc.id != latest_id or doc.status != IdStatus.NEEDS_REVIEW or doc.reviewed_by_id:
        return redirect("/account/identity", "Only your latest card that is waiting for review can be checked again.", "warning")
    if (doc.attempts or 0) >= MAX_RECHECKS:
        return redirect("/account/identity", "This card was already checked several times - a reviewer will take it from here.", "warning")
    await idv.queue_recheck(db, doc)
    await db.commit()
    return redirect("/account/identity", "Running the automatic check again.", "info")


@router.get("/api/identity/status")
async def identity_status(db: DB, user: AuthUser):
    latest = await db.scalar(
        select(IdentityDocument).where(_own_documents_clause(user)).order_by(IdentityDocument.id.desc()).limit(1)
    )
    return JSONResponse({
        "id_status": user.id_status.value if user.id_status else "missing",
        "document_id": latest.id if latest is not None else None,
        "document_status": latest.status.value if latest is not None else None,
        "updated_at": latest.updated_at.isoformat() if latest is not None and latest.updated_at else None,
        "done": latest is None or latest.status not in (IdStatus.PENDING, IdStatus.PROCESSING),
    }, headers={"Cache-Control": "no-store"})


# ====================================================================== authorised image route
async def can_view_document(db: Any, viewer: User, doc: IdentityDocument) -> bool:
    """Subject, super-admins, the uploader, owners of gyms where the customer has/had a subscription,
    and owners of the gym that employs the person. Everyone else gets a 404."""
    if viewer.role == Role.SUPERADMIN:
        return True
    if doc.user_id is not None and doc.user_id == viewer.id:
        return True
    if doc.uploaded_by_id is not None and doc.uploaded_by_id == viewer.id:
        return True
    if doc.employee_id is not None:
        row = (await db.execute(
            select(Employee.user_id, Gym.owner_id).join(Gym, Gym.id == Employee.gym_id).where(Employee.id == doc.employee_id)
        )).first()
        if row is not None and (row.user_id == viewer.id or (viewer.role == Role.OWNER and row.owner_id == viewer.id)):
            return True
    if viewer.role == Role.OWNER and doc.user_id is not None:
        subscribed = (select(Subscription.id).join(Gym, Gym.id == Subscription.gym_id)
                      .where(Subscription.customer_id == doc.user_id, Gym.owner_id == viewer.id).exists())
        employed = (select(Employee.id).join(Gym, Gym.id == Employee.gym_id)
                    .where(Employee.user_id == doc.user_id, Gym.owner_id == viewer.id).exists())
        return bool(await db.scalar(select(or_(subscribed, employed))))
    return False


@router.get("/identity/{doc_id}/{side}")
async def identity_image(request: Request, db: DB, user: AuthUser, doc_id: int, side: str):
    if side not in ("front", "back") or not _valid_id(doc_id):
        raise NotFound()
    doc = await db.get(IdentityDocument, doc_id)
    if doc is None or not await can_view_document(db, user, doc):
        raise NotFound("ID card not found.")
    key = doc.front_key if side == "front" else doc.back_key
    if not key:
        raise NotFound("This side of the card was not uploaded.")
    try:
        data = await storage.load_private(key)
    except (storage.UploadError, OSError, ValueError):
        log.warning("identity image %s/%s could not be loaded", doc.id, side)
        raise NotFound("The image is no longer available.") from None
    subject_id = doc.user_id if doc.user_id is not None else await idv.subject_user_id(db, doc)
    if subject_id != user.id:
        await audit(db, user, "identity.image_viewed", "identity_document", doc.id,
                    detail={"side": side, "subject_user_id": doc.user_id, "employee_id": doc.employee_id},
                    request=request)
        await db.commit()
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "Content-Disposition": f'inline; filename="id-{doc.id}-{side}.jpg"',
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


# ====================================================================== owner / super-admin review
async def _reviewable(db: Any, user: User, doc_id: int) -> IdentityDocument:
    """Owner: a card they uploaded for a customer they registered. Super-admin: any card. Else 404."""
    if user.role not in (Role.OWNER, Role.SUPERADMIN) or not _valid_id(doc_id):
        raise NotFound("ID card not found.")
    doc = await db.scalar(
        select(IdentityDocument)
        .options(joinedload(IdentityDocument.user), joinedload(IdentityDocument.employee))
        .where(IdentityDocument.id == doc_id)
    )
    if doc is None:
        raise NotFound("ID card not found.")
    if user.role == Role.SUPERADMIN:
        return doc
    subject = doc.user
    if (doc.uploaded_by_id != user.id or subject is None or subject.role != Role.CUSTOMER
            or subject.deleted_at is not None):
        raise NotFound("ID card not found.")
    return doc


def _safe_next(raw: Any, fallback: str) -> str:
    value = str(raw or "").strip()
    return value if value and len(value) < 500 and is_safe_redirect(value) else fallback


async def _review_page(request: Request, db: Any, user: User, doc: IdentityDocument, *, errors: dict[str, str] | None = None,
                       form: dict[str, str] | None = None, status_code: int = 200):
    catalog = await _engine_catalog()
    subject = doc.user
    employee = doc.employee
    view = doc_view(doc)
    ctx = {
        "doc": view,
        "doc_obj": doc,
        "subject": subject,
        "employee": employee,
        "subject_name": subject.display_name if subject is not None else (employee.display_name if employee else "Unknown"),
        "steps": stepper(doc, doc.status.value),
        "engine_rows": engine_rows(doc, catalog),
        "check_rows": view["checks"],
        "can_review": doc.status == IdStatus.NEEDS_REVIEW,
        "is_admin": user.role == Role.SUPERADMIN,
        "errors": errors or {},
        "form": form or {},
        "next": _safe_next(request.query_params.get("next") or (form or {}).get("next"), f"/owner/identity/{doc.id}"),
        "ai_feature": True,
    }
    return render(request, "identity/review.html", ctx, status_code=status_code)


@router.get("/owner/identity/{doc_id}")
async def identity_review_page(request: Request, db: DB, user: AuthUser, doc_id: int):
    doc = await _reviewable(db, user, doc_id)
    return await _review_page(request, db, user, doc)


@router.post("/owner/identity/{doc_id}/review")
async def identity_review(request: Request, db: DB, user: AuthUser, doc_id: int):
    doc = await _reviewable(db, user, doc_id)
    form = await request.form()
    decision = str(form.get("decision") or "").strip().lower()
    note = " ".join(str(form.get("note") or "").split())[:500]
    seen = str(form.get("seen_original") or "") in ("1", "on", "true", "yes")
    nxt = _safe_next(form.get("next"), f"/owner/identity/{doc.id}")
    values = {"decision": decision, "note": note, "seen_original": "1" if seen else "", "next": nxt}
    errors: dict[str, str] = {}
    if doc.status != IdStatus.NEEDS_REVIEW:
        return redirect(nxt, "This ID card isn't waiting for a review any more.", "warning")
    if decision not in ("approve", "reject"):
        errors["decision"] = "Choose whether to approve or reject the card."
    if decision == "reject" and len(note) < 5:
        errors["note"] = "Tell the member why the card was rejected (at least 5 characters) - they'll see this note."
    if decision == "approve" and user.role == Role.OWNER and not seen:
        errors["seen_original"] = "Confirm that you checked the original card in person."
    if errors:
        return await _review_page(request, db, user, doc, errors=errors, form=values, status_code=400)
    try:
        await idv.review_identity_document(db, doc, reviewer=user, approve=decision == "approve", note=note)
    except ValueError as exc:  # raised before anything changes (duplicate number guard)
        return await _review_page(request, db, user, doc, errors={"decision": str(exc)}, form=values, status_code=400)
    await db.commit()
    who = doc.user.display_name if doc.user is not None else "the member"
    if decision == "approve":
        return redirect(nxt, f"ID card approved - {who} is now verified.", "success")
    return redirect(nxt, f"ID card rejected - {who} has been asked to upload a new one.", "info")
