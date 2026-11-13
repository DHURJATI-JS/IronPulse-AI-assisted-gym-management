"""Account identifiers: username, member ID, and "sign in with any identifier".

A person can sign in with their e-mail, @username, phone number or member ID (``IP-000123``).
Usernames are unique (case-insensitive), 3-30 chars of a-z 0-9 . _ - and stay reserved while a
deleted account is inside its 30-day retention window (the row still exists).
"""

from __future__ import annotations

import re
import secrets

from sqlalchemy import String, any_, literal, or_, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import normalize_phone, slugify
from app.models import User

USERNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{1,28})[a-z0-9]$")
MEMBER_ID_RE = re.compile(r"^\s*IP[-\s]?0*(\d{1,10})\s*$", re.IGNORECASE)
PHONE_LIKE_RE = re.compile(r"^\+?[\d\s().-]{7,20}$")
RESERVED = {
    "admin", "administrator", "root", "support", "help", "ironpulse", "owner", "staff", "superadmin", "system",
    "api", "auth", "me", "profile", "account", "login", "logout", "signup", "settings", "security", "null", "none",
}


def member_id(user_or_id: User | int) -> str:
    uid = user_or_id if isinstance(user_or_id, int) else user_or_id.id
    return f"IP-{uid:06d}"


def parse_member_id(value: str | None) -> int | None:
    m = MEMBER_ID_RE.match(value or "")
    return int(m.group(1)) if m else None


def normalize_username(value: str | None) -> str:
    return (value or "").strip().lstrip("@").lower()


def username_problem(value: str | None) -> str | None:
    name = normalize_username(value)
    if not USERNAME_RE.match(name):
        return "Use 3-30 characters: letters, numbers, dots, dashes or underscores (start and end with a letter or number)."
    if ".." in name or "__" in name or "--" in name:
        return "Usernames can't contain repeated punctuation."
    if name in RESERVED or name.startswith("ip-"):
        return "That username is reserved - please pick another one."
    return None


async def username_taken(db: AsyncSession, value: str, *, exclude_user_id: int | None = None) -> bool:
    stmt = select(User.id).where(User.username == normalize_username(value))
    if exclude_user_id is not None:
        stmt = stmt.where(User.id != exclude_user_id)
    return (await db.execute(stmt.limit(1))).first() is not None


def _username_base(full_name: str | None, email: str | None) -> str:
    base = slugify(full_name or (email or "member").split("@")[0], max_len=24).replace("-", ".")
    base = re.sub(r"[^a-z0-9._]", "", base).strip("._") or "member"
    if len(base) < 3:
        base = f"{base}fit"
    if base in RESERVED:
        base = f"{base}.member"
    return base


async def generate_username(db: AsyncSession, full_name: str | None, email: str | None = None) -> str:
    """Readable unique handle such as ``riya.kapoor`` / ``riya.kapoor42``."""
    base = _username_base(full_name, email)
    candidate = base[:30]
    for _ in range(12):
        if not await username_taken(db, candidate):
            return candidate
        candidate = f"{base[:24]}{secrets.randbelow(9000) + 100}"
    return f"member{secrets.randbelow(10**8):08d}"


async def find_user_by_identifier(db: AsyncSession, identifier: str | None, *, default_country: str | None = "IN") -> User | None:
    """Resolve an e-mail, @username, phone number or member ID to a user (or None). Never raises."""
    raw = (identifier or "").strip()
    if not raw or len(raw) > 254:
        return None
    if "@" in raw and not raw.startswith("@"):
        return await db.scalar(select(User).where(User.email == raw.lower()))
    uid = parse_member_id(raw)
    if uid is not None:
        return await db.get(User, uid)
    if PHONE_LIKE_RE.match(raw) and sum(ch.isdigit() for ch in raw) >= 7:
        candidates: set[str] = set()
        for country in (None, default_country):
            try:
                norm = normalize_phone(raw, country)
            except ValueError:
                continue
            if norm:
                candidates.add(norm)
        if candidates:
            user = await db.scalar(select(User).where(User.phone_normalized.in_(candidates)).limit(1))
            if user is not None:
                return user
    name = normalize_username(raw)
    if USERNAME_RE.match(name):
        return await db.scalar(select(User).where(User.username == name))
    return None


async def find_users_for_recovery(db: AsyncSession, contact: str, *, default_country: str | None = "IN") -> list[User]:
    """Accounts matching an e-mail or phone number (for the 'forgot username' flow)."""
    raw = (contact or "").strip()
    conds = []
    if "@" in raw:
        conds.append(User.email == raw.lower())
    elif PHONE_LIKE_RE.match(raw):
        for country in (None, default_country):
            try:
                norm = normalize_phone(raw, country)
            except ValueError:
                norm = None
            if norm:
                conds.append(User.phone_normalized == norm)
    if not conds:
        return []
    rows = await db.execute(select(User).where(or_(*conds), User.deleted_at.is_(None)).limit(5))
    return list(rows.scalars().all())


def _like_prefix(prefix: str) -> str:
    """A LIKE pattern for ``prefix%`` (``_`` and ``%`` in a username are literal characters, not wildcards)."""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def backfill_usernames(db: AsyncSession, limit: int = 500) -> int:
    """Give every account without a username one (startup + seed), like ``generate_username`` - in a constant number of
    statements: the batch, every existing username that starts like one of its candidates (one LIKE ANY), the choice
    in memory, one flush. Returns how many were set."""
    users = (await db.execute(select(User).where(User.username.is_(None)).order_by(User.id).limit(limit))).scalars().all()
    if not users:
        return 0
    bases = {u.id: _username_base(u.full_name, u.email) for u in users}
    patterns = sorted({_like_prefix(b[:24]) for b in bases.values()} | {"member%"})
    taken = set((await db.execute(
        select(User.username).where(User.username.like(any_(literal(patterns, ARRAY(String)))))
    )).scalars())
    chosen: list[dict[str, object]] = []
    for u in users:
        base = bases[u.id]
        candidate = base[:30]
        for _ in range(11):
            if candidate not in taken:
                break
            candidate = f"{base[:24]}{secrets.randbelow(9000) + 100}"
        while candidate in taken:
            candidate = f"member{secrets.randbelow(10**8):08d}"
        taken.add(candidate)
        chosen.append({"id": u.id, "username": candidate})
        set_committed_value(u, "username", candidate)
    await db.execute(update(User), chosen)  # one bulk UPDATE by primary key (never one statement per account)
    return len(users)
