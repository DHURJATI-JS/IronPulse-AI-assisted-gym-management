"""Profile pages for everyone (own profile + privacy-scoped profiles of others).

GET /profile            -> your own profile
GET /profile/{user_id}  -> someone's profile (owners & active public staff are public; customers are private:
                           visible to themselves, owners of their gyms, assigned trainers and the super-admin)
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.core.deps import DB, AuthUser, CurrentUser, NotFound
from app.core.templating import render
from app.services.profiles import WEEKDAYS, load_profile

router = APIRouter()


@router.get("/profile")
async def my_profile(user: AuthUser) -> RedirectResponse:
    return RedirectResponse(f"/profile/{user.id}", status_code=303)


@router.get("/profile/{user_id}")
async def view_profile(request: Request, user_id: int, db: DB, viewer: CurrentUser):
    data = await load_profile(db, viewer, user_id)
    if data is None:
        raise NotFound("Profile not found.")
    gear = None
    if data.show_health and data.conditions:
        from app.services.gear import recommend_gear

        gear = recommend_gear([c.name for c in data.conditions], age=data.user.age())
    return render(request, "profiles/view.html", {"p": data, "gear": gear, "weekdays": WEEKDAYS})
