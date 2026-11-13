"""Identity-card verification (implemented by WP-H, identity/OCR).

ID cards are mandatory for customers, staff and gym admins. Pipeline (job ``verify_identity``):
multi-engine OCR -> field extraction per engine (name, DOB, ID number, expiry, MRZ) -> deterministic
validators (MRZ check digits, Aadhaar Verhoeff, PAN/other formats) -> cross-engine consensus ->
name/DOB match against the account -> duplicate-ID check (peppered hash) -> optional AI vision
judgement -> status VERIFIED / NEEDS_REVIEW / REJECTED (+ notification e-mail).

Decision rules
* VERIFIED     every critical check passes and >= ``OCR_MIN_ENGINES`` engines agree on the ID number
               (``vision_llm`` counts as an engine); the AI vision check may only downgrade to NEEDS_REVIEW.
* REJECTED     a clear mismatch: the number is already verified on another account (fraud event), a confirmed
               expiry date in the past, a number several engines read identically that fails its checksum, or a
               name on the card that clearly isn't the account holder's.
* NEEDS_REVIEW everything else (too few engines, disagreement, unreadable photo, DOB mismatch ...).

Only masked numbers + a peppered hash are stored - never the raw OCR text.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from fastapi import UploadFile
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models import Employee, IdentityDocument, SecurityEvent, User
from app.models.enums import (
    EmploymentStatus,
    IdDocType,
    IdStatus,
    NotificationCategory,
    Role,
    Severity,
    ThreatCategory,
)
from app.services import storage
from app.services.jobs import enqueue, enqueue_many, job_handler, periodic

log = logging.getLogger("ironpulse.identity")

MIN_ENGINE_CHARS = 12  # an engine "produced output" when it read at least a few words
MIN_LEGIBLE_CHARS = 25
NAME_MATCH = 0.75
NAME_MISMATCH = 0.5
PAN_HOLDER_HINT = ("This does not look like a valid PAN number (the 4th letter must be a holder type such as P for an "
                   "individual).")
REVIEW_BLOCKED = "This ID document can't be used for an IronPulse account, so the card can't be approved."
DOC_LABELS = {
    "national_id": "national ID card", "passport": "passport", "driving_license": "driving licence",
    "aadhaar": "Aadhaar card", "pan": "PAN card", "voter_id": "voter ID", "residence_permit": "residence permit",
    "student_id": "student ID", "other": "ID card",
}


def _has_file(value: UploadFile | bytes | None) -> bool:
    if value is None:
        return False
    if isinstance(value, bytes):
        return bool(value)
    return bool(getattr(value, "filename", None))


async def submit_identity_document(
    db: AsyncSession,
    *,
    user: User | None,
    employee: Employee | None,
    doc_type: IdDocType,
    country_code: str,
    front: UploadFile | bytes,
    back: UploadFile | bytes | None,
    uploaded_by: User | None,
) -> IdentityDocument:
    """Validate + encrypt the images, create IdentityDocument(status=PENDING), set the subject's
    ``id_status`` to PENDING and enqueue ``verify_identity``. The caller commits.

    Raises ``storage.UploadError`` (message safe to show) for invalid input."""
    if user is None and employee is None:
        raise ValueError("an identity document needs a user or an employee")
    cc = (country_code or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", cc):
        raise storage.UploadError("Enter the 2-letter code of the country that issued the ID (e.g. IN, US, GB).")
    if not _has_file(front):
        raise storage.UploadError("Please upload a photo of the front of your ID card.")
    stored_front, _ = await storage.save_private_file(front, kind="identity", allow_pdf=False)
    stored_back = None
    if _has_file(back):
        try:
            stored_back, _ = await storage.save_private_file(back, kind="identity", allow_pdf=False)  # type: ignore[arg-type]
        except storage.UploadError:
            await storage.delete_key(stored_front.key)
            raise
    size = stored_front.size + (stored_back.size if stored_back else 0)
    if user is not None:  # the member's storage quota (app/services/quotas.py): refused files are removed again
        from app.core import caps
        from app.services import quotas

        try:
            await quotas.reserve_member(db, user.id, size)
        except caps.CapReached as exc:
            for stored in (stored_front, stored_back):
                if stored is not None:
                    await storage.delete_key(stored.key)
            raise storage.UploadError(str(exc)) from None
    doc = IdentityDocument(
        user_id=user.id if user else None,
        employee_id=employee.id if employee else None,
        doc_type=IdDocType(doc_type),
        country_code=cc,
        front_key=stored_front.key,
        back_key=stored_back.key if stored_back else None,
        size_bytes=size,
        status=IdStatus.PENDING,
        uploaded_by_id=uploaded_by.id if uploaded_by else None,
    )
    db.add(doc)
    await db.flush()
    if user is not None:
        user.id_status = IdStatus.PENDING
    if employee is not None:
        employee.id_status = IdStatus.PENDING
    await enqueue(db, "verify_identity", {"document_id": doc.id}, dedupe_key=f"verify_identity:{doc.id}",
                  user_id=uploaded_by.id if uploaded_by is not None else None)
    return doc


def id_requirement_met(status: IdStatus | None) -> bool:
    return status == IdStatus.VERIFIED


# ============================================================================ pure decision logic
def mask_number(number: str | None) -> str | None:
    from app.core.utils import mask_middle

    return mask_middle(number, keep_end=4) if number else None


def check_row(key: str, label: str, status: str, detail: str, *, critical: bool = False) -> dict[str, Any]:
    """Stored check format: ``{key, check, status: pass|fail|warn|skip, critical, detail, passed}``."""
    return {"key": key, "check": label, "status": status, "critical": critical, "detail": detail,
            "passed": status in ("pass", "skip")}


def _title(name: str | None) -> str | None:
    if not name:
        return None
    return " ".join(w.capitalize() for w in name.split())[:160] or None


def _friendly_error(error: str | None) -> str | None:
    if not error:
        return None
    e = error.lower()
    if "timed out" in e or "timeout" in e:
        return "Took too long and was skipped."
    if "ai unavailable" in e:
        return "The AI providers are offline right now."
    if "not available" in e or "not installed" in e:
        return "Not available on this server."
    if "decode" in e or "could not be" in e:
        return "Couldn't open the photo."
    if "no text" in e:
        return "No text found on the photo."
    return "This engine hit an error and was skipped."


@dataclass(slots=True)
class Verdict:
    status: IdStatus
    checks: list[dict[str, Any]]
    engine_results: list[dict[str, Any]]
    consensus: Any  # idparse.Consensus
    number: str | None
    name_on_id: str | None
    dob_on_id: date | None
    expires_on: date | None
    reason: str | None
    duplicate: bool = False
    ai_concern: bool = False
    notes: list[str] = field(default_factory=list)


def decide(
    *,
    per_engine: dict[str, Any],
    results: dict[str, Any],
    consensus: Any,
    account_name: str | None,
    account_dob: date | None,
    doc_type: str,
    min_engines: int,
    today: date,
    duplicate: bool = False,
    ai_assessment: Any = None,
    ai_unavailable: bool = False,
    texts: dict[str, str] | None = None,
    blocked: str | None = None,
) -> Verdict:
    """Pure decision function (no I/O) - every rule of the verifier lives here so it can be unit-tested.

    per_engine: engine -> idparse.Extraction; results: engine -> OcrEngineResult (combined front+back).
    blocked: the trust-and-safety registry's neutral refusal when the number belonged to a deleted or banned account."""
    from app.ai.idcard import raises_concern
    from app.services import idparse
    from app.services.ocr import engine_label

    texts = texts or {}
    min_engines = max(1, int(min_engines))
    cons = consensus
    with_output = [e for e, x in per_engine.items() if x.chars >= MIN_ENGINE_CHARS]
    best_chars = max((x.chars for x in per_engine.values()), default=0)
    checks: list[dict[str, Any]] = []
    reject: list[str] = []
    review: list[str] = []

    # 1 ---- legible text
    if best_chars >= MIN_LEGIBLE_CHARS:
        checks.append(check_row("legible", "Legible text", "pass", f"The engines read up to {best_chars} characters.", critical=True))
    else:
        checks.append(check_row("legible", "Legible text", "fail",
                                "Too little text could be read - retake the photo flat, in good light and in focus.", critical=True))
        review.append("The photo was hard to read, so a person will check it - a sharper photo usually verifies instantly.")

    # 2 ---- independent engines with output
    names = ", ".join(engine_label(e) for e in with_output) or "none"
    if len(with_output) >= min_engines:
        checks.append(check_row("engines", "Independent OCR engines", "pass", f"{len(with_output)} engines read the card ({names}).", critical=True))
    else:
        checks.append(check_row("engines", "Independent OCR engines", "warn",
                                f"Only {len(with_output)} engine{'s' if len(with_output) != 1 else ''} could read the card "
                                f"({names}); {min_engines} are needed for automatic approval.", critical=True))
        review.append(f"Only {len(with_output)} OCR engine{'s' if len(with_output) != 1 else ''} could read your card, so a person will confirm it.")

    # 3 ---- ID number + checksum + agreement
    kind_label = idparse.KIND_LABELS.get(cons.kind or "", "ID number")
    if cons.accepted:
        checks.append(check_row("number", "ID number found", "pass", f"{kind_label} {mask_number(cons.number)}", critical=True))
    elif cons.best_guess:
        detail = ("The engines read different numbers." if cons.outcome == "disagreement" else
                  f"Only one engine found a number ({mask_number(cons.best_guess)}).")
        if cons.outcome == "checksum_invalid":
            detail = f"Several engines read {mask_number(cons.best_guess)}, but it is not a valid {kind_label}."
        checks.append(check_row("number", "ID number found", "warn", detail, critical=True))
        if cons.outcome != "checksum_invalid":
            review.append("The OCR engines couldn't agree on your ID number.")
    else:
        checks.append(check_row("number", "ID number found", "fail", "No ID number could be read from the card.", critical=True))
        review.append("No ID number could be read from the card.")

    if cons.kind in idparse.CHECKSUM_KINDS and cons.best_guess:
        aadhaar = cons.kind == "aadhaar"
        what = "Verhoeff checksum" if aadhaar else "MRZ check digits"
        if cons.checksum == "valid":
            checks.append(check_row("checksum", "Checksum / check digits", "pass",
                                    f"The {what} {'is' if aadhaar else 'are'} valid.", critical=True))
        else:
            checks.append(check_row("checksum", "Checksum / check digits", "fail",
                                    f"The {what} {'does' if aadhaar else 'do'} not match.", critical=True))
            if cons.outcome == "checksum_invalid":
                reject.append(f"The number on the card fails its {what}, so it can't be a genuine {kind_label}.")
    elif cons.best_guess:
        checks.append(check_row("checksum", "Checksum / check digits", "skip",
                                f"{kind_label}s carry no public checksum - the format was verified instead."))
    else:
        checks.append(check_row("checksum", "Checksum / check digits", "skip", "No number to validate."))

    agreeing = list(cons.agreeing) if cons.accepted else []
    if cons.accepted and len(agreeing) >= min_engines:
        checks.append(check_row("consensus", "Engines agree", "pass",
                                f"{len(agreeing)} of {len(with_output) or len(agreeing)} engines read the same number "
                                f"({', '.join(engine_label(e) for e in agreeing)}).", critical=True))
    else:
        detail = (f"Only {len(agreeing)} engine{'s' if len(agreeing) != 1 else ''} read this number; {min_engines} must agree."
                  if cons.accepted else "No number was read the same way by enough engines.")
        checks.append(check_row("consensus", "Engines agree", "warn", detail, critical=True))
        if cons.accepted:
            review.append(f"Only {len(agreeing)} OCR engine{'s' if len(agreeing) != 1 else ''} read your ID number, so a person will confirm it.")

    read_number = cons.number or cons.best_guess
    if doc_type == "pan" and idparse.pan_shaped(read_number) and not idparse.pan_valid(read_number):
        # the outline of a PAN with an impossible holder-type letter: not a valid PAN, so never verified automatically
        checks.append(check_row("doc_type", "Document type", "warn", PAN_HOLDER_HINT, critical=True))
        review.append(PAN_HOLDER_HINT)
    elif cons.accepted and cons.kind and idparse.kind_preference(doc_type, cons.kind) == 0:
        checks.append(check_row("doc_type", "Document type", "warn",
                                f"You chose {DOC_LABELS.get(doc_type, 'ID card')}, but the number looks like "
                                f"{idparse.with_article(kind_label)}."))

    # 3b ---- specimen (sample) cards: their printed example number is never a real person's
    sample = next((n for n in [cons.number, cons.best_guess,
                               *(c.number for x in per_engine.values() for c in x.candidates[:3])]
                   if idparse.specimen_kind(n)), None)
    if sample:
        kind = idparse.specimen_kind(sample)
        checks.append(check_row("sample", "Not a sample card", "fail",
                                f"{sample} is the example number printed on specimen {kind} cards.", critical=True))
        reject.append(f"This looks like a sample {kind} card: {sample} is the example number printed on specimen cards. "
                      "Please upload a photo of your own card.")

    # 4 ---- name on the card vs the account
    extracted = [(e, x.name, x.name_source) for e, x in per_engine.items() if x.name]
    best_sim = max((idparse.name_similarity(account_name, n) for _e, n, _s in extracted), default=0.0)
    coverage = max((idparse.name_in_text(account_name, t) for t in texts.values()), default=0.0)
    strong = any(src in ("mrz", "label") for _e, _n, src in extracted)
    if not account_name:
        checks.append(check_row("name", "Name matches your account", "skip", "The account has no name to compare."))
    elif best_sim >= NAME_MATCH or coverage >= 0.9:
        checks.append(check_row("name", "Name matches your account", "pass",
                                f"Name similarity {int(round(max(best_sim, coverage) * 100))}%.", critical=True))
    elif extracted and strong and best_sim < NAME_MISMATCH and coverage < NAME_MISMATCH:
        checks.append(check_row("name", "Name matches your account", "fail",
                                f"The name on the card doesn't match the account (similarity {int(round(best_sim * 100))}%).", critical=True))
        reject.append(f"The name on the card doesn't match the name on the account ({account_name}).")
    else:
        detail = (f"Partial match only (similarity {int(round(max(best_sim, coverage) * 100))}%)." if (extracted or coverage)
                  else "The name couldn't be read from the card.")
        checks.append(check_row("name", "Name matches your account", "warn", detail, critical=True))
        review.append("We couldn't confirm the name on your card.")

    # 5 ---- date of birth
    dobs = [x.dob for x in per_engine.values() if x.dob]
    yobs = [x.yob for x in per_engine.values() if x.yob and not x.dob]
    dob_on_id = Counter(dobs).most_common(1)[0][0] if dobs else None
    if account_dob is None or (not dobs and not yobs):
        checks.append(check_row("dob", "Date of birth matches", "skip",
                                "No date of birth on the account." if account_dob is None else "No date of birth printed or read."))
    elif account_dob in dobs:
        checks.append(check_row("dob", "Date of birth matches", "pass", "The date of birth on the card matches the account.", critical=True))
    elif not dobs and account_dob.year in yobs:
        checks.append(check_row("dob", "Date of birth matches", "pass", "The year of birth on the card matches the account.", critical=True))
    else:
        checks.append(check_row("dob", "Date of birth matches", "fail", "The date of birth on the card differs from the account.", critical=True))
        review.append("The date of birth on your card differs from the one on your account.")

    # 6 ---- expiry (only a confirmed date can reject: MRZ check digit, or >= 2 engines reading the same date)
    exp_votes = Counter(x.expiry for x in per_engine.values() if x.expiry)
    mrz_exp = next((x.mrz.expiry for x in per_engine.values()
                    if x.mrz is not None and x.mrz.expiry and x.mrz.checks.get("expiry")), None)
    mrz_sure = any(x.mrz is not None and x.mrz.expiry == mrz_exp and x.mrz.checks.get("expiry") and x.mrz.checks.get("composite")
                   for x in per_engine.values()) if mrz_exp else False
    expires_on = mrz_exp or (exp_votes.most_common(1)[0][0] if exp_votes else None)
    confirmed = mrz_sure or (expires_on is not None and exp_votes.get(expires_on, 0) >= 2)
    if expires_on is None:
        checks.append(check_row("expiry", "Not expired", "skip", "No expiry date printed (normal for Aadhaar and PAN)."))
    elif expires_on >= today:
        checks.append(check_row("expiry", "Not expired", "pass", f"Valid until {expires_on:%d %b %Y}.", critical=True))
    elif confirmed:
        checks.append(check_row("expiry", "Not expired", "fail", f"Expired on {expires_on:%d %b %Y}.", critical=True))
        reject.append(f"The ID card expired on {expires_on:%d %b %Y}. Please upload a valid card.")
    else:
        checks.append(check_row("expiry", "Not expired", "warn", f"One engine read an expiry date of {expires_on:%d %b %Y}.", critical=True))
        review.append("One engine read an expiry date in the past.")

    # 7 ---- duplicate (same number already verified on another account) / blocked (a deleted or banned account's ID)
    if blocked:
        checks.append(check_row("duplicate", "Not used by another account", "fail", blocked, critical=True))
        reject.insert(0, blocked)
    elif duplicate:
        checks.append(check_row("duplicate", "Not used by another account", "fail",
                                "This ID number is already verified on a different account.", critical=True))
        reject.insert(0, "This ID card is already verified on another account. If you think this is a mistake, contact the gym or support.")
    elif cons.accepted:
        checks.append(check_row("duplicate", "Not used by another account", "pass", "No other account uses this ID number.", critical=True))
    else:
        checks.append(check_row("duplicate", "Not used by another account", "skip", "Checked once the number is confirmed."))

    # 8 ---- optional AI vision check (evidence only: can require a human review, never approve or reject)
    ai_concern = False
    if ai_assessment is not None:
        ai_concern = raises_concern(ai_assessment)
        if ai_concern:
            signs = "; ".join(ai_assessment.tampering_signs[:2]) or "it may not be a genuine government ID"
            checks.append(check_row("ai", "AI document check", "warn", f"The AI flagged: {signs}."))
            review.append("The AI document check flagged something for a person to look at.")
        else:
            guess = ai_assessment.document_type_guess or "a government ID"
            checks.append(check_row("ai", "AI document check", "pass",
                                    f"Recognised as {guess} ({int(round(ai_assessment.confidence * 100))}% confidence), "
                                    "no signs of tampering. Supporting evidence only."))
    else:
        checks.append(check_row("ai", "AI document check", "skip",
                                "AI unavailable - the deterministic checks decided on their own." if ai_unavailable else "Not run."))

    # ---- verdict
    critical_ok = all(c["status"] in ("pass", "skip") for c in checks if c["critical"])
    if reject:
        status, reason = IdStatus.REJECTED, reject[0]
    elif critical_ok and cons.accepted and len(agreeing) >= min_engines and not ai_concern:
        status, reason = IdStatus.VERIFIED, None
    else:
        status = IdStatus.NEEDS_REVIEW
        reason = review[0] if review else "A person will double-check your card."

    # ---- per-engine summary (masked; never the raw text)
    engine_results: list[dict[str, Any]] = []
    for name, res in results.items():
        x = per_engine.get(name)
        number = None
        if x is not None and x.candidates:
            target = cons.number or cons.best_guess
            close = [c.number for c in x.candidates if target and idparse.numbers_close(target, c.number)]
            number = close[0] if close else x.candidates[0].number
        cover = idparse.name_in_text(account_name, texts.get(name, "")) if account_name else 0.0
        engine_results.append({
            "engine": name,
            "label": engine_label(name),
            "ok": bool(res.ok and x is not None and x.chars > 0),
            "chars": x.chars if x is not None else 0,
            "number_masked": mask_number(number),
            "name_found": bool(x is not None and (x.name or cover >= NAME_MATCH)),
            "dob": x.dob.isoformat() if x is not None and x.dob else None,
            "confidence": res.confidence,
            "ms": res.ms,
            "error": _friendly_error(res.error) if not (res.ok and x is not None and x.chars) else None,
            "agrees": name in agreeing,
        })

    # ---- name / dob as printed (MRZ beats labels beats heuristics)
    name_on_id = None
    for source in ("mrz", "label", "heuristic"):
        cands = [n for _e, n, s in extracted if s == source]
        if cands:
            votes = Counter(cands)
            name_on_id = max(votes, key=lambda n: (votes[n], idparse.name_similarity(account_name, n) if account_name else 0.0,
                                                   -len(n), n))
            break
    return Verdict(status, checks, engine_results, cons, cons.number, _title(name_on_id), dob_on_id, expires_on,
                   reason, duplicate=duplicate, ai_concern=ai_concern, notes=review)


# ============================================================================ database helpers
async def subject_user_id(db: AsyncSession, doc: IdentityDocument) -> int | None:
    if doc.user_id is not None:
        return doc.user_id
    if doc.employee_id is not None:
        return await db.scalar(select(Employee.user_id).where(Employee.id == doc.employee_id))
    return None


async def find_duplicate(db: AsyncSession, doc: IdentityDocument, number_hash: str) -> IdentityDocument | None:
    """A VERIFIED card with the same number hash that belongs to a different person."""
    subject = await subject_user_id(db, doc)
    rows = (await db.execute(
        select(IdentityDocument, Employee.user_id)
        .outerjoin(Employee, Employee.id == IdentityDocument.employee_id)
        .where(IdentityDocument.id_number_hash == number_hash, IdentityDocument.status == IdStatus.VERIFIED,
               IdentityDocument.id != doc.id)
        .order_by(IdentityDocument.id)
    )).all()
    for other, emp_user_id in rows:
        other_subject = other.user_id or emp_user_id
        if subject is not None and other_subject == subject:
            continue  # the same person re-uploading their card
        if subject is None and other_subject is None and other.employee_id == doc.employee_id:
            continue
        return other
    return None


async def registry_refusal(db: AsyncSession, doc: IdentityDocument, number_hash: str) -> str | None:
    """The blocked-identity registry's neutral refusal when this ID number belonged to a deleted or banned account
    (app/services/trust_safety.py) - entries registered for the card's own account don't count (a restored account, or
    one whose ban was lifted, keeps its own ID)."""
    from app.services.trust_safety import registration_blocked

    return await registration_blocked(db, id_number_hash=number_hash, exclude_user_id=await subject_user_id(db, doc))


async def _is_latest(db: AsyncSession, doc: IdentityDocument) -> bool:
    if doc.user_id is not None:
        latest = await db.scalar(select(func.max(IdentityDocument.id)).where(IdentityDocument.user_id == doc.user_id))
    else:
        latest = await db.scalar(select(func.max(IdentityDocument.id)).where(IdentityDocument.employee_id == doc.employee_id))
    return latest is None or latest == doc.id


async def _apply_subject_status(db: AsyncSession, doc: IdentityDocument, status: IdStatus) -> bool:
    """Mirror the latest card's status onto the user and every employment of that person."""
    if not await _is_latest(db, doc):
        return False
    if doc.user_id is not None:
        subject = await db.get(User, doc.user_id)
        if subject is not None:
            subject.id_status = status
        await db.execute(update(Employee).where(Employee.user_id == doc.user_id).values(id_status=status))
    if doc.employee_id is not None:
        await db.execute(update(Employee).where(Employee.id == doc.employee_id).values(id_status=status))
    return True


async def _queue_staff_checks(db: AsyncSession, doc: IdentityDocument) -> int:
    """WP-C1 activates staff once the person's ID is verified: queue ``staff_activation_check`` per employment."""
    ids: set[int] = set()
    user_id = await subject_user_id(db, doc)
    if user_id is not None:
        ids |= set((await db.execute(
            select(Employee.id).where(Employee.user_id == user_id, Employee.deleted_at.is_(None),
                                      Employee.status != EmploymentStatus.TERMINATED)
        )).scalars())
    if doc.employee_id is not None:
        ids.add(doc.employee_id)
    # every employment in one INSERT + one recycling UPDATE (system work, like enqueue), never one enqueue per row
    await enqueue_many(db, "staff_activation_check",
                       [({"employee_id": emp_id}, f"staff_activation_check:{emp_id}") for emp_id in sorted(ids)])
    return len(ids)


def _record_fraud(db: AsyncSession, doc: IdentityDocument, other: IdentityDocument, subject_id: int | None) -> None:
    db.add(SecurityEvent(
        ip="0.0.0.0", user_id=subject_id, method="JOB", path="/jobs/verify_identity", user_agent=None,
        category=ThreatCategory.FRAUD, severity=3, action="logged",
        detail=(f"Duplicate ID number: identity document {doc.id} matches verified document {other.id} "
                f"(user {other.user_id or '-'}, employee {other.employee_id or '-'})")[:500],
    ))


OUTCOME_COPY = {
    IdStatus.VERIFIED: ("Your ID card is verified", Severity.SUCCESS),
    IdStatus.NEEDS_REVIEW: ("Your ID card needs a quick manual check", Severity.WARNING),
    IdStatus.REJECTED: ("We couldn't verify your ID card", Severity.DANGER),
}


def _outcome_messages(doc: IdentityDocument, subject_user: User | None, uploader: User | None,
                      staff_name: str | None, reviewer: User | None = None) -> list[tuple[User, str, str, Severity, str]]:
    """(recipient, title, body, severity, link) of the notices a card's outcome sends - the card's holder and the
    person who uploaded it for them (an owner at the desk, or for a staff record). Pure: the callers load the people."""
    status = doc.status
    if status not in OUTCOME_COPY:
        return []
    title, severity = OUTCOME_COPY[status]
    label = DOC_LABELS.get(doc.doc_type.value, "ID card")
    last4 = (doc.id_number_masked or "")[-4:]
    if status == IdStatus.VERIFIED:
        body = (f"Your {label}{f' ending {last4}' if last4 else ''} was verified"
                f"{' by a reviewer' if reviewer else ''}. Memberships and staff features that need a verified ID are now unlocked.")
    elif status == IdStatus.NEEDS_REVIEW:
        body = (f"{doc.rejection_reason or 'Some checks need a person.'} A reviewer will look at your {label} soon and "
                "we'll e-mail you the result. You can also upload a clearer photo at any time.")
    else:
        body = f"{doc.rejection_reason or 'The card could not be verified.'} Please upload a new photo of a valid government ID."
    out: list[tuple[User, str, str, Severity, str]] = []
    if subject_user is not None and subject_user.deleted_at is None:
        out.append((subject_user, title, body, severity, "/account/identity"))
    # the owner who uploaded the card at the desk (or for a staff record) hears about it too
    uploader_id = doc.uploaded_by_id
    if uploader_id is None or uploader_id == doc.user_id or (reviewer is not None and reviewer.id == uploader_id):
        return out
    if uploader is None or uploader.deleted_at is not None:
        return out
    who = subject_user.display_name if subject_user is not None else (staff_name or "your staff member")
    word = {IdStatus.VERIFIED: "verified", IdStatus.NEEDS_REVIEW: "needs your review", IdStatus.REJECTED: "was rejected"}[status]
    link = "/account/identity"
    if uploader.role == Role.OWNER:
        if doc.user_id is not None and subject_user is not None and subject_user.role == Role.CUSTOMER:
            link = f"/owner/identity/{doc.id}"
        elif doc.employee_id is not None:
            link = f"/owner/staff/{doc.employee_id}"
    detail = (" You registered this member at your desk - open the card, compare it with the original and approve or "
              "reject it." if status == IdStatus.NEEDS_REVIEW and link.startswith("/owner/identity") else
              f" {doc.rejection_reason}" if doc.rejection_reason else "")
    out.append((uploader, f"ID check for {who}: {word}", f"The {label} you uploaded for {who} {word}.{detail}", severity,
                link))
    return out


async def _notify_outcome(db: AsyncSession, doc: IdentityDocument, *, reviewer: User | None = None) -> None:
    from app.services.notify import Note, notify_bulk

    if doc.status not in OUTCOME_COPY:
        return
    subject_user = await db.get(User, doc.user_id) if doc.user_id is not None else None
    uploader_id = doc.uploaded_by_id
    wanted = uploader_id is not None and uploader_id != doc.user_id and not (reviewer is not None and reviewer.id == uploader_id)
    uploader = await db.get(User, uploader_id) if wanted else None
    staff_name = None
    if uploader is not None and subject_user is None and doc.employee_id is not None:
        emp = await db.get(Employee, doc.employee_id)
        staff_name = emp.display_name if emp is not None else None
    notes = [Note(user, title, body, link=link, category=NotificationCategory.VERIFICATION, severity=severity)
             for user, title, body, severity, link in _outcome_messages(doc, subject_user, uploader, staff_name, reviewer)]
    if notes:  # the holder and the uploader: one bulk send
        await notify_bulk(db, notes)


async def _notify_outcomes(db: AsyncSession, docs: Sequence[IdentityDocument]) -> int:
    """``_notify_outcome`` for many cards at once (the sweep): the people in one query, the staff names in one, the
    notices in one ``notify_bulk``. Returns how many notices were created."""
    from app.services.notify import Note, notify_bulk

    docs = [d for d in docs if d.status in OUTCOME_COPY]
    ids = {d.user_id for d in docs if d.user_id is not None} | {d.uploaded_by_id for d in docs if d.uploaded_by_id}
    people = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(ids)))).scalars()} if ids else {}
    emp_ids = {d.employee_id for d in docs if d.user_id is None and d.employee_id is not None}
    names = ({e.id: e.display_name for e in (await db.execute(select(Employee).where(Employee.id.in_(emp_ids)))).scalars()}
             if emp_ids else {})
    notes = [Note(user, title, body, link=link, category=NotificationCategory.VERIFICATION, severity=severity)
             for d in docs
             for user, title, body, severity, link in _outcome_messages(
                 d, people.get(d.user_id) if d.user_id is not None else None,
                 people.get(d.uploaded_by_id) if d.uploaded_by_id is not None else None,
                 names.get(d.employee_id) if d.employee_id is not None else None)]
    return len(await notify_bulk(db, notes)) if notes else 0


async def _apply_subject_statuses(db: AsyncSession, docs: Sequence[IdentityDocument], status: IdStatus) -> int:
    """``_apply_subject_status`` for many cards at once: the latest card of each person in one query each for accounts
    and staff records, then one UPDATE of the accounts and one of the employments. Returns how many cards were latest."""
    user_ids = {d.user_id for d in docs if d.user_id is not None}
    emp_only = {d.employee_id for d in docs if d.user_id is None and d.employee_id is not None}
    latest_user = dict((await db.execute(
        select(IdentityDocument.user_id, func.max(IdentityDocument.id))
        .where(IdentityDocument.user_id.in_(user_ids)).group_by(IdentityDocument.user_id)
    )).all()) if user_ids else {}
    latest_emp = dict((await db.execute(
        select(IdentityDocument.employee_id, func.max(IdentityDocument.id))
        .where(IdentityDocument.employee_id.in_(emp_only)).group_by(IdentityDocument.employee_id)
    )).all()) if emp_only else {}
    latest = [d for d in docs if (latest_user.get(d.user_id, d.id) if d.user_id is not None
                                  else latest_emp.get(d.employee_id, d.id)) == d.id]
    users = sorted({d.user_id for d in latest if d.user_id is not None})
    emps = sorted({d.employee_id for d in latest if d.employee_id is not None})
    if users:
        await db.execute(update(User).where(User.id.in_(users)).values(id_status=status)
                         .execution_options(synchronize_session=False))
    if users or emps:
        await db.execute(update(Employee).where(or_(Employee.user_id.in_(users or [-1]), Employee.id.in_(emps or [-1])))
                         .values(id_status=status).execution_options(synchronize_session=False))
    return len(latest)


# ============================================================================ job + entry points
async def _ai_assess(front: bytes, back: bytes | None, doc: IdentityDocument) -> tuple[Any, bool]:
    """-> (assessment or None, ai_unavailable)."""
    from app.ai.client import AIUnavailableError, ai
    from app.ai.idcard import assess_id_card

    try:
        if not ai.status().get("available"):
            return None, True
        assessment, _provider = await asyncio.wait_for(
            assess_id_card(front, back, doc_type=doc.doc_type.value, country=doc.country_code), timeout=60)
        return assessment, False
    except (AIUnavailableError, TimeoutError):
        return None, True
    except Exception:  # noqa: BLE001 - the AI is optional evidence
        log.exception("AI identity check failed for document %s", doc.id)
        return None, True


def _combine(front: Any, back: Any) -> dict[str, Any]:
    """Per engine: front + back text joined (the OCR results of both sides ran concurrently)."""
    from app.services.ocr import OcrEngineResult

    combined: dict[str, Any] = {}
    for r in front.results:
        combined[r.engine] = r
    if back is not None:
        for r in back.results:
            f = combined.get(r.engine)
            if f is None:
                combined[r.engine] = r
                continue
            texts = [t for t in (f.text if f.ok else "", r.text if r.ok else "") if t]
            confs = [c for c in (f.confidence if f.ok else None, r.confidence if r.ok else None) if c is not None]
            combined[r.engine] = OcrEngineResult(
                engine=r.engine, ok=f.ok or r.ok, text="\n".join(texts),
                confidence=round(sum(confs) / len(confs), 4) if confs else None,
                ms=max(f.ms, r.ms), error=None if (f.ok or r.ok) else (f.error or r.error),
            )
    return combined


async def _none() -> None:
    return None


async def process_identity_document(db: AsyncSession, document_id: int, *, force: bool = False) -> IdentityDocument | None:
    """Run the full verification pipeline for one card (job ``verify_identity``). The caller's session commits."""
    from app.config import get_settings
    from app.core.utils import local_today, utcnow
    from app.security.crypto import pepper_hash
    from app.services import idparse, ocr

    doc = await db.scalar(
        select(IdentityDocument)
        .options(joinedload(IdentityDocument.user), joinedload(IdentityDocument.employee))
        .where(IdentityDocument.id == document_id)
    )
    if doc is None:
        log.warning("verify_identity: document %s no longer exists", document_id)
        return None
    if not force and (doc.status in (IdStatus.VERIFIED, IdStatus.REJECTED) or doc.reviewed_by_id is not None):
        return doc  # already decided (job re-run) - idempotent
    settings = get_settings()
    doc.status = IdStatus.PROCESSING
    doc.attempts = (doc.attempts or 0) + 1
    await _apply_subject_status(db, doc, IdStatus.PROCESSING)
    await db.commit()  # the status page shows "Reading your card" while the engines run

    subject = doc.user
    employee = doc.employee
    account_name = subject.full_name if subject is not None else (employee.full_name if employee is not None else None)
    account_dob = subject.date_of_birth if subject is not None else None
    today = local_today(subject.timezone if subject is not None else "UTC")
    try:
        try:
            front = await storage.load_private(doc.front_key)
            back = await storage.load_private(doc.back_key) if doc.back_key else None
        except Exception as exc:  # noqa: BLE001
            log.warning("verify_identity: images of document %s unreadable: %s", doc.id, exc.__class__.__name__)
            doc.status = IdStatus.NEEDS_REVIEW
            doc.checks = [check_row("image", "Card image", "fail", "The stored image could not be opened.", critical=True)]
            doc.rejection_reason = "We couldn't open the uploaded image - please upload the card again."
            await _apply_subject_status(db, doc, doc.status)
            await _notify_outcome(db, doc)
            return doc

        front_res, back_res, (assessment, ai_unavailable) = await asyncio.gather(
            ocr.run_ocr(front, purpose="id_card"),
            ocr.run_ocr(back, purpose="id_card") if back else _none(),
            _ai_assess(front, back, doc),
        )
        results = _combine(front_res, back_res)
        texts = {name: r.text for name, r in results.items() if r.ok and r.text.strip()}
        per_engine = {
            name: idparse.extract_fields(text, doc_type=doc.doc_type.value, country=doc.country_code, today=today)
            for name, text in texts.items()
        }
        cons = idparse.build_consensus(per_engine, doc_type=doc.doc_type.value, min_chars=MIN_ENGINE_CHARS)
        duplicate_of = None
        blocked = None
        # a specimen card's example number identifies nobody: no hash, no duplicate / fraud match (decide() rejects it)
        number_hash = (pepper_hash(cons.number) if cons.accepted and cons.number and not idparse.specimen_kind(cons.number)
                       else None)
        if number_hash:
            duplicate_of = await find_duplicate(db, doc, number_hash)
            blocked = await registry_refusal(db, doc, number_hash)
        verdict = decide(
            per_engine=per_engine, results=results, consensus=cons, account_name=account_name, account_dob=account_dob,
            doc_type=doc.doc_type.value, min_engines=settings.ocr_min_engines, today=today,
            duplicate=duplicate_of is not None, ai_assessment=assessment, ai_unavailable=ai_unavailable, texts=texts,
            blocked=blocked,
        )
    except Exception:  # noqa: BLE001 - never leave a card stuck in PROCESSING
        log.exception("verify_identity: pipeline failed for document %s", doc.id)
        doc.status = IdStatus.NEEDS_REVIEW
        doc.checks = [check_row("pipeline", "Automatic check", "fail", "The automatic check hit an error.", critical=True)]
        doc.rejection_reason = "The automatic check hit an error, so a person will review your card."
        await _apply_subject_status(db, doc, doc.status)
        await _notify_outcome(db, doc)
        return doc

    from app.ai.idcard import summarize

    doc.status = verdict.status
    doc.engines = list(results)
    doc.engine_results = verdict.engine_results
    doc.checks = verdict.checks
    doc.consensus_score = cons.score
    doc.ai_summary = summarize(assessment) if assessment is not None else None
    doc.id_number_masked = mask_number(cons.number or cons.best_guess)
    doc.id_number_hash = number_hash
    doc.name_on_id = verdict.name_on_id
    doc.dob_on_id = verdict.dob_on_id
    doc.expires_on = verdict.expires_on
    doc.rejection_reason = verdict.reason
    doc.verified_at = utcnow() if verdict.status == IdStatus.VERIFIED else None
    await _apply_subject_status(db, doc, verdict.status)
    if duplicate_of is not None:
        subject_id = await subject_user_id(db, doc)
        _record_fraud(db, doc, duplicate_of, subject_id)
        from app.services.audit import audit

        await audit(db, None, "identity.duplicate_rejected", "identity_document", doc.id,
                    detail={"matches_document": duplicate_of.id, "subject_user_id": subject_id})
    if blocked is not None:  # the number belonged to a deleted or banned account: one person per ID, forever
        from app.services.audit import audit

        await audit(db, None, "identity.blocked_rejected", "identity_document", doc.id,
                    detail={"subject_user_id": await subject_user_id(db, doc), "employee_id": doc.employee_id})
    await _notify_outcome(db, doc)
    await _queue_staff_checks(db, doc)
    log.info("identity document %s -> %s (engines=%s agreeing=%s score=%.2f)", doc.id, doc.status.value,
             ",".join(results), ",".join(e["engine"] for e in verdict.engine_results if e["agrees"]), cons.score)
    return doc


@job_handler("verify_identity", on_exit="keep")
async def verify_identity_job(db: AsyncSession, payload: dict[str, Any]) -> None:
    try:
        document_id = int(payload.get("document_id") or 0)
    except (TypeError, ValueError):
        log.warning("verify_identity: bad payload %r", payload)
        return
    if document_id > 0:
        await process_identity_document(db, document_id)


STUCK_MINUTES = 10
MAX_AUTO_ATTEMPTS = 5


@periodic("identity_sweep", every_seconds=300, initial_delay=60)
async def identity_sweep(db: AsyncSession) -> int:
    """Safety net: re-queue cards stuck in PENDING/PROCESSING whose job is missing or finished (queued before the
    verifier was deployed, a crashed worker ...). After ``MAX_AUTO_ATTEMPTS`` runs a card goes to a human instead."""
    from datetime import timedelta

    from app.core.utils import utcnow
    from app.models import Job
    from app.models.enums import JobStatus

    cutoff = utcnow() - timedelta(minutes=STUCK_MINUTES)
    rows = (await db.execute(
        select(IdentityDocument)
        .outerjoin(Job, Job.dedupe_key == func.concat("verify_identity:", IdentityDocument.id))
        .where(IdentityDocument.status.in_([IdStatus.PENDING, IdStatus.PROCESSING]),
               IdentityDocument.updated_at < cutoff,
               (Job.id.is_(None)) | (Job.status.in_([JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED])))
        .order_by(IdentityDocument.id)
        .limit(50)
    )).scalars().all()
    give_up = [doc for doc in rows if (doc.attempts or 0) >= MAX_AUTO_ATTEMPTS]
    again = [doc for doc in rows if (doc.attempts or 0) < MAX_AUTO_ATTEMPTS]
    if give_up:  # a constant number of statements however many cards: one UPDATE, the people, one bulk notify
        from sqlalchemy.orm.attributes import set_committed_value

        reason = "The automatic check couldn't finish, so a person will review your card."
        await db.execute(
            update(IdentityDocument).where(IdentityDocument.id.in_([d.id for d in give_up]))
            .values(status=IdStatus.NEEDS_REVIEW, rejection_reason=reason)
            .execution_options(synchronize_session=False))
        for doc in give_up:  # the loaded rows show the new state without a second (per-row) UPDATE at the flush
            set_committed_value(doc, "status", IdStatus.NEEDS_REVIEW)
            set_committed_value(doc, "rejection_reason", reason)
        await _apply_subject_statuses(db, give_up, IdStatus.NEEDS_REVIEW)
        await _notify_outcomes(db, give_up)
    await enqueue_many(db, "verify_identity", [({"document_id": d.id}, f"verify_identity:{d.id}") for d in again])
    if rows:
        log.info("identity_sweep: re-queued %s stuck card(s), %s sent to review", len(again), len(give_up))
    return len(again)


async def queue_recheck(db: AsyncSession, doc: IdentityDocument) -> None:
    """Run the automatic check again (e.g. the AI or an OCR engine was offline the first time). Caller commits."""
    doc.status = IdStatus.PENDING
    doc.rejection_reason = None
    await _apply_subject_status(db, doc, IdStatus.PENDING)
    await enqueue(db, "verify_identity", {"document_id": doc.id}, dedupe_key=f"verify_identity:{doc.id}",
                  user_id=doc.user_id)


async def review_identity_document(
    db: AsyncSession, document: IdentityDocument, *, reviewer: User, approve: bool, note: str
) -> None:
    """Manual decision on a card (permission checks are done by callers). Caller commits.

    Raises ValueError when approving a number that is already verified on another account."""
    from app.core.utils import utcnow
    from app.services.audit import audit

    note = " ".join((note or "").split())[:500]
    if approve and document.id_number_hash:
        other = await find_duplicate(db, document, document.id_number_hash)
        if other is not None:
            raise ValueError("This ID number is already verified on another account, so the card can't be approved.")
        if await registry_refusal(db, document, document.id_number_hash):  # a deleted or banned account's ID number
            raise ValueError(REVIEW_BLOCKED)
    now = utcnow()
    status = IdStatus.VERIFIED if approve else IdStatus.REJECTED
    document.status = status
    document.reviewed_by_id = reviewer.id
    document.reviewed_at = now
    document.verified_at = now if approve else None
    document.rejection_reason = None if approve else (note or "Rejected by a reviewer.")
    who = "a platform admin" if reviewer.role == Role.SUPERADMIN else "the gym admin"
    detail = f"{'Approved' if approve else 'Rejected'} by {who}" + (f": {note}" if note else ".")
    document.checks = [c for c in (document.checks or []) if not (isinstance(c, dict) and c.get("key") == "review")] + [
        check_row("review", "Manual review", "pass" if approve else "fail", detail, critical=not approve)
    ]
    await _apply_subject_status(db, document, status)
    await audit(db, reviewer, "identity.approved" if approve else "identity.rejected", "identity_document", document.id,
                detail={"subject_user_id": document.user_id, "employee_id": document.employee_id, "note": note[:200]})
    await _notify_outcome(db, document, reviewer=reviewer)
    await _queue_staff_checks(db, document)
