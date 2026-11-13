"""WP-A1 public site: landing page, gym directory, brand + branch pages (published rules, 30 km gating),
plans explainer, jobs board + enquiries, legal pages, CSP hygiene and N+1 guards."""

from __future__ import annotations

import io
import re
import time
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.ai.client import AIUnavailableError, ai
from app.core.utils import utcnow
from app.db import QueryCounter
from app.models import BranchFeature, EmailOutbox, Gym, JobEnquiry, JobPosting, Notification
from app.models.enums import EmploymentStatus, EmploymentType, EmployeeRole, NotificationCategory
from app.routers.public import ENQUIRY_TOKEN_FIELD, enquiry_form_token
from tests.conftest import login_as, post_form
from tests.factories import (
    INDIRANAGAR,
    KORAMANGALA,
    MYSURU,
    WHITEFIELD,
    make_branch,
    make_customer,
    make_employee,
    make_gym,
    make_owner,
    make_plans,
    make_shift,
    make_user,
    png_bytes,
)


# ------------------------------------------------------------------ helpers
async def published_gym(db, *, name=None, coords=INDIRANAGAR, city="Bengaluru", online=False, owner=None, plans=True,
                        branch_name=None):
    owner = owner or await make_owner(db)
    gym = await make_gym(db, owner, name=name, online=online, offline=True)
    branch = await make_branch(db, gym, coords=coords, city=city, name=branch_name)
    if plans:
        await make_plans(db, gym)
    return owner, gym, branch


async def make_job(db, gym, *, title="Strength Coach", is_open=True, location="Bengaluru", salary=("25000", "35000"),
                   role=EmployeeRole.TRAINER, etype=EmploymentType.FULL_TIME, branch=None, requirements=None):
    job = JobPosting(
        gym_id=gym.id, branch_id=branch.id if branch else None, title=title, role=role, employment_type=etype,
        location=location, salary_min=Decimal(salary[0]) if salary else None,
        salary_max=Decimal(salary[1]) if salary else None, currency=gym.currency,
        requirements=requirements or "- 2+ years of coaching experience\n- Recognised trainer certification",
        description="Coach members on the gym floor and run small-group sessions.", is_open=is_open,
    )
    db.add(job)
    await db.commit()
    return job


def filled_in(job, data: dict) -> dict:
    """An enquiry form as a person sends it: with the token its page issued, a minute earlier
    (tests/test_job_enquiry_spam.py covers forms sent without one, or too soon)."""
    return {**data, ENQUIRY_TOKEN_FIELD: enquiry_form_token(job.id, issued=time.time() - 60)}


def pdf_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (80, 80), "white").save(buf, "PDF")
    return buf.getvalue()


def cards_in(html: str) -> int:
    return html.count('class="pub-gym-card group"')


async def trip_ai_offline() -> None:
    for _ in range(4):
        try:
            await ai.complete(feature="test", system="s", prompt="p")
        except AIUnavailableError:
            pass


# ------------------------------------------------------------------ pure helpers
def test_helpers():
    from app.routers.public import compress_weekdays, km_label, like_pattern, url_with

    assert compress_weekdays([0, 1, 2, 4]) == "Mon–Wed, Fri"
    assert compress_weekdays(range(7)) == "Every day"
    assert compress_weekdays([5, 6]) == "Sat, Sun"
    assert km_label(0.42) == "420 m" and km_label(3.456) == "3.5 km" and km_label(28.7) == "29 km" and km_label(None) is None
    assert url_with("/gyms", {"q": "a b", "city": "", "mode": None}, page=2) == "/gyms?q=a+b&page=2"
    assert url_with("/gyms", {"q": ""}, page=1) == "/gyms"
    assert like_pattern("50%_off\\") == "%50\\%\\_off\\\\%"


def test_plan_catalogue_has_12_types():
    from app.routers.public import PLAN_TYPES

    codes = [p["code"] for p in PLAN_TYPES]
    assert len(codes) == 12 and len(set(codes)) == 12
    assert {"offline", "offline_trainer_ai", "online_ai", "combo_trainer"} <= set(codes)


# ------------------------------------------------------------------ home
async def test_home_renders_featured_gyms_and_live_ai_chip(client, db, fake_ai):
    fake_ai(responses=["{}"])
    _, gym, _ = await published_gym(db, name="Featured Iron Club")
    trainer = await make_employee(db, gym)
    r = await client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Featured Iron Club" in html
    assert "AI online · Fake (gateway)" in html and 'data-component="ai-live"' in html
    assert trainer.full_name in html  # trainer teaser
    assert "Three ways to train" in html and "How it works" in html


async def test_home_ai_unavailable_and_empty_state(client, ai_down):
    await trip_ai_offline()
    r = await client.get("/")
    assert r.status_code == 200
    assert "AI unavailable" in r.text
    assert "Gyms are getting verified" in r.text  # polished empty state, no featured gyms yet


# ------------------------------------------------------------------ directory: published rules
async def test_directory_lists_only_published_or_open_online_gyms(client, db):
    from app.services.accounts import soft_delete_user

    await published_gym(db, name="Alpha Published")
    o = await make_owner(db)
    g = await make_gym(db, o, name="Bravo Unverified", online=False)
    await make_branch(db, g, verified=False)
    g = await make_gym(db, o, name="Charlie Closed", online=False)
    await make_branch(db, g, is_open=False)
    g = await make_gym(db, o, name="Delta TwoPhotos", online=False)
    await make_branch(db, g, photos=2)
    g = await make_gym(db, o, name="Echo OfflineOff", online=False, offline=False)
    await make_branch(db, g)
    await make_gym(db, o, name="Foxtrot Online", online=True, offline=False)
    g = await make_gym(db, o, name="Golf OnlineClosed", online=True, offline=False)
    g.online_open = False
    await db.commit()
    _, g, _ = await published_gym(db, name="Hotel Inactive")
    g.is_active = False
    await db.commit()
    gone_owner = await make_owner(db)
    await published_gym(db, name="India DeletedOwner", owner=gone_owner)
    await soft_delete_user(db, gone_owner, reason="test")
    await db.commit()

    r = await client.get("/gyms")
    assert r.status_code == 200
    html = r.text
    assert "Alpha Published" in html and "Foxtrot Online" in html
    for hidden in ("Bravo Unverified", "Charlie Closed", "Delta TwoPhotos", "Echo OfflineOff", "Golf OnlineClosed",
                   "Hotel Inactive", "India DeletedOwner"):
        assert hidden not in html, hidden
    assert cards_in(html) == 2
    assert "1 verified branch" in html  # badge on the published gym

    r = await client.get("/gyms?mode=online")
    assert "Foxtrot Online" in r.text and "Alpha Published" not in r.text
    r = await client.get("/gyms?mode=offline")
    assert "Alpha Published" in r.text and "Foxtrot Online" not in r.text
    r = await client.get("/gyms?mode=bogus")
    assert r.status_code == 200 and cards_in(r.text) == 2


async def test_directory_search_and_city_filter(client, db):
    await published_gym(db, name="Iron Temple", city="Bengaluru")
    await published_gym(db, name="Core Studio", city="Mysuru", coords=MYSURU)
    o = await make_owner(db)
    g = await make_gym(db, o, name="Hidden Draft", online=False)
    await make_branch(db, g, verified=False, city="Chennai")

    r = await client.get("/gyms?q=  temple  ")
    assert "Iron Temple" in r.text and "Core Studio" not in r.text
    r = await client.get("/gyms?q=mysuru")  # matches a published branch's city
    assert "Core Studio" in r.text and "Iron Temple" not in r.text
    r = await client.get("/gyms?q=chennai")  # unpublished branch cities never match
    assert cards_in(r.text) == 0 and "No gyms match your search" in r.text
    r = await client.get("/gyms?city=mysuru")
    assert "Core Studio" in r.text and "Iron Temple" not in r.text
    r = await client.get("/gyms?q=%25")  # literal "%" is escaped, not a wildcard
    assert r.status_code == 200 and cards_in(r.text) == 0
    r = await client.get("/gyms?q=" + "x" * 300)
    assert r.status_code == 200
    r = await client.get("/gyms")
    assert '<option value="Bengaluru"' in r.text and '<option value="Mysuru"' in r.text
    assert "Chennai" not in r.text  # city list only from published branches


async def test_directory_distance_sort_with_query_location(client, db):
    await published_gym(db, name="Zeta Whitefield", coords=WHITEFIELD)
    await published_gym(db, name="Alpha Mysuru", coords=MYSURU, city="Mysuru")
    o = await make_owner(db)
    await make_gym(db, o, name="Beta OnlineOnly", online=True, offline=False)

    r = await client.get(f"/gyms?lat={WHITEFIELD[0]}&lon={WHITEFIELD[1]}")
    html = r.text
    pos = [html.index(n) for n in ("Zeta Whitefield", "Alpha Mysuru", "Beta OnlineOnly")]
    assert pos == sorted(pos), "nearest first, online-only (no distance) last"
    assert "Nearest first" in html and "10 m" in html  # distance chip for the branch right here
    assert f"lat={WHITEFIELD[0]}" in html  # location carried into gym links

    r = await client.get("/gyms")  # without a location: A-Z
    html = r.text
    assert html.index("Alpha Mysuru") < html.index("Beta OnlineOnly") < html.index("Zeta Whitefield")

    r = await client.get("/gyms?lat=999&lon=abc")  # invalid coordinates are ignored
    assert r.status_code == 200 and "Sorted A–Z" in r.text


async def test_directory_uses_saved_user_location(client, db):
    await published_gym(db, name="Near Mysuru Gym", coords=MYSURU, city="Mysuru")
    await published_gym(db, name="Far Bengaluru Gym", coords=INDIRANAGAR)
    user = await make_customer(db, latitude=MYSURU[0] + 0.01, longitude=MYSURU[1])
    await login_as(client, user)
    r = await client.get("/gyms")
    html = r.text
    assert r.status_code == 200
    assert html.index("Near Mysuru Gym") < html.index("Far Bengaluru Gym")
    assert "your saved location" in html


async def test_directory_pagination(client, db):
    o = await make_owner(db)
    for i in range(13):
        await make_gym(db, o, name=f"Online Gym {i:02d}", online=True, offline=False)
    r1 = await client.get("/gyms")
    assert cards_in(r1.text) == 12 and "Page <span" in r1.text and "?page=2" in r1.text
    r2 = await client.get("/gyms?page=2")
    assert cards_in(r2.text) == 1 and "Online Gym 12" in r2.text
    r3 = await client.get("/gyms?page=99")  # clamps to the last page
    assert cards_in(r3.text) == 1
    r4 = await client.get("/gyms?page=abc")
    assert cards_in(r4.text) == 12


async def test_directory_query_count_is_constant(client, db):
    async def gym_batch(n, start):
        for i in range(n):
            owner, gym, branch = await published_gym(db, name=f"Batch Gym {start + i}", online=bool(i % 2))
            db.add(BranchFeature(branch_id=branch.id, name="Sauna"))
            await db.commit()

    async def counts() -> tuple[int, int, int]:
        out = []
        for url in (f"/gyms?lat={INDIRANAGAR[0]}&lon={INDIRANAGAR[1]}", "/gyms?q=batch", "/"):
            with QueryCounter() as qc:
                r = await client.get(url)
            assert r.status_code == 200, url
            out.append(qc.count)
        return tuple(out)

    await gym_batch(2, 0)
    for g in (await db.execute(select(Gym))).scalars().all():
        await make_employee(db, g)
    small = await counts()
    r = await client.get("/gyms")
    assert cards_in(r.text) == 2
    await gym_batch(9, 10)
    for g in (await db.execute(select(Gym))).scalars().all():
        await make_employee(db, g)
    large = await counts()
    r = await client.get("/gyms")
    assert cards_in(r.text) == 11
    assert min(small) > 0
    assert large == small, (small, large)  # directory (sorted + searched) and home: constant query counts


async def test_gym_and_branch_pages_query_count_is_constant(client, db):
    _, gym, branch = await published_gym(db, online=True)
    await make_branch(db, gym, coords=WHITEFIELD)  # one sister branch: the "other locations" cards are loaded

    async def counts() -> tuple[int, ...]:
        out = []
        for url in (f"/gyms/{gym.slug}", f"/gyms/{gym.slug}/branches/{branch.id}", f"/gyms/{gym.slug}/branches",
                    f"/gyms/{gym.slug}?lat={KORAMANGALA[0]}&lon={KORAMANGALA[1]}"):
            with QueryCounter() as qc:
                r = await client.get(url)
            assert r.status_code == 200
            out.append(qc.count)
        return tuple(out)

    small = await counts()
    for _ in range(4):
        extra = await make_branch(db, gym, coords=KORAMANGALA)
        db.add(BranchFeature(branch_id=extra.id, name="Pool"))
        await db.commit()
        emp = await make_employee(db, gym)
        await make_shift(db, emp, branch=branch, weekday=2)
        await make_shift(db, emp, branch=branch, weekday=3)
    assert await counts() == small


# ------------------------------------------------------------------ gym brand page
async def test_gym_page_404s(client, db):
    from app.services.accounts import soft_delete_user

    assert (await client.get("/gyms/does-not-exist")).status_code == 404
    _, inactive, _ = await published_gym(db)
    inactive.is_active = False
    await db.commit()
    assert (await client.get(f"/gyms/{inactive.slug}")).status_code == 404
    o = await make_owner(db)
    draft = await make_gym(db, o, online=False)
    await make_branch(db, draft, verified=False)
    assert (await client.get(f"/gyms/{draft.slug}")).status_code == 404  # nothing public yet
    gone = await make_owner(db)
    _, orphan, _ = await published_gym(db, owner=gone)
    await soft_delete_user(db, gone, reason="test")
    await db.commit()
    assert (await client.get(f"/gyms/{orphan.slug}")).status_code == 404


async def test_gym_page_shows_owner_photo_branches_plans_and_team(client, db):
    owner = await make_owner(db, full_name="Meera Kapoor", photo_key="public/avatars/" + "a" * 32 + ".jpg")
    _, gym, branch = await published_gym(db, name="Kapoor Strength", owner=owner, online=True, branch_name="Indiranagar Club")
    hidden_branch = await make_branch(db, gym, name="Secret Draft Branch", verified=False)
    db.add(BranchFeature(branch_id=branch.id, name="Steam room"))
    await db.commit()
    trainer = await make_employee(db, gym)
    trainer.specialties = ["Powerlifting", "Mobility"]
    trainer.role_title = "Head Coach"
    await db.commit()
    invited = await make_employee(db, gym, status=EmploymentStatus.INVITED)
    private = await make_employee(db, gym)
    private.is_public = False
    await db.commit()

    r = await client.get(f"/gyms/{gym.slug}")
    assert r.status_code == 200
    html = r.text
    assert "Meera Kapoor" in html and f'src="/media/{owner.photo_key}"' in html  # owner card with photo
    assert "Indiranagar Club" in html and "Steam room" in html
    assert hidden_branch.name not in html
    assert "₹1,499.00" in html and "Offline + Trainer + Ai" in html  # plan names + money filter
    assert trainer.full_name in html and "Head Coach" in html and "Powerlifting" in html
    assert f'src="/media/{trainer.photo_key}"' in html
    assert invited.full_name not in html and private.full_name not in html
    assert "Online · open" in html
    assert f"/subscribe/{gym.slug}?plan=online" in html  # online plans need no location


async def test_gym_page_offline_gating_near_far_city_unknown(client, db, anon_client_factory):
    _, gym, branch = await published_gym(db, name="Gate Gym", online=True)
    offline_link = f"/subscribe/{gym.slug}?plan=offline&amp;branch={branch.id}"

    # near (~5 km): offline plans purchasable for the nearest branch
    r = await client.get(f"/gyms/{gym.slug}?lat={KORAMANGALA[0]}&lon={KORAMANGALA[1]}")
    assert r.status_code == 200 and offline_link in r.text
    assert "In-person plans are available to you" in r.text and "not near your area" not in r.text

    # far (Mysuru, ~128 km): the exact eligibility message, no offline/combo links, online still sold
    r = await client.get(f"/gyms/{gym.slug}?lat={MYSURU[0]}&lon={MYSURU[1]}")
    html = r.text
    assert "not near your area" in html and "visit the gym owner directly" in html
    assert offline_link not in html and f"?plan=combo&amp;branch={branch.id}" not in html
    assert f"/subscribe/{gym.slug}?plan=online" in html

    # unknown location: "Use my location" (foundation geolocate) + no offline link
    anon = anon_client_factory("203.0.113.44")
    r = await anon.get(f"/gyms/{gym.slug}")
    assert 'data-component="geolocate"' in r.text and "Use my location" in r.text
    assert offline_link not in r.text

    # city fallback (alias "Bangalore" == Bengaluru) for a signed-in user without coordinates
    user = await make_customer(db, city="Bangalore")
    await login_as(client, user)
    r = await client.get(f"/gyms/{gym.slug}")
    assert offline_link in r.text and "A branch is in Bengaluru" in r.text

    other = await make_customer(db, city="Chennai")
    c2 = anon_client_factory("203.0.113.45")
    await login_as(c2, other)
    r = await c2.get(f"/gyms/{gym.slug}")
    assert "not near your area" in r.text and offline_link not in r.text


async def test_gym_page_online_only_and_online_closed(client, db):
    o = await make_owner(db)
    gym = await make_gym(db, o, name="Remote Reps", online=True, offline=False)
    await make_plans(db, gym)
    r = await client.get(f"/gyms/{gym.slug}")
    assert r.status_code == 200
    assert "Remote Reps trains online" in r.text
    assert "?plan=offline" not in r.text and "?plan=combo" not in r.text  # no branch -> no in-person plans
    assert f"/subscribe/{gym.slug}?plan=online_ai" in r.text

    gym.online_open = False
    await db.commit()
    r = await client.get(f"/gyms/{gym.slug}")
    assert r.status_code == 200  # still reachable, shown as temporarily closed
    assert "temporarily closed" in r.text and "?plan=online" not in r.text


# ------------------------------------------------------------------ branch page
async def test_branch_page_published_only(client, db):
    _, gym, branch = await published_gym(db)
    assert (await client.get(f"/gyms/{gym.slug}/branches/{branch.id}")).status_code == 200
    unverified = await make_branch(db, gym, verified=False)
    closed = await make_branch(db, gym, is_open=False)
    few_photos = await make_branch(db, gym, photos=2)
    for b in (unverified, closed, few_photos):
        assert (await client.get(f"/gyms/{gym.slug}/branches/{b.id}")).status_code == 404
    _, other_gym, other_branch = await published_gym(db)
    assert (await client.get(f"/gyms/{gym.slug}/branches/{other_branch.id}")).status_code == 404  # wrong brand
    assert (await client.get(f"/gyms/{gym.slug}/branches/abc")).status_code == 404
    assert (await client.get(f"/gyms/{gym.slug}/branches/99999")).status_code == 404
    gym.offline_enabled = False
    await db.commit()
    assert (await client.get(f"/gyms/{gym.slug}/branches/{branch.id}")).status_code == 404


async def test_branch_page_content_gallery_map_hours_team(client, db):
    owner = await make_owner(db, full_name="Arjun Rao")
    _, gym, branch = await published_gym(db, owner=owner, branch_name="Whitefield Hub", coords=WHITEFIELD)
    db.add(BranchFeature(branch_id=branch.id, name="Olympic platforms"))
    await db.commit()
    trainer = await make_employee(db, gym)
    for wd in (0, 1, 2, 4):
        await make_shift(db, trainer, branch=branch, weekday=wd, start="06:00", end="10:00")
    receptionist = await make_employee(db, gym, role=EmployeeRole.RECEPTIONIST)
    other_branch = await make_branch(db, gym, name="Koramangala Hub", coords=KORAMANGALA)
    elsewhere = await make_employee(db, gym)
    await make_shift(db, elsewhere, branch=other_branch, weekday=3)
    suspended = await make_employee(db, gym, status=EmploymentStatus.SUSPENDED)

    r = await client.get(f"/gyms/{gym.slug}/branches/{branch.id}")
    assert r.status_code == 200
    html = r.text
    assert html.count("data-gallery-item") == 3 and 'data-component="gallery"' in html
    assert f'data-lat="{branch.latitude}"' in html and 'data-component="branch-map"' in html
    assert re.search(r'<link rel="stylesheet" href="/static/vendor/leaflet/leaflet\.css', html)
    assert re.search(r'<script nonce="[^"]+" src="/static/vendor/leaflet/leaflet\.js[^"]*" defer>', html)
    assert all(day in html for day in ("Monday", "Sunday")) and "aria-current=\"date\"" in html
    assert ("Open now" in html) or ("Closed" in html)
    assert "Olympic platforms" in html
    team_section = html[html.index("Trainers &amp; staff at this branch"):html.index("Plans for Whitefield Hub")]
    assert trainer.full_name in team_section and "Mon–Wed, Fri · 06:00–10:00" in team_section
    assert receptionist.full_name in team_section  # "more of the team" (no shifts here)
    assert elsewhere.full_name in team_section
    assert team_section.index(trainer.full_name) < team_section.index("More of the")
    assert suspended.full_name not in html
    assert "Arjun Rao" in html and f"/media/{owner.photo_key}" in html
    assert "Koramangala Hub" in html  # other locations of the brand


async def test_branch_page_gating_uses_this_branch(client, db):
    _, gym, branch = await published_gym(db, coords=INDIRANAGAR)
    near = await client.get(f"/gyms/{gym.slug}/branches/{branch.id}?lat={KORAMANGALA[0]}&lon={KORAMANGALA[1]}")
    assert f"/subscribe/{gym.slug}?plan=offline_trainer&amp;branch={branch.id}" in near.text
    far = await client.get(f"/gyms/{gym.slug}/branches/{branch.id}?lat={MYSURU[0]}&lon={MYSURU[1]}")
    assert "not near your area" in far.text and "?plan=offline_trainer&amp;" not in far.text
    unknown = await client.get(f"/gyms/{gym.slug}/branches/{branch.id}")
    assert "Share your location" in unknown.text and 'data-component="geolocate"' in unknown.text


@pytest.mark.parametrize(("km", "eligible"), [(29.0, True), (31.0, False)])
async def test_offline_gating_30_km_boundary(client, db, km, eligible):
    _, gym, branch = await published_gym(db, coords=INDIRANAGAR)
    lat = INDIRANAGAR[0] + km / 111.195  # due north: 1 degree of latitude ~ 111.195 km
    r = await client.get(f"/gyms/{gym.slug}/branches/{branch.id}?lat={lat:.6f}&lon={INDIRANAGAR[1]}")
    link = f"/subscribe/{gym.slug}?plan=offline&amp;branch={branch.id}"
    assert (link in r.text) is eligible
    assert ("not near your area" in r.text) is not eligible


# ------------------------------------------------------------------ plans explainer
async def test_plans_page_matrix_and_notes(client):
    r = await client.get("/plans")
    assert r.status_code == 200
    html = r.text
    assert html.count("data-plan-code=") == 12
    assert "Pre-recorded workout videos are included in every plan" in html
    assert "Desk OTP for offline purchases" in html and "a 6-digit code is sent to the gym owner" in html
    assert "published (AI-verified, open) branch within 30 km" in html


# ------------------------------------------------------------------ jobs board
async def test_jobs_list_filters_and_detail(client, db):
    _, gym, branch = await published_gym(db, name="Hiring Gym")
    open_job = await make_job(db, gym, title="Senior Strength Coach", branch=branch)
    remote_job = await make_job(db, gym, title="Remote Nutritionist", location="Remote", role=EmployeeRole.NUTRITIONIST,
                                etype=EmploymentType.PART_TIME, salary=None)
    closed = await make_job(db, gym, title="Closed Role", is_open=False)
    o2 = await make_owner(db)
    inactive_gym = await make_gym(db, o2, name="Gone Gym")
    inactive_gym.is_active = False
    await db.commit()
    hidden = await make_job(db, inactive_gym, title="Ghost Role")

    r = await client.get("/jobs")
    html = r.text
    assert r.status_code == 200
    assert "Senior Strength Coach" in html and "Remote Nutritionist" in html
    assert "Closed Role" not in html and "Ghost Role" not in html
    assert "₹25,000 – ₹35,000 / month" in html and "INR" in html
    assert "Remote" in html and "Part time" in html

    r = await client.get("/jobs?role=nutritionist")
    assert "Remote Nutritionist" in r.text and "Senior Strength Coach" not in r.text
    r = await client.get("/jobs?type=full_time&q=strength")
    assert "Senior Strength Coach" in r.text and "Remote Nutritionist" not in r.text
    r = await client.get("/jobs?role=astronaut")  # unknown enum values are ignored
    assert r.status_code == 200 and "Senior Strength Coach" in r.text

    r = await client.get(f"/jobs/{open_job.id}")
    assert r.status_code == 200
    assert "Enquire about this job" in r.text and 'name="website"' in r.text and 'enctype="multipart/form-data"' in r.text
    assert "2+ years of coaching experience" in r.text and "Remote Nutritionist" in r.text  # more roles at this gym
    assert (await client.get(f"/jobs/{closed.id}")).status_code == 404
    assert (await client.get(f"/jobs/{hidden.id}")).status_code == 404
    assert (await client.get("/jobs/424242")).status_code == 404
    assert (await client.get("/jobs/abc")).status_code == 404
    assert remote_job.id


async def test_jobs_list_query_count_is_constant(client, db):
    _, gym, _ = await published_gym(db)
    await make_job(db, gym, title="Job 0")
    with QueryCounter() as small:
        await client.get("/jobs")
    for i in range(1, 8):
        _, g, b = await published_gym(db)
        await make_job(db, g, title=f"Job {i}", branch=b)
    with QueryCounter() as large:
        r = await client.get("/jobs")
    assert "Job 7" in r.text
    assert large.count == small.count, (small.count, large.count)


async def test_job_enquiry_validation_errors(client, db):
    _, gym, _ = await published_gym(db)
    job = await make_job(db, gym)
    r = await post_form(client, f"/jobs/{job.id}/enquire", {"name": "A", "email": "not-an-email", "phone": "12",
                                                              "message": "too short"})
    assert r.status_code == 400
    html = r.text
    assert "Please fix the following" in html
    assert "Please enter your full name." in html
    assert "valid e-mail address" in html
    assert "at least 20 characters" in html
    assert "valid phone number" in html
    assert 'value="not-an-email"' in html  # values are kept
    r = await post_form(client, f"/jobs/{job.id}/enquire", {"name": "Long Message", "email": "long@example.com",
                                                              "message": "x" * 4001})
    assert r.status_code == 400 and "under 4,000 characters" in r.text
    r = await post_form(client, f"/jobs/{job.id}/enquire", {"name": "Png Resume", "email": "png@example.com",
                                                              "message": "I have five years of experience coaching."},
                        files={"resume": ("cv.png", png_bytes(), "image/png")})
    assert r.status_code == 400 and "as a PDF file" in r.text
    assert await db.scalar(select(func.count(JobEnquiry.id))) == 0


async def test_job_enquiry_success_notifies_owner_and_emails_applicant(client, db):
    owner, gym, _ = await published_gym(db, name="Notify Gym")
    job = await make_job(db, gym, title="Floor Coach")
    r = await post_form(
        client, f"/jobs/{job.id}/enquire",
        filled_in(job, {"name": "  Priya Sharma ", "email": "Priya@Example.com", "phone": "+91 98765 43210",
                        "message": "Certified coach with 4 years of experience in strength training.", "website": ""}),
        files={"resume": ("Priya CV.pdf", pdf_bytes(), "application/pdf")},
    )
    assert r.status_code == 303 and r.headers["location"] == f"/jobs/{job.id}?sent=1#enquire"
    enquiry = (await db.execute(select(JobEnquiry))).scalar_one()
    assert enquiry.job_id == job.id and enquiry.name == "Priya Sharma" and enquiry.email.lower() == "priya@example.com"
    assert enquiry.resume_key and enquiry.resume_key.startswith("private/resumes/") and enquiry.ip == "127.0.0.1"
    note = (await db.execute(select(Notification).where(Notification.user_id == owner.id))).scalar_one()
    assert "New job enquiry" in note.title and note.category == NotificationCategory.STAFF and note.link == f"/owner/jobs/{job.id}"
    mails = (await db.execute(select(EmailOutbox))).scalars().all()
    assert any(m.to_email.lower() == "priya@example.com" and "We received your enquiry" in m.subject for m in mails)
    assert any(m.to_email == owner.email for m in mails)

    from app.services import storage

    assert (await storage.load_private(enquiry.resume_key)).startswith(b"%PDF-")

    page = await client.get(r.headers["location"])
    assert "Enquiry sent" in page.text and "Priya" in page.text  # success panel + flash


async def test_job_enquiry_honeypot_is_silently_discarded(client, db):
    _, gym, _ = await published_gym(db)
    job = await make_job(db, gym)
    r = await post_form(client, f"/jobs/{job.id}/enquire",
                        {"name": "Spam Bot", "email": "bot@example.com", "message": "Buy cheap things now at my website!!",
                         "website": "http://spam.example"})
    assert r.status_code == 303
    assert await db.scalar(select(func.count(JobEnquiry.id))) == 0
    assert await db.scalar(select(func.count(Notification.id))) == 0


async def test_job_enquiry_closed_or_unknown_job_404(client, db):
    _, gym, _ = await published_gym(db)
    job = await make_job(db, gym, is_open=False)
    data = {"name": "Late Applicant", "email": "late@example.com", "message": "I would love to work at your gym please."}
    assert (await post_form(client, f"/jobs/{job.id}/enquire", data)).status_code == 404
    assert (await post_form(client, "/jobs/987654/enquire", data)).status_code == 404
    assert await db.scalar(select(func.count(JobEnquiry.id))) == 0


async def test_job_enquiry_requires_csrf(client, db):
    _, gym, _ = await published_gym(db)
    job = await make_job(db, gym)
    await client.get("/healthz")
    r = await client.post(f"/jobs/{job.id}/enquire", data={"name": "No Token", "email": "x@example.com",
                                                           "message": "A message that is long enough to pass."})
    assert r.status_code == 403


# ------------------------------------------------------------------ legal + about
async def test_terms_privacy_about(client):
    r = await client.get("/terms")
    assert r.status_code == 200
    text = r.text.lower()
    assert "not responsible" in text and "student" in text
    for phrase in ("injuries", "health outcomes", "ai advice", "verification decisions",
                   "payroll", "gratuity", "payments", "data loss", "outages", "medical, legal or financial advice",
                   "emergency services", "guardian", "encrypted", "30 days", "test mode"):
        assert phrase in text, phrase
    r = await client.get("/privacy")
    assert r.status_code == 200 and "face photos" in r.text.lower() and "never stored" in r.text.lower()
    assert "30 days" in r.text
    r = await client.get("/about")
    assert r.status_code == 200 and "student-grade project" in r.text.lower()


# ------------------------------------------------------------------ CSP hygiene of rendered pages
async def test_public_pages_have_no_inline_handlers_or_styles(client, db):
    _, gym, branch = await published_gym(db, online=True)
    await make_employee(db, gym)
    job = await make_job(db, gym)
    urls = ["/", "/gyms", f"/gyms/{gym.slug}", f"/gyms/{gym.slug}/branches/{branch.id}", "/plans", "/jobs",
            f"/jobs/{job.id}", "/terms", "/privacy", "/about"]
    for url in urls:
        r = await client.get(url)
        assert r.status_code == 200, url
        html = r.text
        assert not re.search(r"\sstyle\s*=", html), url
        assert not re.search(r"\son[a-z]+\s*=", html), url
        for tag in re.findall(r"<script\b[^>]*>", html):
            assert "nonce=" in tag, (url, tag)


def test_public_js_registers_components_and_templates_use_them():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    js = (root / "app/static/src/js/10-public.js").read_text(encoding="utf-8")
    for name in ("ai-live", "geo-form", "gallery", "branch-map", "distance-sort"):
        assert f'IP.register("{name}"' in js, name
    assert "innerHTML" not in js  # DOM is built with IP.h / textContent only
    assert "/maps/tiles/{z}/{x}/{y}.png" in js and "OpenStreetMap" in js
    templates = "".join(p.read_text(encoding="utf-8") for p in (root / "app/templates/public").glob("*.html"))
    for name in ("ai-live", "geo-form", "gallery", "branch-map", "distance-sort", "geolocate"):
        assert f'data-component="{name}"' in templates, name
