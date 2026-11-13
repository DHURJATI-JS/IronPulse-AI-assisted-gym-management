"""Identity verification x trust & safety: the blocked-identity registry is checked by the automatic ID check and by a
manual approval; PAN cards with an impossible holder-type letter are explained (and never verified automatically);
the specimen PAN number ABCDE1234F is flagged as a sample card; the doc-type warning uses the right article."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models import AuditLog, IdentityDocument, SecurityEvent, User
from app.models.enums import IdDocType, IdStatus, Role, ThreatCategory
from app.security.crypto import pepper_hash
from app.services import id_verification as idv
from app.services import idparse
from app.services import trust_safety as TS
from tests.factories import make_customer, make_user
from tests.test_identity import (  # noqa: F401 - fixtures and helpers (no test functions are imported)
    _ai_offline_by_default,
    aadhaar_no,
    aadhaar_text,
    checks_by_key,
    fake_ocr,
    fresh,
    process,
    rahul,
    submit,
    verdict_for,
)

PAN_TEXT = "INCOME TAX DEPARTMENT\nPermanent Account Number\n{n}\nName: RAHUL MEHTA\nDate of Birth: 17/05/1995"


async def _register_id(db, number: str, *, user_id: int | None, reason: str = "banned") -> None:
    await TS.register_entries(db, [("id_number", pepper_hash(number))], reason=reason, user_id=user_id, actor=None)
    await db.commit()


# ====================================================================== registry x automatic check
async def test_an_id_number_of_a_banned_account_is_rejected_by_the_automatic_check(db, fake_ocr):
    banned = await make_customer(db)
    await _register_id(db, aadhaar_no(), user_id=banned.id)
    user = await rahul(db)
    fake_ocr(front={"tesseract": aadhaar_text(), "apple_vision": aadhaar_text()})
    doc = await submit(db, user)
    await process(doc.id)
    doc = await fresh(db, IdentityDocument, doc.id)
    assert doc.status == IdStatus.REJECTED
    assert doc.rejection_reason == TS.BLOCKED_MESSAGES["id_number"]  # neutral: never says whose or why
    assert checks_by_key_from(doc)["duplicate"]["status"] == "fail"
    assert (await fresh(db, User, user.id)).id_status == IdStatus.REJECTED
    row = await db.scalar(select(AuditLog).where(AuditLog.action == "identity.blocked_rejected"))
    assert row is not None and row.entity_id == doc.id and row.detail["subject_user_id"] == user.id
    # not a live duplicate: no fraud event is raised against the uploader
    assert await db.scalar(select(func.count()).select_from(SecurityEvent)
                           .where(SecurityEvent.category == ThreatCategory.FRAUD)) == 0


async def test_the_accounts_own_registry_entry_does_not_block_its_card(db, fake_ocr):
    """A restored account, or one whose ban was lifted, keeps its own ID (exclude_user_id)."""
    user = await rahul(db)
    await _register_id(db, aadhaar_no(), user_id=user.id, reason="self_deleted")
    fake_ocr(front={"tesseract": aadhaar_text(), "apple_vision": aadhaar_text()})
    doc = await submit(db, user)
    await process(doc.id)
    assert (await fresh(db, IdentityDocument, doc.id)).status == IdStatus.VERIFIED


async def test_a_manual_approval_of_a_registry_id_number_is_refused(db, fake_ocr):
    user = await rahul(db)
    fake_ocr(front={"tesseract": aadhaar_text(), "apple_vision": "?"})  # one engine only: a person must decide
    doc = await submit(db, user)
    await process(doc.id)
    doc = await fresh(db, IdentityDocument, doc.id)
    assert doc.status == IdStatus.NEEDS_REVIEW
    doc.id_number_hash = pepper_hash(aadhaar_no())
    await db.commit()
    banned = await make_customer(db)
    await _register_id(db, aadhaar_no(), user_id=banned.id)  # the account behind that number was banned meanwhile
    admin = await make_user(db, role=Role.SUPERADMIN)
    admin_id, doc_id = admin.id, doc.id
    doc = await fresh(db, IdentityDocument, doc_id)
    with pytest.raises(ValueError, match="can't be used for an IronPulse account"):
        await idv.review_identity_document(db, doc, reviewer=admin, approve=True, note="looks fine")
    await db.rollback()
    assert (await fresh(db, IdentityDocument, doc_id)).status == IdStatus.NEEDS_REVIEW
    # rejecting stays possible
    doc = await fresh(db, IdentityDocument, doc_id)
    admin = await db.get(User, admin_id)
    await idv.review_identity_document(db, doc, reviewer=admin, approve=False, note="blocked identity")
    await db.commit()
    assert (await fresh(db, IdentityDocument, doc_id)).status == IdStatus.REJECTED


def checks_by_key_from(doc: IdentityDocument) -> dict[str, dict]:
    return {c["key"]: c for c in doc.checks or []}


# ====================================================================== PAN wording + specimen card
def test_a_pan_with_an_impossible_holder_letter_is_explained_and_never_verified_automatically():
    v = verdict_for({"tesseract": PAN_TEXT.format(n="ABCQE1234F"), "apple_vision": PAN_TEXT.format(n="ABCQE1234F")},
                    doc_type="pan")
    row = checks_by_key(v)["doc_type"]
    assert row["detail"] == ("This does not look like a valid PAN number (the 4th letter must be a holder type such as P "
                             "for an individual).")
    assert "looks like a ID" not in row["detail"]
    assert row["critical"] is True and v.status == IdStatus.NEEDS_REVIEW  # name, DOB and engines agree - still a person
    assert v.reason == idv.PAN_HOLDER_HINT
    # a real PAN (holder type P) still verifies on its own
    v = verdict_for({"tesseract": PAN_TEXT.format(n="ABCPM1234F"), "apple_vision": PAN_TEXT.format(n="ABCPM1234F")},
                    doc_type="pan")
    assert v.status == IdStatus.VERIFIED and "doc_type" not in checks_by_key(v)


def test_the_doc_type_warning_uses_the_right_article():
    generic = "GOVERNMENT OF INDIA\nRahul Mehta\nDOB: 17/05/1995\nCard No: 98765432"
    v = verdict_for({"tesseract": generic, "apple_vision": generic}, doc_type="aadhaar")
    assert checks_by_key(v)["doc_type"]["detail"] == "You chose Aadhaar card, but the number looks like an ID number."
    v = verdict_for({"tesseract": aadhaar_text(), "apple_vision": aadhaar_text()}, doc_type="voter_id")
    assert checks_by_key(v)["doc_type"]["detail"] == "You chose voter ID, but the number looks like an Aadhaar."
    assert idparse.with_article("PAN") == "a PAN" and idparse.with_article("ID number") == "an ID number"


def test_the_specimen_pan_number_is_flagged_as_a_sample_card():
    for doc_type in ("pan", "other", "aadhaar"):
        v = verdict_for({"tesseract": PAN_TEXT.format(n="ABCDE1234F"), "apple_vision": PAN_TEXT.format(n="ABCDE1234F")},
                        doc_type=doc_type)
        sample = checks_by_key(v)["sample"]
        assert sample["status"] == "fail" and sample["critical"] is True and "ABCDE1234F" in sample["detail"], doc_type
        assert v.status == IdStatus.REJECTED and "sample PAN card" in v.reason, doc_type
    # one engine reading it is enough to flag the card
    v = verdict_for({"tesseract": PAN_TEXT.format(n="ABCDE1234F"), "apple_vision": "?"}, doc_type="pan")
    assert v.status == IdStatus.REJECTED and "sample PAN card" in v.reason
    assert idparse.specimen_kind("abcde 1234 f") == "PAN" and idparse.specimen_kind("ABCPM1234F") is None


async def test_a_specimen_card_is_rejected_as_a_sample_not_as_another_accounts_duplicate(db, fake_ocr):
    """An earlier test override verified the specimen number on another account: a new upload of the sample card is
    still told it is a sample card - never 'already verified on another account', never a fraud event."""
    other = await make_customer(db)
    db.add(IdentityDocument(user_id=other.id, doc_type=IdDocType.PAN, country_code="IN", front_key="x/front.png",
                            status=IdStatus.VERIFIED, id_number_hash=pepper_hash("ABCDE1234F")))
    await db.commit()
    user = await rahul(db)
    fake_ocr(front={"tesseract": PAN_TEXT.format(n="ABCDE1234F"), "apple_vision": PAN_TEXT.format(n="ABCDE1234F")})
    doc = await submit(db, user, doc_type="pan")
    await process(doc.id)
    doc = await fresh(db, IdentityDocument, doc.id)
    assert doc.status == IdStatus.REJECTED and "sample PAN card" in doc.rejection_reason
    assert doc.id_number_hash is None
    assert await db.scalar(select(func.count()).select_from(SecurityEvent)
                           .where(SecurityEvent.category == ThreatCategory.FRAUD)) == 0
