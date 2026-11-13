"""WP-A2 helpers shared by ``routers/auth.py`` and ``routers/account.py``.

* reference data for forms (countries, dial codes, grouped IANA time zones, guardian relations)
* validators returning ``(value, error)`` pairs so handlers can collect *field* errors and re-render
* one-time tokens (``AuthToken``): only the SHA-256 of the random token is stored, never the token
* session start with rotation + one session per account (``start_session``) + the safe post-login redirect target
* the sign-in lockout: ``count_failed_sign_in`` (one atomic UPDATE per wrong password of an account),
  ``clear_failed_sign_ins`` (the conditional write a correct password must win before it signs in: a lock set by
  guesses verified at the same time is never bypassed) and ``ghost_throttle``, which mirrors it for identifiers
  without an account (keyed like ``identifier_key``: every spelling of one identifier shares a counter, as a real
  account's does) - shared by every worker through Redis, else in memory - so the sign-in page behaves identically
  whether or not an account exists (no user enumeration)
* ``blocked_identity``: the trust-and-safety registry of deleted / banned accounts' identifiers (sign-up, invitations)

Other work packages that need to e-mail a one-time link (customer invites - WP-F, staff invites - WP-C1)
can call ``issue_token(db, TokenPurpose.INVITE | STAFF_INVITE, ...)`` and link to
``/auth/invite?token=<raw>`` or ``/auth/staff-invite?token=<raw>``.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any

from fastapi import Request, Response
from sqlalchemy import DateTime, case, func, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.config import get_settings
from app.core.utils import (
    DIAL_CODES,
    is_safe_redirect,
    is_valid_timezone,
    normalize_phone,
    sha256_hex,
    strip_bidi,
    utcnow,
)
from app.models import AuthToken, SecurityEvent, User
from app.models.enums import IdDocType, Role, ThreatCategory, TokenPurpose
from app.security.crypto import new_token, token_hash
from app.security.net import client_ip
from app.security.passwords import password_problems
from app.security.ratelimit import SharedStore, lua_script, shared_key, shared_store
from app.security.sessions import (
    create_session,
    device_label,
    end_other_sessions,
    get_session_token,
    lock_account_sessions,
    revoke_session,
    set_session_cookie,
    single_session_enabled,
)

# ------------------------------------------------------------------ reference data
ROLE_HOME: dict[Role, str] = {
    Role.CUSTOMER: "/me",
    Role.OWNER: "/owner",
    Role.STAFF: "/staff",
    Role.SUPERADMIN: "/superadmin",
}

COUNTRY_NAMES: dict[str, str] = {
    "AE": "United Arab Emirates", "AT": "Austria", "AU": "Australia", "BD": "Bangladesh", "BE": "Belgium",
    "BH": "Bahrain", "BR": "Brazil", "CA": "Canada", "CH": "Switzerland", "CN": "China", "DE": "Germany",
    "DK": "Denmark", "EG": "Egypt", "ES": "Spain", "FI": "Finland", "FR": "France", "GB": "United Kingdom",
    "ID": "Indonesia", "IE": "Ireland", "IN": "India", "IT": "Italy", "JP": "Japan", "KE": "Kenya",
    "KR": "South Korea", "KW": "Kuwait", "LK": "Sri Lanka", "MX": "Mexico", "MY": "Malaysia", "NG": "Nigeria",
    "NL": "Netherlands", "NO": "Norway", "NP": "Nepal", "NZ": "New Zealand", "OM": "Oman", "PH": "Philippines",
    "PK": "Pakistan", "PL": "Poland", "PT": "Portugal", "QA": "Qatar", "SA": "Saudi Arabia", "SE": "Sweden",
    "SG": "Singapore", "TH": "Thailand", "TR": "Türkiye", "US": "United States", "VN": "Vietnam",
    "ZA": "South Africa",
}
# (code, name) sorted by name - for "Country" selects
COUNTRIES: list[tuple[str, str]] = sorted(COUNTRY_NAMES.items(), key=lambda kv: kv[1])
# (code, short label, full name) sorted by code - for the compact phone country-code select
PHONE_COUNTRIES: list[tuple[str, str, str]] = [
    (code, f"{code} +{DIAL_CODES[code]}", f"{COUNTRY_NAMES.get(code, code)} (+{DIAL_CODES[code]})")
    for code in sorted(DIAL_CODES)
]

GUARDIAN_RELATIONS: list[tuple[str, str]] = [
    ("parent", "Parent"),
    ("legal_guardian", "Legal guardian"),
    ("grandparent", "Grandparent"),
    ("adult_sibling", "Adult sibling"),
    ("other", "Other adult relative"),
]

DELETE_REASONS: list[tuple[str, str]] = [
    ("not_using", "I don't use IronPulse any more"),
    ("privacy", "Privacy concerns"),
    ("switching", "I'm moving to another gym or app"),
    ("too_many_emails", "Too many e-mails"),
    ("other", "Something else"),
]

ID_DOC_TYPES = {t.value for t in IdDocType}

TZ_REGIONS = ("Africa", "America", "Antarctica", "Arctic", "Asia", "Atlantic", "Australia", "Europe", "Indian", "Pacific")


@lru_cache(maxsize=1)
def timezone_groups() -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """IANA zones grouped by region for <optgroup>s: (("Asia", (("Asia/Kolkata", "Kolkata"), ...)), ...)."""
    try:
        from zoneinfo import available_timezones

        zones = available_timezones()
    except Exception:  # pragma: no cover - tz database missing (Windows without tzdata)
        zones = set()
    groups: dict[str, list[tuple[str, str]]] = {}
    for zone in sorted(zones):
        region, _, rest = zone.partition("/")
        if region in TZ_REGIONS and rest:
            groups.setdefault(region, []).append((zone, rest.replace("_", " ").replace("/", " · ")))
    out: list[tuple[str, tuple[tuple[str, str], ...]]] = [("General", (("UTC", "UTC (Coordinated Universal Time)"),))]
    out += [(region, tuple(groups[region])) for region in TZ_REGIONS if region in groups]
    return tuple(out)


def role_home(user: User | None) -> str:
    if user is None:
        return "/"
    return ROLE_HOME.get(user.role, "/")


def first_name(user: User | None) -> str:
    name = (getattr(user, "full_name", None) or "").strip()
    return name.split()[0] if name else "there"


# ------------------------------------------------------------------ form reading + validators
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES = re.compile(r"\s+")
EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
_NAME_FORBIDDEN = re.compile(r"[<>{}\[\]\\|^~`=@#$%*+_;:\"?!/]")
_PHONE_CHARS = re.compile(r"^\+?[\d\s().-]{1,32}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")


def clean_text(value: Any, cap: int = 1000) -> str:
    """Single-line text: control characters removed, whitespace collapsed, trimmed (bounded by ``cap``)."""
    if not isinstance(value, str):
        return ""
    value = _CTRL.sub("", value[: cap * 2])
    return _SPACES.sub(" ", value).strip()[:cap]


def has_file(value: Any) -> bool:
    return bool(getattr(value, "filename", None)) and hasattr(value, "read")


class FormReader:
    """Typed, bounded access to a Starlette ``FormData`` (never trusts sizes or types)."""

    def __init__(self, form: Any) -> None:
        self.form = form

    def text(self, name: str, cap: int = 500) -> str:
        return clean_text(self.form.get(name), cap)

    def secret(self, name: str, cap: int = 256) -> str:
        """Passwords: kept verbatim (no trimming), bounded."""
        value = self.form.get(name)
        return value[:cap] if isinstance(value, str) else ""

    def flag(self, name: str) -> bool:
        value = self.form.get(name)
        return isinstance(value, str) and value.strip().lower() in ("1", "on", "true", "yes")

    def upload(self, name: str) -> Any:
        value = self.form.get(name)
        return value if has_file(value) else None


def validate_email(raw: str | None) -> tuple[str, str | None]:
    email = (raw or "").strip().lower()
    if not email:
        return "", "Enter your e-mail address."
    if len(email) > 254 or ".." in email or not EMAIL_RE.fullmatch(email):
        return email, "Enter a valid e-mail address, like name@example.com."
    return email, None


def validate_name(raw: str | None, *, what: str = "your full name") -> tuple[str, str | None]:
    name = clean_text(strip_bidi(raw) if isinstance(raw, str) else raw, 300)  # names render everywhere: no bidi controls
    if not name:
        return "", f"Enter {what}."
    if len(name) < 2:
        return name, "That name is too short."
    if len(name) > 120:
        return name, "Use at most 120 characters."
    if not _HAS_LETTER.search(name):
        return name, "Use letters for the name."
    if _NAME_FORBIDDEN.search(name):
        return name, "Names can contain letters, spaces, apostrophes, dots and hyphens only."
    return name, None


def validate_city(raw: str | None, *, required: bool = True) -> tuple[str, str | None]:
    city = clean_text(raw, 300)
    if not city:
        return "", ("Enter your city." if required else None)
    if len(city) > 120:
        return city, "Use at most 120 characters."
    if not _HAS_LETTER.search(city) or _NAME_FORBIDDEN.search(city.replace("/", "")):
        return city, "Enter a real city name."
    return city, None


def validate_country(raw: str | None, *, required: bool = True) -> tuple[str, str | None]:
    code = (raw or "").strip().upper()
    if not code:
        return "", ("Choose your country." if required else None)
    if code not in COUNTRY_NAMES:
        return code, "Choose a country from the list."
    return code, None


def age_on(dob: date, today: date | None = None) -> int:
    today = today or date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def validate_dob(raw: str | None, *, min_age: int = 5, max_age: int = 110, today: date | None = None,
                 required: bool = True) -> tuple[date | None, str | None]:
    value = (raw or "").strip()
    if not value:
        return None, ("Enter your date of birth." if required else None)
    try:
        dob = date.fromisoformat(value)
    except ValueError:
        return None, "Enter a valid date (YYYY-MM-DD)."
    today = today or date.today()
    if dob > today:
        return None, "Your date of birth can't be in the future."
    age = age_on(dob, today)
    if age < min_age:
        return dob, f"You must be at least {min_age} years old."
    if age > max_age:
        return dob, f"Please check the year - ages above {max_age} aren't accepted."
    return dob, None


def validate_phone(raw: str | None, country: str | None, *, required: bool = True) -> tuple[str, str | None, str | None]:
    """Returns (as typed, E.164 normalised, error)."""
    phone = clean_text(raw, 64)
    if not phone:
        return "", None, ("Enter a phone number." if required else None)
    if not _PHONE_CHARS.fullmatch(phone):
        return phone, None, "Use digits only - spaces, dashes, brackets and a leading + are fine."
    cc = (country or "").strip().upper()
    if cc and cc not in DIAL_CODES:
        return phone, None, "Choose the country code for this phone number."
    try:
        normalized = normalize_phone(phone, cc or None)
    except ValueError as exc:
        return phone, None, str(exc)
    return phone, normalized, None


async def validate_username(db: AsyncSession, raw: str | None, *, exclude_user_id: int | None = None,
                            required: bool = False) -> tuple[str, str | None]:
    """Optional @handle (sign-in identifier). Unique case-insensitively, reserved while a deleted account
    is inside its retention window (see app.services.identity_lookup)."""
    from app.services.identity_lookup import normalize_username, username_problem, username_taken

    name = normalize_username(clean_text(raw, 64))
    if not name:
        return "", ("Choose a username." if required else None)
    problem = username_problem(name)
    if problem:
        return name, problem
    if await username_taken(db, name, exclude_user_id=exclude_user_id):
        return name, "That username is taken - try adding a number or a dot."
    return name, None


def validate_timezone(raw: str | None, *, fallback: str | None = None) -> tuple[str, str | None]:
    tz = (raw or "").strip()[:64]
    if is_valid_timezone(tz):
        return tz, None
    if fallback is not None:
        return fallback, None
    return tz, "Choose a valid time zone from the list."


def password_errors(password: str, confirm: str | None, *, email: str | None = None, name: str | None = None,
                    field: str = "password", confirm_field: str = "password_confirm") -> dict[str, str]:
    errors: dict[str, str] = {}
    if not password:
        errors[field] = "Choose a password."
    else:
        problems = password_problems(password, email=email, name=name)
        if problems:
            errors[field] = " ".join(problems)
    if confirm is not None and password and confirm != password:
        errors[confirm_field] = "The two passwords don't match."
    return errors


def validate_id_fields(f: FormReader, prefix: str = "id") -> tuple[dict[str, str], dict[str, str]]:
    """Checks the identity/_fields.html inputs (type, issuing country, front image present)."""
    values = {
        f"{prefix}_doc_type": f.text(f"{prefix}_doc_type", 40),
        f"{prefix}_country": f.text(f"{prefix}_country", 4).upper(),
    }
    errors: dict[str, str] = {}
    if values[f"{prefix}_doc_type"] not in ID_DOC_TYPES:
        errors[f"{prefix}_doc_type"] = "Choose the type of ID document."
    if not re.fullmatch(r"[A-Z]{2}", values[f"{prefix}_country"]):
        errors[f"{prefix}_country"] = "Enter the 2-letter code of the country that issued the ID (e.g. IN, US, GB)."
    if f.upload(f"{prefix}_front") is None:
        errors[f"{prefix}_front"] = "Upload a photo of the front of your government ID card - it's mandatory."
    return values, errors


def safe_next(raw: str | None) -> str | None:
    """Same-site relative ``?next=`` targets only (no open redirects, no auth-page loops)."""
    if not raw:
        return None
    raw = raw.strip()
    if len(raw) > 1000 or any(ch in raw for ch in "\r\n\t") or not is_safe_redirect(raw):
        return None
    if raw.startswith(("/auth/login", "/auth/logout", "/auth/signup", "/auth/reset", "/auth/forgot")):
        return None
    return raw


# ------------------------------------------------------------------ one-time tokens
TOKEN_TTL: dict[TokenPurpose, timedelta] = {
    TokenPurpose.EMAIL_VERIFY: timedelta(hours=48),
    TokenPurpose.PASSWORD_RESET: timedelta(hours=1),
    TokenPurpose.INVITE: timedelta(days=7),
    TokenPurpose.STAFF_INVITE: timedelta(days=7),
}


@dataclass(slots=True)
class TokenLookup:
    state: str  # ok | missing | used | expired
    token: AuthToken | None = None

    @property
    def ok(self) -> bool:
        return self.state == "ok"


async def issue_token(
    db: AsyncSession,
    purpose: TokenPurpose,
    *,
    user_id: int | None = None,
    email: str | None = None,
    employee_id: int | None = None,
    ttl: timedelta | None = None,
    supersede: bool = True,
) -> str:
    """Create a one-time token (caller commits) and return the RAW value for the e-mailed link.

    Only ``sha256(raw)`` is stored. With ``supersede`` earlier unused tokens of the same purpose for the
    same user/employee stop working."""
    now = utcnow()
    if supersede and (user_id is not None or employee_id is not None):
        stmt = update(AuthToken).where(AuthToken.purpose == purpose, AuthToken.used_at.is_(None))
        stmt = stmt.where(AuthToken.user_id == user_id) if user_id is not None else stmt.where(AuthToken.employee_id == employee_id)
        await db.execute(stmt.values(used_at=now).execution_options(synchronize_session=False))
    raw = new_token(32)
    db.add(AuthToken(purpose=purpose, token_hash=token_hash(raw), user_id=user_id, email=email, employee_id=employee_id,
                     expires_at=now + (ttl or TOKEN_TTL[purpose])))
    await db.flush()
    return raw


async def lookup_token(db: AsyncSession, raw: str | None, purpose: TokenPurpose) -> TokenLookup:
    if not raw or not TOKEN_RE.fullmatch(raw):
        return TokenLookup("missing")
    tok = await db.scalar(select(AuthToken).where(AuthToken.token_hash == token_hash(raw), AuthToken.purpose == purpose))
    if tok is None:
        return TokenLookup("missing")
    if tok.used_at is not None:
        return TokenLookup("used", tok)
    if tok.expires_at <= utcnow():
        return TokenLookup("expired", tok)
    return TokenLookup("ok", tok)


async def recently_issued(db: AsyncSession, purpose: TokenPurpose, *, user_id: int, within_seconds: int = 60) -> bool:
    since = utcnow() - timedelta(seconds=within_seconds)
    found = await db.scalar(
        select(AuthToken.id).where(AuthToken.purpose == purpose, AuthToken.user_id == user_id, AuthToken.created_at > since).limit(1)
    )
    return found is not None


async def send_email_verification(db: AsyncSession, user: User, *, email: str | None = None) -> None:
    """Queue the 48-hour e-mail confirmation link (the in-app notification never contains the token)."""
    from app.services.notify import email_address

    target = email or user.email
    if not target:
        return
    raw = await issue_token(db, TokenPurpose.EMAIL_VERIFY, user_id=user.id, email=target)
    await email_address(
        db, target, "Confirm your e-mail address",
        f"Please confirm that {target} is your e-mail address so we can reach you about memberships, payments and "
        "safety alerts. The link is valid for 48 hours. If you didn't create an IronPulse account, ignore this e-mail.",
        link=f"/auth/verify-email?token={raw}", recipient=user.full_name,
    )


# ------------------------------------------------------------------ sessions
async def start_session(db: AsyncSession, request: Request, response: Response, user: User) -> str:
    """Start a signed-in session for ``user`` (the caller commits) and set the cookie; returns the raw token.

    * Rotation: whatever session this browser had is revoked (its other tabs share the cookie and simply carry on with
      the new one - a re-sign-in on the same browser is never "another device").
    * One session per account (SINGLE_SESSION_PER_ACCOUNT, default on): every OTHER session of the account ends - the
      newest sign-in wins. The account's sign-ins are serialised (``lock_account_sessions``, held until the caller's
      COMMIT), so two at the same moment leave exactly one live session: the later one. The ended devices' sockets
      close with 4409 and their AI work stops after COMMIT; their next request lands on the sign-in page with "signed
      out because your account signed in on another device (<browser>, <time>)". Recorded as a security event (no
      secrets: device labels and the count) and in the audit log (``auth.signed_out_elsewhere``)."""
    exclusive = single_session_enabled()
    if exclusive:
        await lock_account_sessions(db, user.id)
    old = get_session_token(request)
    if old:
        await revoke_session(db, old)
    now = utcnow()  # after the rotation's UPDATE: never equal to its revoked_at (that marker means "elsewhere")
    raw = await create_session(db, user, request, now=now)
    if exclusive:
        ended = await end_other_sessions(db, user.id, keep_hash=token_hash(raw), now=now)
        if ended:
            await _record_signed_out_elsewhere(db, request, user, ended)
    set_session_cookie(response, raw)
    return raw


async def _record_signed_out_elsewhere(db: AsyncSession, request: Request, user: User, ended: list[Any]) -> None:
    from app.services.audit import audit

    here = device_label(request.headers.get("user-agent"))
    others = sorted({e.label for e in ended})
    detail = (f"signed out elsewhere: account {user.id} signed in on {here}; ended {len(ended)} other "
              f"session{'s' if len(ended) != 1 else ''} ({', '.join(others)})")
    db.add(SecurityEvent(
        ip=client_ip(request), user_id=user.id, method=request.method[:10], path=request.url.path[:512],
        user_agent=(request.headers.get("user-agent") or "")[:300] or None, category=ThreatCategory.OTHER,
        severity=1, detail=detail[:500], action="logged",
    ))
    await audit(db, user, "auth.signed_out_elsewhere", "user", user.id, request=request,
                detail={"device": here, "ended": len(ended), "ended_devices": others[:5]})


# ------------------------------------------------------------------ sign-in lockout
async def count_failed_sign_in(db: AsyncSession, user: User, now: datetime) -> bool:
    """Count one wrong password for ``user`` (an active account) with ONE atomic ``UPDATE ... RETURNING``: guesses that
    arrive together - on any connection or worker - are all counted (a read-modify-write of the loaded row loses
    them). The ``login_max_failures``-th failure locks the account for ``login_lockout_minutes`` and the count starts
    again. ``user`` is updated to the stored values; the caller commits. True when this failure locked the account."""
    s = get_settings()
    until = literal(now + timedelta(minutes=s.login_lockout_minutes), DateTime(timezone=True))
    failures = func.coalesce(User.failed_login_count, 0) + 1
    reached = failures >= s.login_max_failures
    row = (await db.execute(
        # a guess verified while the account got locked (it read the account before the lock) neither counts towards
        # nor extends the lock - and the caller can tell the lock is on from ``user.locked_until``
        update(User).where(User.id == user.id, or_(User.locked_until.is_(None), User.locked_until <= now))
        .values(failed_login_count=case((reached, 0), else_=failures),
                locked_until=case((reached, until), else_=User.locked_until))
        .returning(User.failed_login_count, User.locked_until, (User.locked_until == until).label("locked_now"))
        .execution_options(synchronize_session=False)
    )).one_or_none()
    if row is None:
        await _refresh_lock(db, user)
        return False
    set_committed_value(user, "failed_login_count", row.failed_login_count)
    set_committed_value(user, "locked_until", row.locked_until)
    return bool(row.locked_now)


async def clear_failed_sign_ins(db: AsyncSession, user: User, now: datetime) -> bool:
    """A correct password: reset the failure count - only if the account is not locked at this moment (one conditional
    ``UPDATE ... RETURNING``; the caller commits). Several guesses read the account before Argon2 and are verified at the
    same time; when wrong ones among them lock the account, a right one verified after them must not sign in (it would
    turn the 5-guess lockout into "every guess already in flight"). False = locked meanwhile (``user.locked_until`` is
    refreshed): refuse the sign-in with the lock message."""
    row = (await db.execute(
        update(User).where(User.id == user.id, or_(User.locked_until.is_(None), User.locked_until <= now))
        .values(failed_login_count=0, locked_until=None)
        .returning(User.id)
        .execution_options(synchronize_session=False)
    )).first()
    if row is None:
        await _refresh_lock(db, user)
        return False
    set_committed_value(user, "failed_login_count", 0)
    set_committed_value(user, "locked_until", None)
    return True


async def _refresh_lock(db: AsyncSession, user: User) -> None:
    row = (await db.execute(select(User.failed_login_count, User.locked_until).where(User.id == user.id))).first()
    if row is not None:
        set_committed_value(user, "failed_login_count", row.failed_login_count)
        set_committed_value(user, "locked_until", row.locked_until)


def identifier_key(raw: str | None, *, default_country: str | None = "IN") -> str:
    """The ghost-throttle key of a sign-in identifier, normalised the way ``find_user_by_identifier`` resolves it: one
    counter per identifier whatever its spelling - e-mail case, '@alice' / 'alice' / 'ALICE', phone formats, member-ID
    forms - exactly like a real account's counter covers every spelling that reaches it (otherwise locking '@x' and then
    trying 'x' would tell a real account - locked - from an unknown one - not locked)."""
    from app.services.identity_lookup import PHONE_LIKE_RE, USERNAME_RE, normalize_username, parse_member_id

    value = (raw or "").strip()
    if "@" in value and not value.startswith("@"):
        return "email:" + value.lower()
    uid = parse_member_id(value)
    if uid is not None:
        return f"member:{uid}"
    if PHONE_LIKE_RE.match(value) and sum(ch.isdigit() for ch in value) >= 7:
        # a national number is read in the default country first (as it is stored), an international one as written
        for country in ((None,) if value.startswith(("+", "00")) else (default_country, None)):
            try:
                norm = normalize_phone(value, country)
            except ValueError:
                continue
            if norm:
                return "phone:" + norm
    name = normalize_username(value)
    if USERNAME_RE.match(name):
        return "username:" + name
    return "other:" + value.lower()


async def blocked_identity(db: AsyncSession, *, email: str | None = None, phone: str | None = None,
                           country_code: str | None = None, exclude_user_id: int | None = None) -> str | None:
    """The trust-and-safety registry (app/services/trust_safety.py ``registration_blocked``): a neutral refusal when the
    e-mail or phone belonged to a deleted or banned account - one person per e-mail and ID, forever. A phone is matched by
    its E.164 form: pass it in that form, or the national number plus ``country_code``. None when it is free."""
    from app.services import trust_safety

    return await trust_safety.registration_blocked(db, email=email or None, phone=phone or None,
                                                   country_code=country_code or None, exclude_user_id=exclude_user_id)


# KEYS[1]: one identifier's failures {c = count, f = first failure, l = locked until} (Redis clock, epoch seconds).
# ARGV: max failures, counting window (s), lock (s), '1' = count this failure / '0' = only look.
# -> {1, locked_until} locked (before or by this failure) | {0, failures so far}
_LOGIN_LUA = lua_script("login", """
local maxf, window, lock = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
local s = redis.call('HMGET', KEYS[1], 'c', 'f', 'l')
local locked = tonumber(s[3])
if locked and locked > now then return {1, fmt(locked)} end
if ARGV[4] ~= '1' then return {0, '0'} end
local c, f = tonumber(s[1]) or 0, tonumber(s[2]) or now
if now - f > window then c, f = 0, now end
c = c + 1
if c >= maxf then
  redis.call('DEL', KEYS[1])
  redis.call('HSET', KEYS[1], 'l', fmt(now + lock))
  redis.call('PEXPIRE', KEYS[1], math.ceil(lock * 1000) + 1000)
  return {1, fmt(now + lock)}
end
redis.call('HSET', KEYS[1], 'c', c, 'f', fmt(f))
redis.call('HDEL', KEYS[1], 'l')
redis.call('PEXPIRE', KEYS[1], math.ceil((f + window - now) * 1000) + 1000)
return {0, tostring(c)}
""")


class GhostThrottle:
    """Failed sign-in counters for identifiers WITHOUT an account.

    Mirrors ``failed_login_count`` / ``locked_until`` (``login_max_failures`` failures within ``login_lockout_minutes``
    lock the identifier for ``login_lockout_minutes``) so attackers can't tell real accounts apart by watching for the
    lockout message. Shared by every worker through Redis (app/security/ratelimit.py's store: one Lua script per
    attempt, key ``ip:<env>:<db>:sec:login:<sha256 of the identifier>`` expiring with the lock or the counting
    window), like the per-account counters in PostgreSQL; in this worker's memory - at most ``max_keys`` identifiers
    per map, least recently used dropped first - without Redis or while it fails."""

    def __init__(self, max_keys: int = 50_000, store: SharedStore | None = None) -> None:
        self._fails: OrderedDict[str, tuple[int, float]] = OrderedDict()
        self._locked: OrderedDict[str, float] = OrderedDict()
        self._max_keys = max_keys
        self._store = store

    @property
    def store(self) -> SharedStore:
        return self._store if self._store is not None else shared_store

    @staticmethod
    def _key(identifier: str) -> str:
        # normalised like find_user_by_identifier: '@ghost', 'ghost' and 'GHOST' share one counter, as a real account's
        return sha256_hex(identifier_key(identifier))[:32]

    async def locked_until(self, email: str) -> float | None:
        """The lock's expiry (epoch seconds) while ``email`` is locked, else None."""
        return await self._judge(email, count=False)

    async def fail(self, email: str) -> float | None:
        """Count a failed sign-in for ``email`` unless it is locked already; returns the lock's expiry (epoch seconds)
        when it is locked - before or by this failure - else None. One round trip."""
        return await self._judge(email, count=True)

    async def _judge(self, email: str, *, count: bool) -> float | None:
        s = get_settings()
        key = self._key(email)
        window = float(s.login_lockout_minutes * 60)
        reply = await self.store.run(_LOGIN_LUA, [shared_key("login", key)],
                                     [s.login_max_failures, window, window, "1" if count else "0"],
                                     lambda r: (int(r[0]), float(r[1])))
        if reply is not None:
            self._mirror(key, locked=bool(reply[0]), value=reply[1], counted=count)
            return reply[1] if reply[0] else None
        return self._judge_here(key, count, s.login_max_failures, window)

    def _mirror(self, key: str, *, locked: bool, value: float, counted: bool) -> None:
        """Keep this worker's memory in step with the shared answer, so a Redis outage continues from the shared state
        (as app/security/ratelimit.py's limiter does) - never a fresh count of zero per worker. Approximate: a mirrored
        count keeps its first local sighting as the window start (stricter, never looser)."""
        now = time.time()
        if locked:
            self._fails.pop(key, None)
            self._locked[key] = value
            self._locked.move_to_end(key)
            if len(self._locked) > self._max_keys:
                self._locked.popitem(last=False)
            return
        self._locked.pop(key, None)  # the shared store says it is not locked (any more)
        if not counted:
            return
        _, first = self._fails.get(key, (0, now))
        self._fails[key] = (int(value), first)
        self._fails.move_to_end(key)
        if len(self._fails) > self._max_keys:
            self._fails.popitem(last=False)

    def _judge_here(self, key: str, count: bool, max_failures: int, window: float) -> float | None:
        now = time.time()
        until = self._locked.get(key)
        if until is not None:
            if until > now:
                return until
            del self._locked[key]
        if not count:
            return None
        failures, first = self._fails.pop(key, (0, now))
        if now - first > window:
            failures, first = 0, now
        failures += 1
        if failures >= max_failures:
            until = now + window
            self._locked[key] = until
            if len(self._locked) > self._max_keys:
                self._locked.popitem(last=False)
            return until
        self._fails[key] = (failures, first)
        if len(self._fails) > self._max_keys:
            self._fails.popitem(last=False)
        return None

    def reset(self) -> None:
        self._fails.clear()
        self._locked.clear()


ghost_throttle = GhostThrottle()
shared_store.on_reset(ghost_throttle.reset)  # tests: cleared together with the shared keys


def lock_message(minutes_left: int) -> str:
    minutes_left = max(1, minutes_left)
    unit = "minute" if minutes_left == 1 else "minutes"
    return (f"Too many failed sign-in attempts. For your security, sign-in for this account is paused for "
            f"{minutes_left} {unit}. Try again later or reset your password.")
