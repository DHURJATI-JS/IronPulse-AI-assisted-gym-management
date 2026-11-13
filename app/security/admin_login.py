"""Platform-admin sign-in hardening: the address allow-list, failed attempts per address and the IP ban they lead to.

Rules (README "Platform admin")
* Super-admin accounts sign in ONLY at /superadmin/login (app/routers/superadmin_login.py). Every other sign-in path
  (/auth/login with an e-mail, username, phone or member ID, the account chooser's references, invitations, password
  resets) treats a super-admin's identifier exactly like one that does not exist - same neutral error, same Argon2 work,
  same lockout behaviour - and never points to the admin page (app/routers/auth.py).
* SUPERADMIN_ALLOWED_IPS (addresses / CIDR ranges, empty = any address): outside the list the admin sign-in page is a
  404 and the /superadmin console refuses (app/core/deps.py). The client address is ``app.security.net.client_ip``:
  X-Forwarded-For counts only from TRUSTED_PROXIES - never what a client typed.
* Failed attempts per client address within a rolling window of SUPERADMIN_LOGIN_WINDOW_MINUTES - a wrong password or an
  unknown e-mail on /superadmin/login, a non-admin account trying it, and ANY attempt with a super-admin's identifier on
  the normal sign-in paths - are counted by every worker together: one Lua script on the shared store of
  app/security/ratelimit.py (key ``ip:<env>:<db>:sec:{<address>}:salogin``, a sliding window of attempt times), this
  worker's bounded memory without Redis or while it fails. The SUPERADMIN_LOGIN_MAX_FAILURES-th failure bans the address
  through the ordinary ``ip_bans`` table for SUPERADMIN_LOGIN_BAN_HOURS - a repeat offender (another ban of the address
  within 30 days, no member's address) follows the existing escalation of app/security/bans.py ``ban_duration``:
  permanent. The ban is written with a security event (BRUTEFORCE) and an audit row, the super-admins are notified,
  this worker's gate refuses the address as soon as it commits, the other workers at their next ban sync (and the admin
  sign-in page checks the database itself first, so it refuses at once on every worker). Then the window starts over.
* SAFETY VALVE: the server machine itself (loopback 127.0.0.1 / ::1) is never banned by this rule - counted, logged and
  audited only - and neither is an address of RATE_LIMIT_WHITELIST or TRUSTED_PROXIES (a proxy's address is everyone's).
  ``python scripts/manage.py unban <ip>`` lifts a ban from the machine's terminal (``unban``).
* A successful admin sign-in clears the address's count. Every attempt is audited - the identifier masked, never the
  password.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, text

from app.config import get_settings
from app.core.utils import utcnow
from app.security.ratelimit import SharedStore, address_bucket, address_key, lua_script, shared_store

log = logging.getLogger("ironpulse.security")

WINDOW_CAP = 64  # attempt times remembered per address (more than any SUPERADMIN_LOGIN_MAX_FAILURES allows)
BAN_SEVERITY = 4  # brute force against the platform admin
BAN_CREATED_BY = "admin_login"  # ip_bans.created_by of these bans
AUDIT_FAILED = "superadmin.login_failed"
AUDIT_REFUSED = "superadmin.login_refused"  # a super-admin identifier on a normal sign-in path
AUDIT_OK = "superadmin.login"
AUDIT_BAN = "security.ban_admin_login"
AUDIT_BAN_SKIPPED = "security.ban_admin_login_skipped"

# KEYS[1]: the address's attempt times. ARGV: window (s), cap, clear_at, '1' = count this attempt / '0' = only look.
# -> attempts within the window (this one included); at clear_at the window starts over
_FAIL_LUA = lua_script("salogin", """
local window, cap, clear_at = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
if ARGV[4] == '1' then return window_hit(KEYS[1], window, cap, clear_at) end
local recent = 0
for _, ts in ipairs(redis.call('LRANGE', KEYS[1], 0, -1)) do
  if now - tonumber(ts) < window then recent = recent + 1 end
end
return recent
""")


# ====================================================================== who may open the page
def _network_list(entries: list[str]) -> list[Any]:
    out = []
    for entry in entries:
        try:
            out.append(ipaddress.ip_network(entry.strip(), strict=False))
        except ValueError:
            log.warning("SUPERADMIN_ALLOWED_IPS: %r is not an address or CIDR range - ignored", entry[:60])
    return out


def address_allowed(ip: str) -> bool:
    """SUPERADMIN_ALLOWED_IPS: True when the list is empty or holds ``ip`` (an address or a CIDR range of it)."""
    entries = list(get_settings().superadmin_allowed_ips or [])
    if not entries:
        return True
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return False
    if getattr(addr, "ipv4_mapped", None) is not None:
        addr = addr.ipv4_mapped  # type: ignore[union-attr]
    return any(addr.version == net.version and addr in net for net in _network_list(entries))


def is_loopback(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    return bool((mapped or addr).is_loopback)


def never_banned(ip: str) -> str | None:
    """Why this rule never bans ``ip`` (loopback = the server machine itself, whitelisted, a trusted proxy) - else None."""
    from app.security.net import is_trusted_proxy

    if is_loopback(ip):
        return "loopback"
    if ip in (get_settings().rate_limit_whitelist or []):
        return "whitelisted"
    if is_trusted_proxy(ip):
        return "trusted_proxy"
    return None


def mask_identifier(raw: str | None) -> str:
    """What the audit keeps of a typed identifier: enough to recognise it, never the whole value (people type
    passwords into the wrong box)."""
    value = " ".join(str(raw or "").split())[:254]
    if not value:
        return ""
    local, at, domain = value.partition("@")
    if at and local and domain:
        return f"{local[:2]}***@{domain[:60]}"
    return f"{value[:2]}***" if len(value) > 2 else "***"


# ====================================================================== the shared failure counter
class AdminLoginFailures:
    """Failed platform-admin sign-ins per address (a rolling window), shared by every worker through Redis - else, and
    while Redis fails, counted in this worker's bounded memory (as app/services/auth_flows.py's ghost throttle)."""

    def __init__(self, max_keys: int = 50_000, store: SharedStore | None = None) -> None:
        self._local: OrderedDict[str, deque[float]] = OrderedDict()
        self._max_keys = max_keys
        self._store = store

    @property
    def store(self) -> SharedStore:
        return self._store if self._store is not None else shared_store

    @staticmethod
    def key(ip: str) -> str:
        return address_bucket(ip)

    @staticmethod
    def _window() -> float:
        return float(get_settings().superadmin_login_window_minutes * 60)

    async def _judge(self, ip: str, *, count: bool) -> int:
        k = self.key(ip)
        window = self._window()
        limit = int(get_settings().superadmin_login_max_failures)
        reply = await self.store.run(_FAIL_LUA, [address_key(k, "salogin")], [window, WINDOW_CAP, limit,
                                                                              "1" if count else "0"], int)
        if reply is not None:
            if count:
                self._mirror(k, int(reply), window, limit)
            return int(reply)
        return self._here(k, count, window, limit)

    def _here(self, k: str, count: bool, window: float, limit: int) -> int:
        now = time.monotonic()
        times = self._local.get(k)
        if times is None:
            if not count:
                return 0
            times = self._local[k] = deque(maxlen=WINDOW_CAP)
            if len(self._local) > self._max_keys:
                self._local.popitem(last=False)
        else:
            self._local.move_to_end(k)
        if count:
            times.append(now)
        recent = sum(1 for t in times if now - t < window)
        if count and recent >= limit:
            self._local.pop(k, None)  # the window starts over (as the shared script's clear_at)
        return recent

    def _mirror(self, k: str, recent: int, window: float, limit: int) -> None:
        """Keep this worker's memory close to the shared count, so a Redis outage continues from it."""
        if recent >= limit or recent <= 0:
            self._local.pop(k, None)
            return
        now = time.monotonic()
        self._local[k] = deque([now] * min(recent, WINDOW_CAP), maxlen=WINDOW_CAP)
        self._local.move_to_end(k)
        if len(self._local) > self._max_keys:
            self._local.popitem(last=False)

    async def fail(self, ip: str) -> int:
        """Count one failed attempt from ``ip``; returns the attempts within the window (this one included). At
        SUPERADMIN_LOGIN_MAX_FAILURES the window starts over (the caller bans)."""
        return await self._judge(ip, count=True)

    async def count(self, ip: str) -> int:
        return await self._judge(ip, count=False)

    async def reset(self, ip: str) -> None:
        """A successful admin sign-in (or an unban): the address starts from zero - here and on every worker."""
        k = self.key(ip)
        self._local.pop(k, None)
        await self.store.forget(address_key(k, "salogin"))

    def clear(self) -> None:
        self._local.clear()


failures = AdminLoginFailures()
shared_store.on_reset(failures.clear)  # tests: cleared together with the shared keys


# ====================================================================== bans
@dataclass(slots=True)
class FailureOutcome:
    failures: int  # attempts within the window, this one included (0 after the window restarted at a ban)
    limit: int
    banned: bool = False  # the address is banned now (by this failure or before)
    banned_now: bool = False  # this failure placed the ban
    until: datetime | None = None  # None with banned = permanent
    exempt: str | None = None  # why the address was not banned (loopback / whitelisted / trusted_proxy)


async def active_ban(db: Any, ip: str) -> tuple[bool, datetime | None]:
    """(banned?, until - None = permanent) of ``ip`` or its /64 in the DATABASE - the source of truth every worker
    shares - and the ban is put into this worker's registry at once (its gate refuses the address from now on)."""
    from app.models import IpBan
    from app.security.bans import BanInfo, ban_registry

    now = utcnow()
    keys = sorted({ip, address_bucket(ip)})
    rows = (await db.execute(
        select(IpBan.ip, IpBan.banned_until, IpBan.severity).where(
            IpBan.ip.in_(keys), IpBan.lifted_at.is_(None), or_(IpBan.banned_until.is_(None), IpBan.banned_until > now))
    )).all()
    if not rows:
        return False, None
    permanent = [r for r in rows if r.banned_until is None]
    best = permanent[0] if permanent else max(rows, key=lambda r: r.banned_until)
    ban_registry.put(best.ip, BanInfo(until=best.banned_until, reason="", severity=best.severity))
    return True, best.banned_until


async def _place_ban(db: Any, request: Any, ip: str, attempts: int) -> tuple[datetime | None, bool]:
    """Ban ``ip`` (its /64 for IPv6) after ``attempts`` failed admin sign-ins: (until, placed now?). An active ban of
    the address is kept as it is (never a second row - that would count as a repeat offence). Serialised with the
    security flush's ban decisions (the same advisory lock), the caller commits."""
    from app.models import IpBan, SecurityEvent, Subscription, User
    from app.models.enums import Severity, SubscriptionStatus, ThreatCategory
    from app.security.bans import BAN_FLUSH_LOCK_KEY, BanInfo, ban_duration, ban_registry, storable
    from app.services.audit import audit
    from app.services.realtime import call_after_commit
    from app.services.trust_safety import notify_superadmins

    s = get_settings()
    key = address_bucket(ip)
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": BAN_FLUSH_LOCK_KEY})
    banned, until = await active_ban(db, ip)
    if banned:
        return until, False
    now = utcnow()
    offenses = int(await db.scalar(select(func.count(IpBan.id)).where(
        IpBan.ip == key, IpBan.created_at > now - timedelta(days=30))) or 0) + 1
    member_ip = await db.scalar(
        select(User.id).join(Subscription, Subscription.customer_id == User.id)
        .where(User.last_login_ip == ip, Subscription.status == SubscriptionStatus.ACTIVE).limit(1))
    permanent = ban_duration(BAN_SEVERITY, offense_count=offenses, has_subscription=member_ip is not None) is None
    until = None if permanent else now + timedelta(hours=s.superadmin_login_ban_hours)
    window = s.superadmin_login_window_minutes
    reason = storable(f"{attempts} failed platform-admin sign-ins within {window} minutes", 300)
    ban = IpBan(ip=key, reason=reason, severity=BAN_SEVERITY, banned_until=until, created_by=BAN_CREATED_BY,
                offense_count=offenses, had_subscription=member_ip is not None)
    db.add(ban)
    await db.flush()
    info = BanInfo(until=until, reason=reason, severity=BAN_SEVERITY)
    call_after_commit(db, lambda k=key, i=info: ban_registry.put(k, i))
    agent = request.headers.get("user-agent") if request is not None else None
    db.add(SecurityEvent(ip=ip, method=(request.method if request is not None else "POST")[:10],
                         path=(request.url.path if request is not None else "/superadmin/login")[:512],
                         user_agent=storable(agent, 300) or None, category=ThreatCategory.BRUTEFORCE,
                         severity=BAN_SEVERITY, detail=storable(f"{reason}: address banned", 500), action="banned"))
    when = "permanently (repeat offence)" if until is None else f"until {until:%d %b %Y %H:%M} UTC"
    await audit(db, None, AUDIT_BAN, "ip_ban", ban.id, request=request, detail={
        "ip": key, "until": until.isoformat() if until else "permanent", "failures": attempts,
        "offense_count": offenses, "by": BAN_CREATED_BY})
    await notify_superadmins(db, f"Address banned after failed admin sign-ins: {key}",
                             f"{attempts} failed platform-admin sign-in attempts came from {key} within {window} minutes, "
                             f"so the address is banned {when}. Lift the ban in the security centre if it was you - or "
                             "from the server's terminal: python scripts/manage.py unban " + key,
                             link=f"/superadmin/security/ip/{key}", severity=Severity.DANGER)
    log.warning("platform-admin sign-in: %s banned %s after %s failed attempts", key, when, attempts)
    return until, True


async def note_failure(db: Any, request: Any, *, reason: str, identifier: str = "", user_id: int | None = None,
                       path: str | None = None) -> FailureOutcome:
    """Count, audit and - at SUPERADMIN_LOGIN_MAX_FAILURES - ban: one refused platform-admin sign-in (``reason``:
    unknown | wrong_password | not_admin | locked | disabled | admin_on_member_signin ...). COMMITS (the caller's read
    transaction already ended before Argon2; nothing else is pending)."""
    from app.security.net import client_ip
    from app.services.audit import audit

    s = get_settings()
    ip = client_ip(request)
    limit = int(s.superadmin_login_max_failures)
    count = await failures.fail(ip)
    outcome = FailureOutcome(failures=count, limit=limit)
    path = path or (request.url.path if request is not None else "")
    action = AUDIT_REFUSED if reason == "admin_on_member_signin" else AUDIT_FAILED
    await audit(db, None, action, "user", user_id, request=request, detail={
        "reason": reason, "identifier": mask_identifier(identifier), "path": path[:120], "failures": count,
        "limit": limit})
    if count >= limit:
        exempt = never_banned(ip)
        if exempt is not None:
            outcome.exempt = exempt
            await audit(db, None, AUDIT_BAN_SKIPPED, "ip_ban", None, request=request, detail={
                "ip": ip, "failures": count, "why": exempt})
            log.warning("platform-admin sign-in: %s failed attempts from %s - not banned (%s)", count, ip, exempt)
        else:
            outcome.until, outcome.banned_now = await _place_ban(db, request, ip, count)
            outcome.banned = True
    await db.commit()
    return outcome


async def note_success(db: Any, request: Any, user: Any) -> None:
    """A successful admin sign-in: the address's count starts over; audited (the caller commits)."""
    from app.security.net import client_ip
    from app.services.audit import audit

    await failures.reset(client_ip(request))
    await audit(db, user, AUDIT_OK, "user", user.id, request=request, detail={"path": "/superadmin/login"})


async def unban(db: Any, ip: str, *, note: str = "lifted from the server's terminal") -> list[Any]:
    """Lift every active ban of ``ip`` and of its /64 (the CLI's ``manage.py unban``; the caller commits) and clear
    the address's failed admin sign-ins, threat score and ban claim in the shared store. Returns the lifted bans."""
    from app.services.security_triage import lift_ip_bans

    lifted: list[Any] = []
    for key in dict.fromkeys([ip, address_bucket(ip)]):
        lifted += await lift_ip_bans(db, key, actor=None, note=note, source="cli")
    for key in dict.fromkeys([ip, address_bucket(ip)]):
        await shared_store.forget(address_key(key, "salogin"), address_key(key, "threat"), address_key(key, "ban"))
    failures._local.pop(failures.key(ip), None)  # noqa: SLF001 - this process's twin
    return lifted


__all__ = [
    "AUDIT_BAN", "AUDIT_FAILED", "AUDIT_OK", "AUDIT_REFUSED", "AdminLoginFailures", "FailureOutcome", "active_ban",
    "address_allowed", "failures", "is_loopback", "mask_identifier", "never_banned", "note_failure", "note_success",
    "unban",
]
