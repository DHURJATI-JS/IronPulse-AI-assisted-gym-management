"""Jinja2 templating, the ``render`` helper, flash messages and e-mail rendering.

Every page handler returns ``render(request, "folder/page.html", {...})`` (or ``redirect(...)``).
The base context always contains: ``user``, ``csrf_token``, ``csp_nonce``, ``flashes``, ``nav``,
``ai_status``, ``hiring`` (open job count) and ``app`` (public settings).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeSerializer
from jinja2 import pass_context
from markupsafe import Markup
from starlette.templating import Jinja2Templates

from app.config import APP_DIR, get_settings
from app.core.utils import initials, mask_middle, money, to_local, utcnow

templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
env = templates.env
env.trim_blocks = True
env.lstrip_blocks = True

FLASH_COOKIE = "ip_flash"

STATUS_TONES = {
    "green": {"verified", "active", "paid", "success", "sent", "accepted", "done", "online", "up", "approved", "hired", "low", "logged"},
    "amber": {"pending", "queued", "needs_review", "ai_unavailable", "due", "awaiting_proof", "proof_review", "pending_otp",
              "pending_payment", "warning", "moderate", "degraded", "invited", "pending_verification", "unknown", "new", "reviewed"},
    "red": {"rejected", "failed", "suspended", "missing", "critical", "life_threatening", "down", "offline", "high", "banned", "danger"},
    "blue": {"processing", "open", "info", "contacted", "running", "draft_ready"},
    "zinc": {"draft", "cancelled", "expired", "terminated", "declined", "closed", "none", "void", "not_configured"},
}


def status_tone(value: Any) -> str:
    v = str(getattr(value, "value", value) or "").lower()
    for tone, values in STATUS_TONES.items():
        if v in values:
            return tone
    return "zinc"


def humanize(value: Any) -> str:
    v = str(getattr(value, "value", value) or "")
    return v.replace("_", " ").strip().capitalize()


# ------------------------------------------------------------------ static assets (cache busting)
def static_url(path: str) -> str:
    """``/static/<path>?v=<content version>``: served with a year-long immutable Cache-Control (app/core/static.py),
    so the version follows the file's bytes - a rebuilt asset gets a new URL at once (no restart needed)."""
    from app.core.static import content_version

    path = path.lstrip("/")
    try:
        ver = content_version(APP_DIR / "static" / path)
    except OSError:
        ver = "0"
    return f"/static/{path}?v={ver}"


def media_url(key: str | None, fallback: str | None = None) -> str | None:
    if key and key.startswith("public/"):
        return f"/media/{key}"
    return fallback


# ------------------------------------------------------------------ filters
@pass_context
def _dt(ctx: Any, value: datetime | None, fmt: str = "%d %b %Y, %H:%M") -> str:
    if value is None:
        return "—"
    user = ctx.get("user")
    tz = getattr(user, "timezone", None) or "UTC"
    local = to_local(value, tz)
    return local.strftime(fmt) if local else "—"


def _date(value: date | datetime | None, fmt: str = "%d %b %Y") -> str:
    if value is None:
        return "—"
    return value.strftime(fmt)


def _timeago(value: datetime | None) -> str:
    if value is None:
        return "—"
    delta = utcnow() - value
    s = int(delta.total_seconds())
    if s < 0:
        s = -s
        prefix, suffix = "in ", ""
    else:
        prefix, suffix = "", " ago"
    for unit, size in (("year", 31536000), ("month", 2592000), ("week", 604800), ("day", 86400), ("hour", 3600), ("minute", 60)):
        if s >= size:
            n = s // size
            return f"{prefix}{n} {unit}{'s' if n != 1 else ''}{suffix}"
    return "just now"


def _tojson_attr(value: Any) -> Markup:
    """JSON for data-* attributes (HTML-escaped)."""
    from markupsafe import escape

    return escape(json.dumps(value, default=str, separators=(",", ":")))


env.filters.update(
    money=money,
    dt=_dt,
    date=_date,
    timeago=_timeago,
    initials=initials,
    tone=status_tone,
    humanize=humanize,
    mask=mask_middle,
    media=media_url,
    jsonattr=_tojson_attr,
)
from app.core.icons import icon  # noqa: E402

env.globals.update(static=static_url, media_url=media_url, status_tone=status_tone, humanize=humanize, icon=icon)


# ------------------------------------------------------------------ flash messages
def _flash_serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().secret_key, salt="ironpulse-flash")


def _read_flashes(request: Request) -> list[dict[str, str]]:
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return []
    try:
        data = _flash_serializer().loads(raw)
        return [d for d in data if isinstance(d, dict)][:5] if isinstance(data, list) else []
    except BadSignature:
        return []


def add_flash(response: Any, message: str, category: str = "success", request: Request | None = None) -> None:
    existing = _read_flashes(request) if request is not None else []
    existing.append({"m": message[:300], "c": category})
    response.set_cookie(
        FLASH_COOKIE, _flash_serializer().dumps(existing[-5:]), max_age=120, httponly=True,
        secure=get_settings().cookie_secure, samesite="lax", path="/",
    )


def redirect(url: str, message: str | None = None, category: str = "success", status_code: int = 303) -> RedirectResponse:
    response = RedirectResponse(url, status_code=status_code)
    if message:
        add_flash(response, message, category)
    return response


# ------------------------------------------------------------------ render
def base_context(request: Request) -> dict[str, Any]:
    from app.ai.client import ai
    from app.core.nav import nav_for, nav_state
    from app.security.csrf import get_csrf_token

    settings = get_settings()
    user = getattr(request.state, "user", None)
    return {
        "request": request,
        "user": user,
        "csrf_token": get_csrf_token(request),
        "csp_nonce": request.scope.get("state", {}).get("csp_nonce", ""),
        "flashes": _read_flashes(request),
        "nav": nav_for(user),
        "hiring": nav_state.open_jobs,
        "ai_status": ai.status(),
        "app": {
            "name": settings.app_name,
            "environment": settings.environment,
            "payments_test_mode": settings.payments_test_mode,
            "geoapify": settings.geoapify_enabled,
            "max_upload_mb": settings.max_upload_mb,
            "offline_max_distance_km": settings.offline_max_distance_km,
        },
        "now": utcnow(),
    }


def render(request: Request, template: str, context: dict[str, Any] | None = None, *, status_code: int = 200) -> HTMLResponse:
    ctx = base_context(request)
    if context:
        ctx.update(context)
    response = templates.TemplateResponse(request, template, ctx, status_code=status_code)
    if ctx["flashes"]:
        response.delete_cookie(FLASH_COOKIE, path="/")
    return response


def render_email(*, title: str, body: str, link: str | None, recipient: str | None, guardian_copy: bool) -> tuple[str, str]:
    settings = get_settings()
    greeting = f"Hi {recipient.split()[0]}," if recipient else "Hello,"
    text = f"{greeting}\n\n{title}\n\n{body}\n"
    if link:
        text += f"\nOpen: {link}\n"
    if guardian_copy:
        text += "\n(A copy of this message was sent to the registered guardian.)\n"
    text += f"\n— {settings.app_name}\nThis is an automated message from a student project. See the Terms for the disclaimer.\n"
    html = env.get_template("emails/notification.html").render(
        title=title, body=body, link=link, greeting=greeting, guardian_copy=guardian_copy, app_name=settings.app_name,
    )
    return text, html
