"""Employment lifecycle + trust & safety: ending employments with a reason, re-hire eligibility, auto-bans, IP bans for
severe violations and the blocked-identity registry ("one person per ID and e-mail, forever").

Rules (the gym owner's, README "Staff removal and bans")
* Ending an employment needs a reason from ``REMOVAL_REASONS`` - one fixed list in three categories:
  - BENIGN (position change, contract ended, resigned, role no longer needed / restructuring, relocation): the person
    may be added again later, by any gym.
  - VIOLATION (behavioural issue, policy violation, repeated no-shows, unprofessional conduct with members): this
    owner's gyms can never add the person again (``rehire_refusal``) and an immutable ``staff_violations`` row is kept;
    the person's other pending records at this owner's gyms (invited, or linked and in verification) are withdrawn with
    it, and a record is never activated while ``rehire_refusal`` refuses (app/services/staffing.py).
    Violations reported by VIOLATION_AUTOBAN_THRESHOLD different gym owners (any gyms) within VIOLATION_WINDOW_DAYS ban
    the account - counted per owner, so one owner can't ban someone by ending their jobs at two of their own gyms, and
    only for employments the person had accepted with their account (a never-accepted invitation proves nothing about
    who uses an address, so it never bans, never registers an identity - the super-admin is alerted instead).
  - SEVERE (harassment, violence or threats, theft or fraud, sexual misconduct, endangering members' safety): the
    account is banned at once, its recent public network addresses are IP-banned for SEVERE_IP_BAN_DAYS and the
    super-admin is notified.
  VIOLATION / SEVERE need a short note (NOTE_MIN..NOTE_MAX characters). Reasons and notes are shown to the owner of the
  gym that wrote them and to the super-admin (/superadmin/trust) - never to another gym, whose refusal is the neutral
  ``CANT_ADD`` sentence.
* A position (role / job title) is never edited in place: "Change position" ends the employment with the BENIGN reason
  'Position change' and re-adds the person in the new role through the normal invitation and verification.
* An account ban (``ban_account``): sign-in refused with the neutral "disabled" message (``users.is_active`` false),
  every session revoked and its sockets closed (``revoke_all_sessions``), queued jobs and AI work cancelled, the
  person's other current employments suspended (their owners are told only that the account was disabled), the
  identity registered, the super-admin notified. Lifted only by the super-admin, with a note; violations before the
  lift no longer count toward the threshold.
* IP bans (``ban_recent_ips``, SEVERE only): the account's distinct addresses from its sessions / sign-ins of the last
  IP_LOOKBACK_DAYS (at most MAX_BANNED_IPS, newest first) are banned through the ordinary ``ip_bans`` table - except
  (skipped and flagged on /superadmin/trust) an address more than SHARED_IP_THRESHOLD other active accounts used
  recently (gym Wi-Fi, carrier NAT), any address of the admin performing the removal or of a platform super-admin
  (they lift bans), and any address that is not a public internet address (loopback, private / LAN, link-local,
  carrier-grade NAT ...) or is whitelisted / a trusted proxy.
* Blocked identities: when an account is deleted (by its holder or an admin) or banned, keyed hashes of its e-mail
  (normalised: case, spaces, '+tag', Gmail dots), phone (E.164) and ID numbers are kept forever in
  ``blocked_identities`` (app/models/trust.py explains why only hashes). ``registration_blocked`` answers every
  sign-up / invitation / e-mail or phone change / ID check with a neutral refusal; ``identifier_conflict``
  (app/services/accounts.py) already asks it, so every path that checks identifiers refuses them.

Concurrency: every decision about a person runs under per-person advisory locks (``lock_person``: the account, each
e-mail hash and ID hash, in key order) taken BEFORE the employment row's lock (``lock_employee``), so two gyms removing
the same person at the same moment are counted one after the other (the threshold is never missed) and a double submit
finds the row already ended.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import and_, false, func, or_, select, update
from sqlalchemy.dialects.postgresql import array as pg_array
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.utils import local_today, normalize_phone, utcnow
from app.models import (
    AccountBan,
    AuditLog,
    AuthToken,
    BlockedIdentity,
    Employee,
    Gym,
    IdentityDocument,
    IpBan,
    StaffViolation,
    Subscription,
    TrainerClient,
    TrainerShift,
    TrustIpAction,
    User,
    UserSession,
)
from app.models.enums import EmploymentStatus, IdStatus, NotificationCategory, Role, Severity, TokenPurpose
from app.services.audit import audit
from app.services.notify import Note, email_address, notify, notify_bulk, notify_many

log = logging.getLogger("ironpulse.trust")

# ====================================================================== reasons
BENIGN, VIOLATION, SEVERE = "benign", "violation", "severe"
CATEGORY_LABELS = {BENIGN: "Can rejoin later", VIOLATION: "Conduct violation", SEVERE: "Severe violation"}
CATEGORY_TONES = {BENIGN: "zinc", VIOLATION: "amber", SEVERE: "red"}
CATEGORY_HELP = {
    BENIGN: "They can be added again later, by any gym.",
    VIOLATION: "Your gyms can never add them again. Repeated violations across gyms ban their IronPulse account.",
    SEVERE: "Their IronPulse account is banned at once, their recent network addresses are blocked and the platform "
            "team is alerted.",
}


@dataclass(frozen=True, slots=True)
class RemovalReason:
    code: str
    label: str
    category: str


REMOVAL_REASONS: dict[str, RemovalReason] = {r.code: r for r in (
    RemovalReason("position_change", "Position change", BENIGN),
    RemovalReason("contract_ended", "Contract ended", BENIGN),
    RemovalReason("resigned", "Resigned", BENIGN),
    RemovalReason("restructuring", "Role no longer needed / restructuring", BENIGN),
    RemovalReason("relocation", "Relocation", BENIGN),
    RemovalReason("behavioural_issue", "Behavioural issue", VIOLATION),
    RemovalReason("policy_violation", "Policy violation", VIOLATION),
    RemovalReason("repeated_no_shows", "Repeated no-shows", VIOLATION),
    RemovalReason("unprofessional_conduct", "Unprofessional conduct with members", VIOLATION),
    RemovalReason("harassment", "Harassment", SEVERE),
    RemovalReason("violence_threats", "Violence or threats", SEVERE),
    RemovalReason("theft_fraud", "Theft or fraud", SEVERE),
    RemovalReason("sexual_misconduct", "Sexual misconduct", SEVERE),
    RemovalReason("endangering_members", "Endangering members' safety", SEVERE),
)}
POSITION_CHANGE = "position_change"
REASON_GROUPS: tuple[tuple[str, str, tuple[RemovalReason, ...]], ...] = tuple(
    (cat, label, tuple(r for r in REMOVAL_REASONS.values() if r.category == cat))
    for cat, label in ((BENIGN, "Can rejoin later"), (VIOLATION, "Conduct violation - never again at your gyms"),
                       (SEVERE, "Severe violation - platform ban"))
)

NOTE_MIN = 10  # a VIOLATION / SEVERE removal needs a short, specific note ...
NOTE_MAX = 500  # ... of at most this many characters (also the cap of an optional BENIGN note)
LIFT_NOTE_MIN = 5  # the super-admin's note when lifting a ban
LIFT_NOTE_MAX = 500
IP_LOOKBACK_DAYS = 30  # addresses of the account's sessions / sign-ins this recent are considered ...
MAX_BANNED_IPS = 5  # ... at most this many, newest first
MAX_ID_HASHES = 30  # ID-number hashes registered per account (= the id_documents_per_user cap)
SUPERADMINS_NOTIFIED = 20  # super-admin accounts alerted per event (bounded fan-out)
SUSPEND_OTHER_LIMIT = 50  # other current employments suspended by one ban (a person rarely has more than a few)

CANT_ADD = "This person can't be added."
BLOCKED_MESSAGES = {
    "email": "This e-mail address can't be used for a new IronPulse account. If you think this is a mistake, "
             "please contact support.",
    "phone": "This phone number can't be used for a new IronPulse account. If you think this is a mistake, "
             "please contact support.",
    "id_number": "This ID document can't be used for a new IronPulse account. If you think this is a mistake, "
                 "please contact support.",
}
DISABLED_TITLE = "Your IronPulse account has been disabled"
DISABLED_BODY = ("Your IronPulse account has been disabled after a review of reports about conduct at a gym. You can't "
                 "sign in, and this e-mail address, phone number and ID can't be used for a new account. If you believe "
                 "this is a mistake, reply to this e-mail to ask for a review.")


def clean_note(value: Any) -> str:
    """One paragraph of plain text: whitespace collapsed (a note is a short statement, not a document)."""
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def validate_removal(reason: str, note: str) -> dict[str, str]:
    """Field errors of a removal form (empty = valid)."""
    errors: dict[str, str] = {}
    r = REMOVAL_REASONS.get(reason)
    if r is None:
        errors["reason"] = "Choose a reason from the list."
    if len(note) > NOTE_MAX:
        errors["note"] = f"Keep the note under {NOTE_MAX} characters."
    elif r is not None and r.category != BENIGN and len(note) < NOTE_MIN:
        errors["note"] = (f"Describe what happened in at least {NOTE_MIN} characters - it is kept on the record and "
                          "reviewed by the platform team.")
    return errors


# ====================================================================== identity hashes
def _keyed(kind: str, value: str) -> str:
    """HMAC-SHA256 with ID_HASH_PEPPER (the scheme of identity_documents.id_number_hash), domain-separated by kind."""
    return hmac.new(get_settings().id_hash_pepper.encode(), f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()


def normalize_email(email: str | None) -> str | None:
    """The canonical form of an address for "one person per e-mail": NFKC, trimmed, lower-case, without a '+tag', and
    without dots for Gmail (which ignores them) - so ``" John.Doe+x@GoogleMail.com "`` is ``johndoe@gmail.com``."""
    value = unicodedata.normalize("NFKC", str(email or "")).strip().lower()
    if not value:
        return None
    local, at, domain = value.rpartition("@")
    if not at or not local or not domain:
        return value
    local = local.split("+", 1)[0] or local
    domain = domain.rstrip(".")
    if domain in ("gmail.com", "googlemail.com"):
        local, domain = local.replace(".", "") or local, "gmail.com"
    return f"{local}@{domain}"


def email_hash(email: str | None) -> str | None:
    norm = normalize_email(email)
    return _keyed("email", norm) if norm else None


def phone_hash(phone: str | None, country_code: str | None = None) -> str | None:
    """Keyed hash of the E.164 form (``normalize_phone``); None for an empty or invalid number."""
    try:
        norm = normalize_phone(phone, country_code)
    except ValueError:
        return None
    return _keyed("phone", norm) if norm else None


# ====================================================================== the registry
async def registration_blocked(db: AsyncSession, *, email: str | None = None, phone: str | None = None,
                               id_number_hash: str | None = None, country_code: str | None = None,
                               exclude_user_id: int | None = None,
                               id_number_hashes: Iterable[str] = ()) -> str | None:
    """A neutral refusal when the e-mail, phone (any format; ``country_code`` for a national number) or an ID-number
    hash (``id_number_hash`` and / or every one of ``id_number_hashes``) belonged to a deleted or banned account - else
    None. ``exclude_user_id``: entries registered for that same account don't count (a restored account, or a banned
    one that was lifted, keeps its own identifiers). ONE query, whatever was given."""
    checks: list[tuple[str, str]] = []
    if (h := email_hash(email)) is not None:
        checks.append(("email", h))
    if phone and (h := phone_hash(phone, country_code)) is not None:
        checks.append(("phone", h))
    for h in dict.fromkeys([id_number_hash, *id_number_hashes]):
        if h:
            checks.append(("id_number", str(h)))
    if not checks:
        return None
    stmt = select(BlockedIdentity.kind).where(or_(*(and_(BlockedIdentity.kind == kind, BlockedIdentity.value_hash == value)
                                                    for kind, value in checks)))
    if exclude_user_id is not None:
        stmt = stmt.where(or_(BlockedIdentity.user_id.is_(None), BlockedIdentity.user_id != exclude_user_id))
    found = set((await db.execute(stmt.distinct())).scalars())
    return next((BLOCKED_MESSAGES[kind] for kind, _value in checks if kind in found), None)


async def _id_hashes(db: AsyncSession, *, user_id: int | None = None, employee_id: int | None = None,
                     verified_only: bool = False) -> set[str]:
    """ID-number hashes on the identity documents of an account (and of its employment records) or of one employment
    record. A REJECTED card is left out unless asked for: it was refused precisely because the number may not be
    this person's (another person's card, a duplicate), and registering it could block the real owner."""
    subjects = []
    if user_id is not None:
        subjects.append(IdentityDocument.user_id == user_id)
        subjects.append(IdentityDocument.employee_id.in_(select(Employee.id).where(Employee.user_id == user_id)))
    if employee_id is not None:
        subjects.append(IdentityDocument.employee_id == employee_id)
    if not subjects:
        return set()
    stmt = select(IdentityDocument.id_number_hash).where(or_(*subjects), IdentityDocument.id_number_hash.is_not(None))
    stmt = stmt.where(IdentityDocument.status == IdStatus.VERIFIED) if verified_only else stmt.where(
        IdentityDocument.status != IdStatus.REJECTED)
    return {h for h in (await db.execute(stmt.distinct().limit(MAX_ID_HASHES))).scalars().all() if h}


async def identity_entries(db: AsyncSession, user: User) -> list[tuple[str, str]]:
    """(kind, keyed hash) of every identifier of ``user``: e-mail, phone, ID numbers."""
    out: list[tuple[str, str]] = []
    if (h := email_hash(user.email)) is not None:
        out.append(("email", h))
    phone = user.phone_normalized or user.phone
    if phone and (h := phone_hash(phone, user.country_code)) is not None:
        out.append(("phone", h))
    out += [("id_number", h) for h in sorted(await _id_hashes(db, user_id=user.id))]
    return out


async def register_entries(db: AsyncSession, entries: Iterable[tuple[str, str]], *, reason: str,
                           user_id: int | None, actor: User | None, note: str | None = None) -> int:
    """Add registry rows (idempotent: one row per identifier and reason). Returns how many were new."""
    rows = [{"kind": kind, "value_hash": value, "reason": reason, "user_id": user_id,
             "actor_id": actor.id if actor is not None else None, "note": (note or None) and note[:255]}
            for kind, value in dict.fromkeys(entries)]
    if not rows:
        return 0
    result = await db.execute(pg_insert(BlockedIdentity).values(rows)
                              .on_conflict_do_nothing(index_elements=["kind", "value_hash", "reason"])
                              .returning(BlockedIdentity.id))
    return len(result.scalars().all())


async def register_identity(db: AsyncSession, user: User, *, reason: str, actor: User | None = None,
                            note: str | None = None) -> int:
    """Register the e-mail, phone and ID numbers of a deleted / banned account (``reason``: self_deleted |
    admin_deleted | banned | severe_violation). The caller commits."""
    return await register_entries(db, await identity_entries(db, user), reason=reason, user_id=user.id, actor=actor,
                                  note=note)


# ====================================================================== the person behind an employment
@dataclass(slots=True)
class Person:
    """Who an employment record (or an invitation) is about: the linked account, if any, and keyed identity hashes."""

    user: User | None
    email_hashes: set[str] = field(default_factory=set)
    id_hashes: set[str] = field(default_factory=set)

    @property
    def hashes(self) -> list[str]:
        return sorted(self.email_hashes | self.id_hashes)

    def match(self) -> Any:
        """WHERE clause: the person's violations (by account or by any identity hash) - re-hire at the same owner."""
        clause = StaffViolation.subject_hashes.overlap(self.hashes) if self.hashes else false()
        if self.user is not None:
            clause = or_(clause, StaffViolation.subject_user_id == self.user.id)
        return clause

    def ban_match(self) -> Any:
        """WHERE clause for the auto-ban threshold: violations of the person's account, or matched by identity hash on
        records that HAD a linked account. A never-accepted invitation proves nothing about who uses an address - an
        owner could otherwise invite anyone's e-mail and 'remove' it to push that person toward a platform ban."""
        clause = (and_(StaffViolation.subject_user_id.is_not(None), StaffViolation.subject_hashes.overlap(self.hashes))
                  if self.hashes else false())
        if self.user is not None:
            clause = or_(clause, StaffViolation.subject_user_id == self.user.id)
        return clause


async def person_of_employee(db: AsyncSession, emp: Employee) -> Person:
    user = await db.get(User, emp.user_id) if emp.user_id else None
    emails = {h for h in (email_hash(emp.invite_email), email_hash(user.email if user else None)) if h}
    ids = await _id_hashes(db, user_id=user.id if user else None, employee_id=emp.id, verified_only=True)
    return Person(user, emails, ids)


async def person_of_email(db: AsyncSession, email: str) -> Person:
    """The person an invitation to ``email`` reaches: the account with that address (if any) and its verified IDs."""
    user = await db.scalar(select(User).where(User.email == (email or "").strip().lower()))
    emails = {h for h in (email_hash(email), email_hash(user.email if user else None)) if h}
    ids = await _id_hashes(db, user_id=user.id, verified_only=True) if user is not None else set()
    return Person(user, emails, ids)


def _lock_key(name: str) -> int:
    return int.from_bytes(hashlib.blake2b(f"trust:{name}".encode(), digest_size=8).digest(), "big", signed=True)


async def lock_person(db: AsyncSession, person: Person) -> None:
    """Serialise every trust decision about this person until the caller's COMMIT / ROLLBACK (keys in a fixed order,
    so two transactions can never wait for each other in a circle)."""
    names = {f"hash:{h}" for h in person.hashes}
    if person.user is not None:
        names.add(f"user:{person.user.id}")
    for key in sorted(_lock_key(n) for n in names):
        await db.execute(select(func.pg_advisory_xact_lock(key)))


async def try_lock_person(db: AsyncSession, person: Person) -> bool:
    """``lock_person`` without waiting - ONE statement: False when another transaction is deciding about this person
    right now (the keys it did get stay held until COMMIT / ROLLBACK, as ``lock_person``'s). For a path that may already
    hold an employment row's lock (the activation check), where waiting could deadlock with a removal that holds the
    person's lock and waits for that row."""
    names = {f"hash:{h}" for h in person.hashes}
    if person.user is not None:
        names.add(f"user:{person.user.id}")
    keys = sorted(_lock_key(n) for n in names)
    if not keys:
        return True
    got = await db.scalar(select(pg_array([func.pg_try_advisory_xact_lock(k) for k in keys])))
    return all(got or [])


async def lock_employee(db: AsyncSession, employee_id: int) -> Any:
    """Lock the employment row (FOR UPDATE, until COMMIT) and return its CURRENT state - ``(status, deleted_at,
    user_id)``, or None when it is gone: the state every check after the lock must use (a double submit or a
    concurrent change committed meanwhile shows here, not in the instance loaded before the lock - whose loaded
    relationships stay usable, unlike a populate_existing reload)."""
    return (await db.execute(
        select(Employee.status, Employee.deleted_at, Employee.user_id).where(Employee.id == employee_id).with_for_update()
    )).first()


# ====================================================================== bans
async def active_ban(db: AsyncSession, user_id: int) -> AccountBan | None:
    return await db.scalar(select(AccountBan).where(AccountBan.user_id == user_id, AccountBan.lifted_at.is_(None))
                           .order_by(AccountBan.id.desc()).limit(1))


async def recent_violation_count(db: AsyncSession, person: Person) -> int:
    """How many DIFFERENT gym owners removed the person for a violation within VIOLATION_WINDOW_DAYS - only after the
    last lifted ban (a lift with a note is the super-admin's decision that the earlier ones no longer count). Counted
    per owner, not per gym: one owner ending the person's jobs at two of their gyms is one report, never a ban on its
    own (a violation already bars re-hire at all of that owner's gyms, so a second one from them is the same matter)."""
    since = utcnow() - timedelta(days=get_settings().violation_window_days)
    if person.user is not None:
        lifted = await db.scalar(select(func.max(AccountBan.lifted_at)).where(AccountBan.user_id == person.user.id))
        if lifted is not None and lifted > since:
            since = lifted
    owners = func.count(func.distinct(func.coalesce(StaffViolation.owner_id, -StaffViolation.id)))
    return int(await db.scalar(select(owners).where(person.ban_match(), StaffViolation.created_at >= since)) or 0)


async def _superadmins(db: AsyncSession) -> list[User]:
    return list((await db.execute(
        select(User).where(User.role == Role.SUPERADMIN, User.deleted_at.is_(None), User.is_active.is_(True))
        .order_by(User.id).limit(SUPERADMINS_NOTIFIED)
    )).scalars().all())


async def notify_superadmins(db: AsyncSession, title: str, body: str, *, link: str = "/superadmin/trust",
                             severity: Severity = Severity.WARNING) -> int:
    """Alert the (at most SUPERADMINS_NOTIFIED) active super-admins: ONE bulk send, never one notify per admin."""
    admins = await _superadmins(db)
    if admins:
        await db.flush()  # as notify() did: the caller's pending changes are written before the notices
        await notify_many(db, admins, title, body, category=NotificationCategory.SECURITY, severity=severity, link=link)
    return len(admins)


async def stop_account_work(db: AsyncSession, user_id: int, *, reason: str) -> None:
    """Cancel the account's jobs and AI requests (app/services/accounts.py, the deletion path)."""
    from app.services.accounts import stop_background_work

    await stop_background_work(db, user_id, reason=reason)


# ---------------------------------------------------------------------- what owners see of the platform's actions
PLATFORM_BY = ("platform", "trust")  # ``detail.by`` of an audit row that records what the PLATFORM did
PLATFORM_ACTOR = "IronPulse platform"  # how owners see the actor of such a row


def is_platform_row(log: Any) -> bool:
    """An audit row the platform wrote about its own action - e.g. an account ban suspending a job at another gym."""
    detail = getattr(log, "detail", None)
    return isinstance(detail, dict) and detail.get("by") in PLATFORM_BY


def audit_actor_join() -> Any:
    """ON clause joining an audit row's actor (``User``) for an owner-facing page - except on a platform row, whose
    actor is never shown nor matched by a name search: rows written before the platform recorded these side effects as
    its own named the owner whose report triggered the ban (another tenant must never learn who reported them)."""
    return and_(User.id == AuditLog.actor_id, func.coalesce(AuditLog.detail["by"].astext, "").not_in(PLATFORM_BY))


async def _suspend_other_employments(db: AsyncSession, user: User) -> int:
    """A banned person's other current employments are suspended; their owners learn only that the account was
    disabled by the platform (never which gym, why, or any note). Each gym's audit row is the PLATFORM's action - no
    actor, no request (address): the owner whose report triggered the ban appears only in the platform-level
    ``trust.account_ban`` row, which no gym's audit log shows."""
    emps = list((await db.execute(
        select(Employee).where(Employee.user_id == user.id, Employee.deleted_at.is_(None),
                               Employee.status.in_((EmploymentStatus.INVITED, EmploymentStatus.PENDING_VERIFICATION,
                                                    EmploymentStatus.ACTIVE)))
        .order_by(Employee.id).limit(SUSPEND_OTHER_LIMIT)
    )).scalars().all())
    if not emps:
        return 0
    gyms = {g.id: g for g in (await db.execute(select(Gym).where(Gym.id.in_({e.gym_id for e in emps})))).scalars()}
    notes: list[Note] = []
    for emp in emps:
        old = emp.status
        emp.status = EmploymentStatus.SUSPENDED
        gym = gyms.get(emp.gym_id)
        if gym is not None:
            notes.append(Note(
                gym.owner_id, f"{emp.full_name}'s staff access is suspended",
                f"{emp.full_name}'s IronPulse account has been disabled by the platform, so their staff access "
                f"at {gym.name} is suspended. Contact IronPulse support if you have questions.",
                link=f"/owner/staff/{emp.id}", category=NotificationCategory.STAFF, severity=Severity.WARNING,
            ))
        await audit(db, None, "staff.suspend", "employee", emp.id, gym_id=emp.gym_id, detail={
            "from": old.value, "summary": "IronPulse account disabled by the platform", "by": "platform"})
    if notes:  # the owners: ONE bulk send (``notify_bulk``), never one notify per employment
        await db.flush()  # as notify() did: the suspensions (and their audit rows) are written before the notices
        await notify_bulk(db, notes)
    return len(emps)


async def ban_account(db: AsyncSession, user: User, *, source: str, reason: str, violation: StaffViolation | None = None,
                      actor: User | None = None, request: Any = None) -> tuple[AccountBan | None, bool]:
    """Ban ``user`` platform-wide (the caller holds ``lock_person`` and commits). Returns (the active ban, whether it
    was created now). Platform and gym admin accounts are never banned automatically: the super-admin is asked to
    review instead (None)."""
    if user.role in (Role.SUPERADMIN, Role.OWNER):
        await notify_superadmins(db, f"Review needed: {user.full_name}",
                                 f"A staff removal for a conduct violation would ban account #{user.id}, which is a "
                                 f"{user.role.value} account - automatic bans never apply to admin accounts. Review it "
                                 "on the Trust & safety page.", severity=Severity.DANGER)
        return None, False
    existing = await active_ban(db, user.id)
    if existing is not None:
        return existing, False
    ban = AccountBan(user_id=user.id, person_name=(user.full_name or "")[:120], source=source, reason=reason[:300],
                     violation_id=violation.id if violation is not None else None,
                     created_by_id=actor.id if actor is not None else None)
    db.add(ban)
    await db.flush()
    from app.security.sessions import revoke_all_sessions

    user.is_active = False
    await revoke_all_sessions(db, user.id)  # closes their sockets and stops their sessions' AI work after COMMIT
    await stop_account_work(db, user.id, reason="account_banned")
    await _suspend_other_employments(db, user)
    await register_identity(db, user, reason="severe_violation" if source == "severe" else "banned", actor=actor,
                            note=f"Account ban #{ban.id}")
    await notify(db, user, DISABLED_TITLE, DISABLED_BODY, category=NotificationCategory.SECURITY,
                 severity=Severity.DANGER)
    await audit(db, actor, "trust.account_ban", "user", user.id, request=request, detail={
        "ban_id": ban.id, "source": source, "violation_id": ban.violation_id, "summary": reason[:200]})
    what = "a severe conduct violation" if source == "severe" else "repeated conduct violations"
    await notify_superadmins(db, f"Account banned: {user.full_name}",
                             f"Account #{user.id} was banned automatically after {what}. Review the evidence and lift "
                             "the ban with a note if it was a mistake.",
                             link=f"/superadmin/trust?tab=bans#ban-{ban.id}", severity=Severity.DANGER)
    return ban, True


PLATFORM_BAN_TITLE = "Your IronPulse account has been disabled"
PLATFORM_BAN_BODY = ("The IronPulse platform team disabled your account after a review. You can't sign in, and this "
                     "e-mail address, phone number and ID can't be used for a new account. Pay you have earned at a gym "
                     "is kept on record and settled. If you believe this is a mistake, reply to this e-mail to ask for a "
                     "review.")


async def platform_ban(db: AsyncSession, user: User, *, reason: str, severe: bool, actor: User, request: Any = None,
                       suspend_employments: bool = True) -> tuple[AccountBan, bool]:
    """A super-admin's platform ban (app/services/governance.py) - an owner's or a staff member's account, never a
    super-admin's (ValueError: the caller refuses first). The caller holds ``lock_person`` and commits. Returns (the
    active ban, whether it was created now); an account already banned keeps its ban (idempotent).

    As ``ban_account``: sign-in refused (``is_active`` false), every session revoked and its sockets closed, queued
    jobs and AI work stopped, the identity registered (one person per ID and e-mail), the person told - and with
    ``suspend_employments`` their other current employments suspended (the governance employee ban ends them itself).
    SEVERE also bans the account's recent network addresses through ``ban_recent_ips`` (its exclusions: shared
    addresses, the acting admin's and every platform admin's, non-public, whitelisted)."""
    if user.role == Role.SUPERADMIN:
        raise ValueError("platform admin accounts are never banned")
    existing = await active_ban(db, user.id)
    if existing is not None:
        return existing, False
    ban = AccountBan(user_id=user.id, person_name=(user.full_name or "")[:120], source="platform", reason=reason[:300],
                     created_by_id=actor.id)
    db.add(ban)
    await db.flush()
    from app.security.sessions import revoke_all_sessions

    user.is_active = False
    await revoke_all_sessions(db, user.id)
    await stop_account_work(db, user.id, reason="account_banned")
    if suspend_employments:
        await _suspend_other_employments(db, user)
    await register_identity(db, user, reason="severe_violation" if severe else "banned", actor=actor,
                            note=f"Platform ban #{ban.id}")
    await notify(db, user, PLATFORM_BAN_TITLE, PLATFORM_BAN_BODY, category=NotificationCategory.SECURITY,
                 severity=Severity.DANGER)
    await audit(db, actor, "trust.account_ban", "user", user.id, request=request, detail={
        "ban_id": ban.id, "source": "platform", "severe": severe, "summary": reason[:200]})
    if severe:
        await ban_recent_ips(db, user, ban, actor=actor, request=request)
    return ban, True


# ---------------------------------------------------------------------- IP bans
def _public_address(value: str | None) -> str | None:
    """The canonical text of a PUBLIC internet address, else None (loopback, private / LAN, link-local, carrier-grade
    NAT, documentation, multicast ... are never banned: the platform and its users' own networks use them)."""
    try:
        addr = ipaddress.ip_address((value or "").strip().strip("[]"))
    except ValueError:
        return None
    if getattr(addr, "ipv4_mapped", None) is not None:
        addr = addr.ipv4_mapped  # type: ignore[union-attr]
    if not addr.is_global or addr.is_multicast or addr.is_reserved or addr.is_unspecified:
        return None
    return str(addr)


async def _recent_addresses(db: AsyncSession, user: User, since: Any) -> list[str]:
    """The account's distinct addresses (sessions + last sign-in) since ``since``, newest first."""
    seen = func.max(func.greatest(UserSession.created_at, UserSession.last_seen_at))
    rows = (await db.execute(
        select(UserSession.ip, seen.label("seen")).where(
            UserSession.user_id == user.id, UserSession.ip.is_not(None),
            or_(UserSession.created_at >= since, UserSession.last_seen_at >= since))
        .group_by(UserSession.ip).order_by(seen.desc()).limit(MAX_BANNED_IPS * 4)
    )).all()
    found: list[tuple[Any, str]] = [(row.seen, row.ip) for row in rows]
    if user.last_login_ip and user.last_login_at is not None and user.last_login_at >= since:
        found.append((user.last_login_at, user.last_login_ip))
    found.sort(key=lambda t: t[0], reverse=True)
    out: list[str] = []
    for _seen, ip in found:
        raw = (ip or "").strip().strip("[]")
        try:
            raw = str(ipaddress.ip_address(raw))  # one entry per address, whatever text form a row stored
        except ValueError:
            pass
        if raw and raw not in out:
            out.append(raw)
    return out


def _canonical(ip: str) -> str:
    return _public_address(ip) or ip


async def _protected_addresses(db: AsyncSession, actor: User | None, request: Any, since: Any) -> set[str]:
    """Addresses that are never banned by this flow: the acting admin's current and recent ones."""
    out: set[str] = set()
    if request is not None:
        from app.security.net import client_ip

        out.add(client_ip(request))
    if actor is not None:
        out.update(ip for ip in (await db.execute(
            select(UserSession.ip).where(UserSession.user_id == actor.id, UserSession.ip.is_not(None),
                                         or_(UserSession.created_at >= since, UserSession.last_seen_at >= since))
            .distinct().limit(50)
        )).scalars().all() if ip)
        if actor.last_login_ip:
            out.add(actor.last_login_ip)
    return {_canonical(ip) for ip in out}


async def _platform_admin_addresses(db: AsyncSession, since: Any) -> set[str]:
    """Recent addresses of the platform's super-admins: banning one would lock out the people who lift bans."""
    admins = select(User.id).where(User.role == Role.SUPERADMIN, User.deleted_at.is_(None))
    rows = (await db.execute(
        select(UserSession.ip).where(UserSession.user_id.in_(admins), UserSession.ip.is_not(None),
                                     or_(UserSession.created_at >= since, UserSession.last_seen_at >= since))
        .distinct().limit(200)
    )).scalars().all()
    logins = (await db.execute(select(User.last_login_ip).where(
        User.role == Role.SUPERADMIN, User.deleted_at.is_(None), User.last_login_ip.is_not(None)).limit(50))).scalars().all()
    return {_canonical(ip) for ip in (*rows, *logins) if ip}


async def _other_accounts_on(db: AsyncSession, ip: str, user_id: int, since: Any, cap: int) -> int:
    """How many OTHER active accounts used ``ip`` since ``since`` (sessions or last sign-in) - counted up to ``cap``."""
    via_sessions = (select(UserSession.user_id.label("uid")).join(User, User.id == UserSession.user_id)
                    .where(UserSession.ip == ip, UserSession.user_id != user_id, User.is_active.is_(True),
                           User.deleted_at.is_(None),
                           or_(UserSession.created_at >= since, UserSession.last_seen_at >= since)))
    via_login = (select(User.id.label("uid"))
                 .where(User.last_login_ip == ip, User.id != user_id, User.is_active.is_(True),
                        User.deleted_at.is_(None), User.last_login_at >= since))
    others = via_sessions.union(via_login).subquery()
    return int(await db.scalar(select(func.count()).select_from(select(others.c.uid).limit(cap).subquery())) or 0)


async def ban_recent_ips(db: AsyncSession, user: User, ban: AccountBan, *, actor: User | None,
                         request: Any = None) -> list[TrustIpAction]:
    """IP-ban the account's recent public addresses (SEVERE). Every address considered gets a ``trust_ip_actions``
    row: banned, or skipped with the reason it was spared. The caller commits; the gate of this process applies the
    bans after the COMMIT, the other processes at their next ban sync (new ``ip_bans`` rows are read by id)."""
    from app.security.bans import BanInfo, ban_registry
    from app.security.net import is_trusted_proxy
    from app.services.realtime import call_after_commit

    s = get_settings()
    now = utcnow()
    since = now - timedelta(days=IP_LOOKBACK_DAYS)
    protected = await _protected_addresses(db, actor, request, since)
    platform_admins = await _platform_admin_addresses(db, since)
    whitelist = {str(w).strip() for w in (s.rate_limit_whitelist or [])}
    until = now + timedelta(days=s.severe_ip_ban_days)
    reason = f"Trust & safety: severe conduct violation (account {user.id})"
    actions: list[TrustIpAction] = []
    for raw in (await _recent_addresses(db, user, since))[:MAX_BANNED_IPS]:
        ip = _public_address(raw)
        other = None
        if ip is None:
            action, ip = "not_public", raw[:45]
        elif ip in protected or raw in protected:
            action = "actor"
        elif ip in platform_admins:
            action = "platform_admin"
        elif ip in whitelist or raw in whitelist or is_trusted_proxy(ip):
            action = "whitelisted"
        else:
            other = await _other_accounts_on(db, ip, user.id, since, s.shared_ip_threshold + 1)
            action = "shared" if other > s.shared_ip_threshold else "banned"
        row = TrustIpAction(account_ban_id=ban.id, user_id=user.id, ip=ip, action=action, other_accounts=other)
        if action == "banned":
            offences = int(await db.scalar(select(func.count(IpBan.id)).where(
                IpBan.ip == ip, IpBan.created_at > now - timedelta(days=30))) or 0) + 1
            ip_ban = IpBan(ip=ip, user_id=user.id, reason=reason[:300], severity=5, banned_until=until,
                           created_by="trust", offense_count=offences, had_subscription=False)
            db.add(ip_ban)
            await db.flush()
            row.ip_ban_id = ip_ban.id
            info = BanInfo(until=until, reason=ip_ban.reason, severity=5)
            call_after_commit(db, lambda ip=ip, info=info: ban_registry.put(ip, info))  # never shortens a longer ban
            await audit(db, actor, "trust.ip_ban", "ip_ban", ip_ban.id, request=request, detail={
                "ip": ip, "until": until.isoformat(), "account_ban_id": ban.id, "user_id": user.id})
        db.add(row)
        actions.append(row)
    await db.flush()
    flagged = [a for a in actions if a.action == "shared"]
    if flagged:
        await notify_superadmins(db, "Shared network address not banned",
                                 f"{len(flagged)} recent address{'es' if len(flagged) != 1 else ''} of banned account "
                                 f"#{user.id} {'were' if len(flagged) != 1 else 'was'} used by more than "
                                 f"{s.shared_ip_threshold} other accounts, so {'they were' if len(flagged) != 1 else 'it was'}"
                                 " not banned. Review on the Trust & safety page.",
                                 link="/superadmin/trust?tab=ips&action=shared")
    return actions


async def lift_account_ban(db: AsyncSession, ban: AccountBan, *, actor: User, note: str, request: Any = None) -> int:
    """Lift an active account ban (super-admin, with a note): the account may sign in again, the IP bans this ban
    created are lifted (other bans of those addresses stay), the person is told. Registry rows stay (the owner's
    rule) - they never block the account they came from. Returns how many IP bans were lifted. The caller commits."""
    from app.security.bans import BanInfo, ban_registry
    from app.services.realtime import call_after_commit

    now = utcnow()
    ban.lifted_at = now
    ban.lifted_by_id = actor.id
    ban.lift_note = note[:LIFT_NOTE_MAX]
    user = await db.get(User, ban.user_id)
    if user is not None and user.deleted_at is None:
        others = await db.scalar(select(AccountBan.id).where(AccountBan.user_id == user.id, AccountBan.id != ban.id,
                                                             AccountBan.lifted_at.is_(None)).limit(1))
        if others is None:
            user.is_active = True
            user.failed_login_count = 0
            user.locked_until = None
    ip_bans = list((await db.execute(
        select(IpBan).join(TrustIpAction, TrustIpAction.ip_ban_id == IpBan.id)
        .where(TrustIpAction.account_ban_id == ban.id, IpBan.lifted_at.is_(None))
    )).scalars().all())
    for ip_ban in ip_bans:
        ip_ban.lifted_at = now
        ip_ban.lifted_by_id = actor.id
    await db.flush()
    for ip in sorted({b.ip for b in ip_bans}):
        remaining = list((await db.execute(
            select(IpBan.banned_until, IpBan.severity).where(
                IpBan.ip == ip, IpBan.lifted_at.is_(None), or_(IpBan.banned_until.is_(None), IpBan.banned_until > now))
        )).all())
        info = None
        if remaining:
            permanent = [r for r in remaining if r.banned_until is None]
            best = permanent[0] if permanent else max(remaining, key=lambda r: r.banned_until)
            info = BanInfo(until=best.banned_until, reason="", severity=best.severity)
        call_after_commit(db, lambda ip=ip, info=info: ban_registry.set_confirmed(ip, info))
        # the other processes reload this address from the audit trail (app/security/bans.py BAN_CHANGE_ACTIONS)
        await audit(db, actor, "security.ban_lift", "ip_ban", ip_bans[0].id, request=request, detail={
            "ip": ip, "bans": [b.id for b in ip_bans if b.ip == ip], "note": note[:300], "by": "trust"})
    if user is not None and user.deleted_at is None and user.is_active:
        await notify(db, user, "Your IronPulse account is active again",
                     "The IronPulse team reviewed your account and lifted the restriction. You can sign in again. Staff "
                     "positions that were suspended are not reactivated automatically - contact your gym.",
                     category=NotificationCategory.SECURITY, severity=Severity.SUCCESS, link="/auth/login")
    await audit(db, actor, "trust.ban_lift", "user", ban.user_id, request=request, detail={
        "ban_id": ban.id, "note": note[:LIFT_NOTE_MAX], "ip_bans_lifted": len(ip_bans)})
    return len(ip_bans)


# ====================================================================== re-hire eligibility
async def rehire_refusal(db: AsyncSession, *, owner_id: int, email: str) -> str | None:
    """``CANT_ADD`` when the person an invitation to ``email`` reaches may not be added by this owner: their account is
    banned, the identity is in the blocked registry (a deleted / banned account), or a gym of this owner ended an
    employment of theirs for a VIOLATION / SEVERE reason - matched by account, normalised e-mail and verified ID-number
    hash. A violation at ANOTHER owner's gym never refuses (and is never shown); BENIGN history never refuses."""
    person = await person_of_email(db, email)
    user = person.user
    if user is not None and await active_ban(db, user.id) is not None:
        return CANT_ADD
    if await registration_blocked(db, email=email, id_number_hashes=person.id_hashes,
                                  exclude_user_id=user.id if user is not None else None):
        return CANT_ADD  # the e-mail or any verified ID number of the person (one query)
    found = await db.scalar(select(StaffViolation.id).where(StaffViolation.owner_id == owner_id, person.match()).limit(1))
    return CANT_ADD if found is not None else None


# ====================================================================== ending an employment
@dataclass(slots=True)
class RemovalOutcome:
    employee: Employee
    reason: RemovalReason
    violation: StaffViolation | None = None
    ban: AccountBan | None = None
    banned_now: bool = False
    ip_actions: list[TrustIpAction] = field(default_factory=list)
    shifts_deleted: int = 0
    clients_ended: int = 0
    withdrawn: list[int] = field(default_factory=list)  # the person's other pending records at this owner's gyms


WITHDRAW_PENDING_MAX = 50  # other pending records (invited / in verification) one violation removal withdraws ...
WITHDRAW_CANDIDATES_MAX = 200  # ... picked from at most this many records of the same account or e-mail domain


def _email_domain(email: str | None) -> str | None:
    norm = normalize_email(email)
    return norm.rpartition("@")[2] if norm and "@" in norm else None


async def _withdraw_pending_records(db: AsyncSession, emp: Employee, gym: Gym, person: Person, r: RemovalReason,
                                    note: str, actor: User, now: Any, request: Any) -> list[int]:
    """A VIOLATION / SEVERE removal also withdraws the person's OTHER pending records - invited, or linked and still in
    verification - at the same owner's gyms: that owner may never add them again (``rehire_refusal``), so neither an
    invitation sent before the violation nor one accepted but not yet active may still become an employment. Matched by
    the linked account or the invitation's normalised e-mail (``person.email_hashes``); ended like ``emp`` (TERMINATED,
    same reason and note), their shifts, client links and unused invite links removed - set-based, under the person
    lock the caller holds, each record's row locked in id order (the documented order: person, then rows)."""
    domains = {d for d in (_email_domain(emp.invite_email), _email_domain(person.user.email if person.user else None)) if d}
    if "gmail.com" in domains:
        domains.add("googlemail.com")
    stored_domain = func.rtrim(func.split_part(func.lower(func.btrim(Employee.invite_email)), "@", 2), ".")
    who: list[Any] = [stored_domain.in_(sorted(domains))] if domains else []
    if person.user is not None:
        who.append(Employee.user_id == person.user.id)
    if not who:
        return []
    candidates = list((await db.execute(
        select(Employee).where(
            Employee.id != emp.id, Employee.deleted_at.is_(None),
            Employee.status.in_((EmploymentStatus.INVITED, EmploymentStatus.PENDING_VERIFICATION)),
            Employee.gym_id.in_(select(Gym.id).where(Gym.owner_id == gym.owner_id)), or_(*who))
        .order_by(Employee.id).limit(WITHDRAW_CANDIDATES_MAX).with_for_update()
    )).scalars().all())
    uid = person.user.id if person.user is not None else None
    doomed = [e for e in candidates
              if (uid is not None and e.user_id == uid) or email_hash(e.invite_email) in person.email_hashes]
    doomed = doomed[:WITHDRAW_PENDING_MAX]
    if not doomed:
        return []
    ids = [e.id for e in doomed]
    was = {e.id: e.status.value for e in doomed}
    for other in doomed:  # attributes only: written by the next flush, one UPDATE per row (at most WITHDRAW_PENDING_MAX)
        other.status = EmploymentStatus.TERMINATED
        other.termination_date = emp.termination_date
        other.termination_reason = r.label[:255]
        other.removal_reason, other.removal_category = r.code, r.category
        other.removal_note = note or None
        other.removed_by_id, other.removed_at = actor.id, now
    await db.execute(TrainerShift.__table__.delete().where(TrainerShift.employee_id.in_(ids)))
    await db.execute(update(TrainerClient).where(TrainerClient.employee_id.in_(ids), TrainerClient.active.is_(True))
                     .values(active=False).execution_options(synchronize_session=False))
    await db.execute(update(Subscription).where(Subscription.trainer_id.in_(ids)).values(trainer_id=None)
                     .execution_options(synchronize_session=False))
    await db.execute(AuthToken.__table__.delete().where(AuthToken.employee_id.in_(ids),
                                                        AuthToken.purpose == TokenPurpose.STAFF_INVITE,
                                                        AuthToken.used_at.is_(None)))
    ip = None
    if request is not None:
        from app.security.net import client_ip

        ip = client_ip(request)
    db.add_all([AuditLog(actor_id=actor.id, gym_id=other.gym_id, action="staff.removed", entity_type="employee",
                         entity_id=other.id, ip=ip,
                         detail={"reason": r.code, "category": r.category, "summary": f"{r.label} (withdrawn)",
                                 "withdrawn_with": emp.id, "from": was[other.id]})
                for other in doomed])
    return ids


async def end_employment(db: AsyncSession, emp: Employee, *, reason: str, note: str = "", actor: User,
                         gym: Gym | None = None, person: Person | None = None, request: Any = None,
                         termination_date: date | None = None, notify_person: bool = True,
                         platform: bool = False) -> RemovalOutcome:
    """End ``emp`` now with ``reason`` (REMOVAL_REASONS) and apply its consequences. The caller has taken
    ``lock_person`` and ``lock_employee`` (in that order), checked that the employment is current, and commits.

    ``platform``: a super-admin's governance ban ends it (app/services/governance.py) - the record keeps the reason and
    note, but no violation record, auto-ban or IP ban follows here (the platform ban did what it decided itself), and
    the gym's audit log shows the IronPulse platform as the actor (``PLATFORM_BY``), never the admin.

    Access ends at once: status TERMINATED (staff areas, clients and chats check it on every request), weekly shifts
    deleted, client links ended (their members are told the gym will assign a new trainer - never why), unused invite
    links invalidated. VIOLATION / SEVERE add the immutable violation record and the bans described in the module
    docstring, and withdraw the person's other pending records at this owner's gyms (``_withdraw_pending_records``)."""
    r = REMOVAL_REASONS[reason]
    gym = gym or await db.get(Gym, emp.gym_id)
    person = person or await person_of_employee(db, emp)
    now = utcnow()
    note = clean_note(note)[:NOTE_MAX]
    emp.status = EmploymentStatus.TERMINATED
    emp.termination_date = termination_date or local_today(getattr(actor, "timezone", None))
    emp.termination_reason = r.label[:255]
    emp.removal_reason, emp.removal_category = r.code, r.category
    emp.removal_note = note or None
    emp.removed_by_id, emp.removed_at = actor.id, now
    shifts = (await db.execute(TrainerShift.__table__.delete().where(TrainerShift.employee_id == emp.id))).rowcount or 0
    client_ids = sorted(set((await db.execute(
        update(TrainerClient).where(TrainerClient.employee_id == emp.id, TrainerClient.active.is_(True))
        .values(active=False).returning(TrainerClient.customer_id).execution_options(synchronize_session=False)
    )).scalars().all()))
    await db.execute(update(Subscription).where(Subscription.trainer_id == emp.id).values(trainer_id=None)
                     .execution_options(synchronize_session=False))
    await db.execute(AuthToken.__table__.delete().where(AuthToken.employee_id == emp.id,
                                                        AuthToken.purpose == TokenPurpose.STAFF_INVITE,
                                                        AuthToken.used_at.is_(None)))
    if client_ids:
        await notify_many(db, client_ids, "Trainer change",
                          f"{emp.full_name} is no longer your trainer at {gym.name}. The gym will assign a new trainer "
                          "for your plan.", category=NotificationCategory.STAFF, link="/chat")
    if notify_person:
        title = f"Your employment at {gym.name} has ended"
        body = f"Your staff access at {gym.name} ended today. Reason given by the gym: {r.label}."
        if emp.user_id is not None:
            await notify(db, emp.user_id, title, body, category=NotificationCategory.STAFF, severity=Severity.WARNING,
                         link="/staff")
        elif emp.invite_email:
            await email_address(db, emp.invite_email, f"Your invitation from {gym.name} was withdrawn",
                                f"{gym.name} withdrew its staff invitation. The link we sent earlier no longer works.",
                                recipient=emp.full_name)
    detail: dict[str, Any] = {"reason": r.code, "category": r.category, "summary": r.label, "shifts_deleted": shifts,
                              "clients_ended": len(client_ids)}
    if platform:
        detail["by"] = "platform"
    await audit(db, actor, "staff.removed", "employee", emp.id, gym_id=emp.gym_id, request=request, detail=detail)
    out = RemovalOutcome(emp, r, shifts_deleted=shifts, clients_ended=len(client_ids))
    if r.category == BENIGN or platform:
        return out
    if gym is not None:  # the person's other pending invitations at this owner's gyms end with this one
        out.withdrawn = await _withdraw_pending_records(db, emp, gym, person, r, note, actor, now, request)

    violation = StaffViolation(
        employee_id=emp.id, gym_id=emp.gym_id, owner_id=gym.owner_id if gym else None, actor_id=actor.id,
        subject_user_id=person.user.id if person.user is not None else None, subject_hashes=person.hashes,
        gym_name=(gym.name if gym else "")[:160], person_name=(emp.full_name or "")[:120], reason=r.code,
        category=r.category, note=note,
    )
    db.add(violation)
    await db.flush()
    out.violation = violation
    await audit(db, actor, "trust.violation", "staff_violation", violation.id, gym_id=emp.gym_id, request=request,
                detail={"employee_id": emp.id, "reason": r.code, "category": r.category,
                        "subject_user_id": violation.subject_user_id})
    if person.user is None:
        # a never-accepted invitation: no account to ban, and no proof of who uses that address - blocking it on one
        # gym's word would let an owner bar anyone's e-mail from IronPulse. The record still bars re-hire at this
        # owner's gyms; a severe one is flagged for the platform team.
        if r.category == SEVERE:
            await notify_superadmins(db, f"Severe violation reported: {emp.full_name}",
                                     f"{violation.gym_name} ended an invitation for a severe reason ({r.label}). The "
                                     "person never linked an IronPulse account, so nothing was banned - review the "
                                     "record on the Trust & safety page.", link="/superadmin/trust?tab=violations")
        return out
    count = await recent_violation_count(db, person)
    threshold = get_settings().violation_autoban_threshold
    if r.category != SEVERE and count < threshold:
        return out
    source = "severe" if r.category == SEVERE else "violations"
    why = (f"Severe violation at {violation.gym_name}: {r.label}" if source == "severe"
           else f"Conduct violations reported by {count} gyms within {get_settings().violation_window_days} days")
    out.ban, out.banned_now = await ban_account(db, person.user, source=source, reason=why, violation=violation,
                                                actor=actor, request=request)
    if r.category == SEVERE and out.ban is not None:
        out.ip_actions = await ban_recent_ips(db, person.user, out.ban, actor=actor, request=request)
    return out


async def payroll_blockers(db: AsyncSession, emp: Employee) -> list[str]:
    """Why payroll says this employment can't end yet (unpaid statements, no final settlement) - empty = clear."""
    from app.services import payroll as pr

    return [b for b in await pr.termination_blockers(db, emp) if "already been terminated" not in b]


async def successor_of(db: AsyncSession, emp: Employee) -> Employee | None:
    """The record that replaced ``emp`` after a position change (if any)."""
    return await db.scalar(select(Employee).where(Employee.previous_employment_id == emp.id)
                           .order_by(Employee.id.desc()).limit(1))


async def removal_actor_name(db: AsyncSession, emp: Employee) -> str | None:
    if emp.removed_by_id is None:
        return None
    return await db.scalar(select(User.full_name).where(User.id == emp.removed_by_id))


def reason_label(code: str | None) -> str | None:
    r = REMOVAL_REASONS.get(code or "")
    return r.label if r is not None else None


__all__ = [
    "BENIGN", "BLOCKED_MESSAGES", "CANT_ADD", "CATEGORY_LABELS", "NOTE_MAX", "NOTE_MIN", "PLATFORM_ACTOR", "PLATFORM_BY",
    "POSITION_CHANGE", "REASON_GROUPS", "REMOVAL_REASONS", "SEVERE", "VIOLATION", "Person", "RemovalOutcome",
    "RemovalReason", "active_ban", "audit_actor_join", "ban_account", "ban_recent_ips", "email_hash", "end_employment",
    "is_platform_row", "lift_account_ban", "lock_employee", "lock_person", "normalize_email", "payroll_blockers",
    "person_of_email", "person_of_employee", "phone_hash", "platform_ban", "reason_label", "recent_violation_count",
    "register_identity", "registration_blocked", "rehire_refusal", "try_lock_person", "validate_removal",
]
