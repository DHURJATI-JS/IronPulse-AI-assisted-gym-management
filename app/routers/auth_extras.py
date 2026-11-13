"""Account recovery extras: "Forgot username?" (e-mails the username + member ID to the account owner).

Always answers with the same message whether or not an account exists (no user enumeration);
rate limited by the auth bucket (path starts with /auth/forgot).
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request

from app.config import get_settings
from app.core.deps import DB, CurrentUser
from app.core.templating import redirect, render
from app.models.enums import NotificationCategory, Role, Severity
from app.services.identity_lookup import find_users_for_recovery, member_id
from app.services.notify import notify

router = APIRouter()

SAME_ANSWER = (
    "If an account matches that e-mail address or phone number, we've e-mailed its username and member ID. "
    "Check your inbox (and spam folder)."
)


@router.get("/auth/forgot-username")
async def forgot_username_form(request: Request, user: CurrentUser):
    if user is not None:
        return redirect("/profile")
    return render(request, "auth_extras/forgot_username.html", {"sent": False, "error": None, "contact": ""})


@router.post("/auth/forgot-username")
async def forgot_username_submit(request: Request, db: DB, contact: str = Form("", max_length=254)):
    contact = contact.strip()
    if len(contact) < 5:
        return render(request, "auth_extras/forgot_username.html",
                      {"sent": False, "error": "Enter the e-mail address or phone number on your account.",
                       "contact": contact}, status_code=400)
    settings = get_settings()
    for user in await find_users_for_recovery(db, contact):
        if not user.email or user.role == Role.SUPERADMIN:  # platform admins sign in at /superadmin/login only
            continue
        handle = f"@{user.username}" if user.username else "(not set yet)"
        await notify(
            db, user, "Your sign-in details",
            f"You (or someone using your e-mail/phone) asked for your {settings.app_name} sign-in details.\n\n"
            f"Username: {handle}\nMember ID: {member_id(user)}\nE-mail: {user.email}\n\n"
            "You can sign in with any of these plus your password. Forgot the password too? Use 'Forgot password' "
            "on the sign-in page. If this wasn't you, you can ignore this e-mail.",
            category=NotificationCategory.SECURITY, severity=Severity.INFO, link="/auth/login",
        )
    await db.commit()
    return render(request, "auth_extras/forgot_username.html", {"sent": True, "error": None, "contact": ""})
