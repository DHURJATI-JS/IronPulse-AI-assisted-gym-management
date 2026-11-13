"""Physical-branch verification pipeline (WP-B) - background job ``verify_branch``.

QUEUED -> PROCESSING ->
  1. OCR every document (``app.services.ocr.ocr_document``, multi-engine; a failing engine is not fatal)
  2. deterministic checks: required documents, registration number format/checksum, the number / business name
     (token overlap >= 0.6) / city appear in the OCR text, expiry dates are in the future
  3. geo: geocode the typed address and compare with the owner's pin (<= 0.5 km pass, <= 2 km warning, else fail)
     + the country must match
  4. AI judgement (``app.ai.verification``) on a compact summary (+ first page of the registration document)
  5. decision: critical deterministic failure -> REJECTED (NEEDS_REVIEW when the only problem is "OCR unavailable");
     AI approve with confidence >= 0.75 and no failed check -> VERIFIED; AI unavailable -> AI_UNAVAILABLE and an
     automatic retry 15 minutes later (after MAX_AI_ATTEMPTS -> NEEDS_REVIEW for the super-admin queue);
     anything else -> NEEDS_REVIEW.
Every run writes a ``VerificationRun`` and notifies the owner (in-app + e-mail).

Also exported for other packages:
  ``queue_verification(db, branch, actor=, request=)``                (owner submit; caller commits)
  ``review_branch(db, branch, reviewer=, approve=, note=, request=)``  (super-admin manual review; caller commits)
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.ai.client import AIUnavailableError
from app.config import get_settings
from app.core.geo import haversine_km, normalize_city, valid_coords
from app.core.utils import minutes, to_local, utcnow
from app.models import Branch, BranchDocument, Gym, User, VerificationRun
from app.models.enums import NotificationCategory, Severity, VerificationStatus
from app.services import compliance, geo, storage
from app.services.audit import audit
from app.services.jobs import enqueue, job_handler
from app.services.notify import notify
from app.services.realtime import publish_after_commit

log = logging.getLogger("ironpulse.branch_verification")

VS = VerificationStatus
JOB_KIND = "verify_branch"
MAX_AI_ATTEMPTS = 6
RETRY_DELAY = timedelta(minutes=15)
STALE_PROCESSING = timedelta(minutes=10)
OCR_TEXT_LIMIT = 20_000
APPROVE_CONFIDENCE = 0.75
NAME_OVERLAP_MIN = 0.6
GEO_PASS_KM = 0.5
GEO_WARN_KM = 2.0

NAME_STOPWORDS = {
    "the", "and", "of", "pvt", "ltd", "private", "limited", "llp", "inc", "llc", "co", "company", "corp", "gym", "gyms",
    "fitness", "club", "centre", "center", "studio", "branch", "health", "studios",
}
CITY_ALIASES = {
    "bangalore": "bengaluru", "bombay": "mumbai", "madras": "chennai", "calcutta": "kolkata", "gurgaon": "gurugram",
    "new delhi": "delhi", "nyc": "new york", "new york city": "new york", "trivandrum": "thiruvananthapuram",
    "mysore": "mysuru", "poona": "pune",
}
MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
EXPIRY_KEYWORDS = re.compile(
    r"(valid\s*(?:up\s*to|upto|until|till|through|thru|to)|expir(?:y|es|ation|ing)(?:\s*date)?|date\s*of\s*expiry|"
    r"renewal\s*(?:due|date)|validity(?:\s*period)?)\s*(?:date)?\s*[:\-]?\s*",
    re.IGNORECASE,
)
DATE_DMY = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b")
DATE_YMD = re.compile(r"\b(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})\b")
DATE_D_MON_Y = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")
DATE_MON_D_Y = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")
OCR_CONFUSIONS = str.maketrans("OILSBZG", "0115826")


# =====================================================================================================
# pure helpers (unit-tested)
# =====================================================================================================
def _ascii_lower(text: str) -> str:
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


def registration_in_text(text: str, registration: str | None) -> bool:
    """Is the registration number in the OCR text? Ignores separators and common OCR confusions (O/0, I/1 ...)."""
    key = compliance.registration_key(registration)
    if len(key) < 4:
        return False
    hay = re.sub(r"[^A-Z0-9]", "", (text or "").upper())
    return key in hay or key.translate(OCR_CONFUSIONS) in hay.translate(OCR_CONFUSIONS)


def _name_tokens(name: str, *, strict: bool = True) -> list[str]:
    tokens = [t for t in re.findall(r"[a-z0-9]+", _ascii_lower(name)) if len(t) >= 2]
    if strict:
        distinctive = [t for t in tokens if t not in NAME_STOPWORDS]
        return distinctive or tokens
    return tokens


def name_overlap(text: str, names: list[str]) -> tuple[float, str]:
    """Best fraction of a name's distinctive words found in the text (fuzzy for long words) and that name."""
    words = set(re.findall(r"[a-z0-9]+", _ascii_lower(text)))
    long_words = [w for w in words if len(w) >= 5]
    best, best_name = 0.0, names[0] if names else ""
    for name in names:
        tokens = _name_tokens(name)
        if not tokens:
            continue
        hits = 0
        for t in tokens:
            if t in words or (len(t) >= 5 and difflib.get_close_matches(t, long_words, n=1, cutoff=0.85)):
                hits += 1
        score = hits / len(tokens)
        if score > best:
            best, best_name = score, name
    return best, best_name


def city_in_text(city: str | None, text: str) -> bool:
    canon = normalize_city(city)
    if not canon:
        return False
    variants = {canon} | {alias for alias, target in CITY_ALIASES.items() if target == canon}
    hay = " " + " ".join(re.findall(r"[a-z0-9]+", _ascii_lower(text))) + " "
    return any(f" {v} " in hay for v in variants)


def _mk_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d) if 1900 < y < 2200 else None
    except ValueError:
        return None


def _parse_date_at(snippet: str, month_first: bool) -> date | None:
    best: tuple[int, date] | None = None
    for m in DATE_YMD.finditer(snippet):
        d = _mk_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d and (best is None or m.start() < best[0]):
            best = (m.start(), d)
    for m in DATE_DMY.finditer(snippet):
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        day, mon = (b, a) if (month_first and a <= 12) or b > 12 else (a, b)
        d = _mk_date(y, mon, day)
        if d and (best is None or m.start() < best[0]):
            best = (m.start(), d)
    for m in DATE_D_MON_Y.finditer(snippet):
        mon = MONTHS.get(m.group(2)[:3].lower())
        d = _mk_date(int(m.group(3)), mon, int(m.group(1))) if mon else None
        if d and (best is None or m.start() < best[0]):
            best = (m.start(), d)
    for m in DATE_MON_D_Y.finditer(snippet):
        mon = MONTHS.get(m.group(1)[:3].lower())
        d = _mk_date(int(m.group(3)), mon, int(m.group(2))) if mon else None
        if d and (best is None or m.start() < best[0]):
            best = (m.start(), d)
    return best[1] if best else None


def expiry_dates(text: str, *, month_first: bool = False) -> list[date]:
    """Dates that follow an expiry keyword ("valid until", "expiry date", ...) within ~40 characters."""
    found: list[date] = []
    for m in EXPIRY_KEYWORDS.finditer(text or ""):
        d = _parse_date_at(text[m.end(): m.end() + 40], month_first)
        if d:
            found.append(d)
    return found


# =====================================================================================================
# queueing (owner submit) + manual review (super-admin)
# =====================================================================================================
async def queue_verification(db: AsyncSession, branch: Branch, *, actor: User, request: Any = None) -> None:
    """Mark the branch QUEUED and enqueue the job (deduplicated per branch). The caller commits."""
    branch.verification_status = VS.QUEUED
    branch.verification_summary = "Queued - OCR engines, rule checks and the AI reviewer will look at it shortly."
    await enqueue(db, JOB_KIND, {"branch_id": branch.id, "attempt": 1}, dedupe_key=f"{JOB_KIND}:{branch.id}", user_id=actor.id)
    await audit(db, actor, "branch.submit", "branch", branch.id, gym_id=branch.gym_id, request=request)
    await notify(
        db, actor, f"{branch.name} submitted for verification",
        f"We're checking {branch.name}'s documents with several OCR engines, the {compliance.requirements_for(branch.country_code).name} "
        "rules and our AI reviewer. You'll get the result here and by e-mail - usually within a minute.",
        category=NotificationCategory.VERIFICATION, severity=Severity.INFO, link=f"/owner/branches/{branch.id}#verification",
    )
    publish_after_commit(db, [actor.id], {"type": "branch.verification", "branch_id": branch.id, "status": VS.QUEUED.value})


async def review_branch(db: AsyncSession, branch: Branch, *, reviewer: User, approve: bool, note: str,
                        request: Any = None) -> VerificationRun:
    """Manual decision by the platform super-admin (NEEDS_REVIEW / AI_UNAVAILABLE / REJECTED queue)."""
    if branch.verification_status not in (VS.NEEDS_REVIEW, VS.AI_UNAVAILABLE, VS.REJECTED):
        raise ValueError("Only branches waiting for review can be decided manually.")
    note = (note or "").strip()
    if not 3 <= len(note) <= 2000:
        raise ValueError("Write a short note for the owner (3-2000 characters).")
    outcome = VS.VERIFIED if approve else VS.REJECTED
    branch.verification_status = outcome
    branch.verification_summary = f"Manual review: {note}"
    branch.verified_at = utcnow() if approve else None
    if approve:
        docs = (await db.execute(select(BranchDocument).where(BranchDocument.branch_id == branch.id))).scalars().all()
        for doc in docs:
            doc.accepted = True
    run = VerificationRun(branch_id=branch.id, outcome=outcome, checks=branch.verification_checks, summary=note,
                          manual=True, reviewer_id=reviewer.id, score=1.0 if approve else 0.0)
    db.add(run)
    owner_id = await db.scalar(select(Gym.owner_id).where(Gym.id == branch.gym_id))
    if owner_id:
        await _notify_outcome(db, owner_id, branch, outcome, [note], manual=True)
    await audit(db, reviewer, "branch.review_approve" if approve else "branch.review_reject", "branch", branch.id,
                gym_id=branch.gym_id, detail={"note": note[:500]}, request=request)
    return run


# =====================================================================================================
# the job
# =====================================================================================================
@job_handler(JOB_KIND, on_exit="keep")
async def verify_branch_job(db: AsyncSession, payload: dict[str, Any]) -> None:
    await run_verification(db, int(payload.get("branch_id") or 0), attempt=int(payload.get("attempt") or 1))


@dataclass(slots=True)
class _Doc:
    doc: BranchDocument
    text: str
    engines: list[str]
    error: str | None
    data: bytes | None


def _check(key: str, label: str, status: str, detail: str, *, critical: bool = False) -> dict[str, Any]:
    return {"key": key, "check": key, "label": label, "status": status, "passed": status == "pass",
            "critical": critical, "detail": detail[:400]}


async def _read_document(doc: BranchDocument, keep_bytes: bool) -> _Doc:
    try:
        data = await storage.load_private(doc.file_key)
    except Exception as exc:  # missing/corrupted file
        log.warning("branch document %s unreadable: %s", doc.id, exc.__class__.__name__)
        return _Doc(doc, "", [], "The file could not be opened", None)
    try:
        from app.services import ocr as ocr_service

        result = await ocr_service.ocr_document(data, doc.content_type)
        text = (result.combined_text or result.best_text or "").strip()
        engines = list(result.engines_used)
        errors = [f"{r.engine}: {r.error}" for r in result.results if not r.ok and r.error]
        error = None if text else ("; ".join(errors)[:200] or "No text recognised")
    except Exception as exc:  # NotImplementedError until the OCR package is deployed, engine crashes, ...
        log.warning("OCR failed for branch document %s: %s", doc.id, exc.__class__.__name__)
        text, engines, error = "", [], f"OCR unavailable ({exc.__class__.__name__})"
    return _Doc(doc, text, engines, error, data if keep_bytes else None)


async def run_verification(db: AsyncSession, branch_id: int, *, attempt: int = 1) -> VerificationStatus | None:
    """Execute the pipeline for one branch (the caller's session commits at the end)."""
    await db.execute(select(Branch.id).where(Branch.id == branch_id).with_for_update())
    branch = (await db.execute(
        select(Branch).where(Branch.id == branch_id).options(
            joinedload(Branch.gym).joinedload(Gym.owner), selectinload(Branch.documents), selectinload(Branch.hours))
    )).unique().scalar_one_or_none()
    if branch is None:
        log.info("verify_branch: branch %s no longer exists", branch_id)
        return None
    now = utcnow()
    stale = branch.verification_status == VS.PROCESSING and (branch.updated_at or now) < now - STALE_PROCESSING
    if branch.verification_status not in (VS.QUEUED, VS.AI_UNAVAILABLE) and not stale:
        log.info("verify_branch: branch %s is %s - nothing to do", branch_id, branch.verification_status.value)
        return None
    owner: User = branch.gym.owner
    owner_id = owner.id
    branch.verification_status = VS.PROCESSING
    branch.verification_summary = "Reading your documents with several OCR engines…"
    publish_after_commit(db, [owner.id], {"type": "branch.verification", "branch_id": branch.id, "status": VS.PROCESSING.value})
    await db.commit()
    try:
        return await _pipeline(db, branch, owner, attempt)
    except Exception:
        log.exception("verify_branch %s crashed - sending it to manual review", branch_id)
        await db.rollback()
        branch = await db.get(Branch, branch_id)
        if branch is None:
            return None
        summary = "The automatic check hit an unexpected error, so a person will review this branch instead."
        branch.verification_status = VS.NEEDS_REVIEW
        branch.verification_summary = summary
        db.add(VerificationRun(branch_id=branch.id, outcome=VS.NEEDS_REVIEW, summary=summary, checks=[]))
        await _notify_outcome(db, owner_id, branch, VS.NEEDS_REVIEW, [summary])
        publish_after_commit(db, [owner_id], {"type": "branch.verification", "branch_id": branch.id,
                                              "status": VS.NEEDS_REVIEW.value})
        return VS.NEEDS_REVIEW


async def _pipeline(db: AsyncSession, branch: Branch, owner: User, attempt: int) -> VerificationStatus:
    from app.ai import verification as ai_verification

    gym = branch.gym
    cc = (branch.country_code or "").upper()
    rules = compliance.requirements_for(cc)
    country_label = compliance.country_name(cc)
    today = date.today()
    checks: list[dict[str, Any]] = []
    docs = sorted(branch.documents, key=lambda d: d.id)
    reg_req = next((r for r in rules.requirements if r.key == "registration"), rules.requirements[0])
    reg_doc = next((d for d in docs if d.doc_type in reg_req.any_of), docs[0] if docs else None)

    # ---------------------------------------------------------------- 1. OCR
    read: list[_Doc] = []
    for doc in docs:
        item = await _read_document(doc, keep_bytes=doc is reg_doc)
        doc.ocr_text = item.text[:OCR_TEXT_LIMIT] or None
        doc.ocr_engines = item.engines or None
        read.append(item)
    readable = [r for r in read if r.text]
    all_text = "\n".join(r.text for r in readable)

    # ---------------------------------------------------------------- 2. deterministic checks
    missing = compliance.missing_requirements(cc, [d.doc_type for d in docs])
    checks.append(_check(
        "required_documents", "Required documents",
        "fail" if missing else "pass",
        ("Missing: " + "; ".join(r.label for r in missing)) if missing
        else f"All {len(rules.requirements)} {rules.name} requirements are covered ({len(docs)} files).",
        critical=True,
    ))
    reg_ok, reg_msg = compliance.validate_registration_number(cc, branch.registration_number)
    checks.append(_check("registration_format", f"{rules.registration_label} format", "pass" if reg_ok else "fail",
                         reg_msg, critical=True))
    if not valid_coords(branch.latitude, branch.longitude):
        checks.append(_check("map_pin", "Map pin", "fail", "The branch has no map pin.", critical=True))
    max_minutes = int(get_settings().max_open_hours_per_day * 60)
    long_days = [h for h in branch.hours if minutes(h.closes_at) - minutes(h.opens_at) > max_minutes]
    if not branch.hours:
        checks.append(_check("opening_hours", "Opening hours", "fail", "No opening hours were set."))
    elif long_days:
        checks.append(_check("opening_hours", "Opening hours", "fail",
                             f"Open longer than {get_settings().max_open_hours_per_day:g} h on {len(long_days)} day(s)."))
    else:
        checks.append(_check("opening_hours", "Opening hours", "pass",
                             f"Open {len(branch.hours)} day(s) a week within the {get_settings().max_open_hours_per_day:g}-hour limit."))

    if not docs:
        pass  # already a critical "required documents" failure
    elif not readable:
        reasons = sorted({r.error for r in read if r.error})
        checks.append(_check("ocr", "Document text (OCR)", "fail",
                             "OCR unavailable - no engine could read the documents" + (f" ({reasons[0]})" if reasons else "") + ".",
                             critical=True))
    else:
        engines = sorted({e for r in readable for e in r.engines})
        checks.append(_check(
            "ocr", "Document text (OCR)", "pass" if len(readable) == len(read) else "warn",
            f"Read {len(readable)} of {len(read)} documents" + (f" with {', '.join(engines)}." if engines else "."),
        ))
        found_in = [r for r in readable if registration_in_text(r.text, branch.registration_number)]
        checks.append(_check(
            "registration_in_documents", f"{rules.registration_label} on the documents",
            "pass" if found_in else "fail",
            f"Found in: {', '.join(compliance.doc_type_label(r.doc.doc_type) for r in found_in)}." if found_in
            else f"{branch.registration_number or 'The number'} does not appear in any document's text.",
        ))
        score, matched = name_overlap(all_text, [gym.name, branch.name, f"{gym.name} {branch.name}"])
        checks.append(_check(
            "name_match", "Business name on the documents", "pass" if score >= NAME_OVERLAP_MIN else "fail",
            f"{round(score * 100)}% of the words in “{matched}” appear in the documents (60% needed).",
        ))
        city_ok = city_in_text(branch.city, all_text)
        checks.append(_check("city_match", "City on the documents", "pass" if city_ok else "fail",
                             f"“{branch.city}” {'appears' if city_ok else 'does not appear'} in the documents."))

    # expiry: owner-entered dates + dates found next to expiry keywords
    expired: list[str] = []
    valid_until: list[date] = []
    doc_expired: set[int] = set()
    for r in read:
        dates = expiry_dates(r.text, month_first=cc == "US") if r.text else []
        if r.doc.expires_on is not None:
            dates.append(r.doc.expires_on.date() if isinstance(r.doc.expires_on, datetime) else r.doc.expires_on)
        for d in dates:
            if d < today:
                expired.append(f"{compliance.doc_type_label(r.doc.doc_type)} expired on {d:%d %b %Y}")
                doc_expired.add(r.doc.id)
            else:
                valid_until.append(d)
    if expired:
        checks.append(_check("expiry", "Documents are valid", "fail", "; ".join(sorted(set(expired)))[:400], critical=True))
    elif valid_until:
        checks.append(_check("expiry", "Documents are valid", "pass", f"Valid until at least {min(valid_until):%d %b %Y}."))
    else:
        checks.append(_check("expiry", "Documents are valid", "skip", "No expiry dates were found on the documents."))

    # ---------------------------------------------------------------- 3. geo
    if valid_coords(branch.latitude, branch.longitude):
        address = ", ".join(p for p in (branch.address_line1, branch.address_line2, branch.city, branch.state,
                                        branch.postal_code, country_label) if p)
        found = await geo.geocode(address)
        if found is None:
            checks.append(_check("geo_address", "Address found on the map", "fail",
                                 "The typed address could not be located by the geocoder, so the pin can't be confirmed."))
        else:
            branch.geocoded_latitude, branch.geocoded_longitude = found.lat, found.lon
            branch.formatted_address = (found.label or "")[:300] or None
            branch.geo_place_id = (found.place_id or "")[:300] or None
            branch.geo_confidence = found.confidence
            dist = haversine_km(float(branch.latitude), float(branch.longitude), found.lat, found.lon)  # type: ignore[arg-type]
            status = "pass" if dist <= GEO_PASS_KM else "warn" if dist <= GEO_WARN_KM else "fail"
            checks.append(_check(
                "geo_distance", "Pin matches the address", status,
                f"The pin is {dist:.2f} km from “{found.label[:120]}”"
                + (" - move the pin onto the building." if status != "pass" else "."),
                critical=status == "fail",
            ))
            if found.country_code and found.country_code.upper() != cc:
                checks.append(_check("geo_country", "Country matches", "fail",
                                     f"The address resolves to {compliance.country_name(found.country_code)}, not {country_label}.",
                                     critical=True))
            elif found.country_code:
                checks.append(_check("geo_country", "Country matches", "pass", f"The address is in {country_label}."))

    # ---------------------------------------------------------------- 4. + 5. AI judgement and decision
    critical = [c for c in checks if c["status"] == "fail" and c["critical"]]
    failing = [c for c in checks if c["status"] == "fail"]
    passes = sum(1 for c in checks if c["status"] == "pass")
    score: float | None = round(passes / max(1, passes + len(failing)), 2)
    provider: str | None = None
    reasons: list[str] = []
    if critical:
        if all(c["key"] == "ocr" for c in critical):
            outcome = VS.NEEDS_REVIEW
            summary = "Our OCR engines couldn't read the documents, so a person from the IronPulse team will review them."
            reasons = [critical[0]["detail"]]
        else:
            outcome = VS.REJECTED
            reasons = [c["detail"] for c in critical]
            summary = "Not verified: " + " ".join(r if r.endswith(".") else r + "." for r in reasons)
    else:
        ctx = ai_verification.VerificationInput(
            gym_name=gym.name, branch_name=branch.name,
            address=", ".join(p for p in (branch.address_line1, branch.address_line2, branch.state, branch.postal_code) if p),
            city=branch.city, country_code=cc, country_name=country_label,
            registration_label=rules.registration_label, registration_number=branch.registration_number or "",
            requirements=[f"{r.label} ({r.authority})" for r in rules.requirements],
            checks=checks,
            documents=[ai_verification.DocumentExcerpt(compliance.doc_type_label(r.doc.doc_type), r.doc.original_filename,
                                                       r.text, r.engines) for r in read],
        )
        reg_item = next((r for r in read if r.data is not None), None)
        image = None
        if reg_item is not None and reg_item.data:
            image = await asyncio.to_thread(ai_verification.first_page_image, reg_item.data, reg_item.doc.content_type)
        try:
            result = await ai_verification.judge_branch(ctx, image)
        except AIUnavailableError:
            result = None
        if result is None:
            if attempt < MAX_AI_ATTEMPTS:
                outcome = VS.AI_UNAVAILABLE
                retry_at = utcnow() + RETRY_DELAY
                await enqueue(db, JOB_KIND, {"branch_id": branch.id, "attempt": attempt + 1}, run_after=retry_at,
                              dedupe_key=f"{JOB_KIND}:{branch.id}:r{attempt + 1}")
                local = to_local(retry_at, owner.timezone)
                summary = (f"The automated checks passed, but every AI provider is offline right now. We'll retry "
                           f"automatically at {local:%H:%M} (attempt {attempt + 1} of {MAX_AI_ATTEMPTS}).")
            else:
                outcome = VS.NEEDS_REVIEW
                summary = "The AI reviewer stayed offline, so a person from the IronPulse team will review this branch."
            reasons = [summary]
        else:
            verdict = result.data
            provider = result.provider or None
            score = round(float(verdict.confidence), 2)
            reasons = list(verdict.reasons)
            if verdict.missing:
                reasons.append("Missing: " + "; ".join(verdict.missing))
            ai_note = (" AI reviewer: " + " ".join(r if r.endswith(".") else r + "." for r in reasons[:3])) if reasons else ""
            if verdict.decision == "approve" and verdict.confidence >= APPROVE_CONFIDENCE and not failing:
                outcome = VS.VERIFIED
                summary = f"Verified - every check passed and the AI reviewer approved ({round(score * 100)}% confidence)."
            else:
                outcome = VS.NEEDS_REVIEW
                why = []
                if failing:
                    why.append("some checks did not pass (" + ", ".join(c["label"].lower() for c in failing) + ")")
                if verdict.decision != "approve":
                    why.append(f"the AI reviewer suggested {verdict.decision.replace('_', ' ')}")
                elif verdict.confidence < APPROVE_CONFIDENCE:
                    why.append(f"the AI reviewer was only {round(score * 100)}% confident")
                summary = "Sent to manual review because " + " and ".join(why) + "." + ai_note
                reasons = [c["detail"] for c in failing] + reasons

    # ---------------------------------------------------------------- persist
    reg_key = compliance.registration_key(branch.registration_number)
    for r in read:
        notes: list[str] = []
        if not r.text:
            notes.append(r.error or "OCR could not read this file")
        elif reg_key and registration_in_text(r.text, branch.registration_number):
            notes.append(f"{rules.registration_label} found")
        if r.doc.id in doc_expired:
            notes.append("Expired")
        r.doc.assessment = {"notes": notes, "ocr_chars": len(r.text), "engines": r.engines, "checked_at": utcnow().isoformat()}
        if outcome == VS.VERIFIED:
            r.doc.accepted = True
        elif r.doc.id in doc_expired or not r.text and outcome == VS.REJECTED:
            r.doc.accepted = False
        else:
            r.doc.accepted = None
    branch.verification_status = outcome
    branch.verification_summary = summary[:2000]
    branch.verification_checks = checks
    branch.verified_at = utcnow() if outcome == VS.VERIFIED else None
    db.add(VerificationRun(branch_id=branch.id, outcome=outcome, score=score, checks=checks, summary=summary[:4000],
                           ai_provider=provider))
    if outcome != VS.AI_UNAVAILABLE or attempt == 1:
        await _notify_outcome(db, owner.id, branch, outcome, reasons)
    publish_after_commit(db, [owner.id], {"type": "branch.verification", "branch_id": branch.id, "status": outcome.value})
    log.info("branch %s verification -> %s (attempt %s, provider %s)", branch.id, outcome.value, attempt, provider)
    return outcome


async def _notify_outcome(db: AsyncSession, owner_id: int, branch: Branch, outcome: VerificationStatus,
                          reasons: list[str], *, manual: bool = False) -> None:
    link = f"/owner/branches/{branch.id}#verification"
    bullet = "\n".join(f"• {r}" for r in reasons[:6] if r)
    min_photos = get_settings().min_branch_photos
    if outcome == VS.VERIFIED:
        title = f"{branch.name} is verified"
        body = (f"Great news - {branch.name} passed {'manual review' if manual else 'AI verification'}. "
                f"Next step: upload at least {min_photos} photos (machines + the whole gym) to go live on the website.")
        severity, link = Severity.SUCCESS, f"/owner/branches/{branch.id}#photos"
    elif outcome == VS.REJECTED:
        title = f"{branch.name} could not be verified"
        body = f"We couldn't verify {branch.name}:\n{bullet}\n\nFix these points on the branch page and submit it again."
        severity = Severity.DANGER
    elif outcome == VS.AI_UNAVAILABLE:
        title = f"{branch.name}: AI verification delayed"
        body = (f"Your documents passed the automated checks, but the AI reviewer is offline. "
                f"We'll retry automatically every {int(RETRY_DELAY.total_seconds() // 60)} minutes - no action needed.")
        severity = Severity.WARNING
    else:
        title = f"{branch.name} is in manual review"
        body = (f"{branch.name} needs a quick look from the IronPulse team, usually within 1-2 business days."
                + (f"\n{bullet}" if bullet else ""))
        severity = Severity.WARNING
    await notify(db, owner_id, title, body, category=NotificationCategory.VERIFICATION, severity=severity, link=link)
