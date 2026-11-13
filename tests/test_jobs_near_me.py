"""Jobs board "near me" - the gym directory's location option, capped by the platform's 30 km "near" rule.

* location: ``?lat=&lon=`` from the "Use my location" geo-form button in the search panel, else the signed-in user's
  saved coordinates (``visitor_location``, exactly like /gyms);
* a posting's distance: to its own branch while that branch is PUBLISHED (even a far one - it never borrows a sister
  branch's distance); a posting whose own branch is set but NOT published (draft / closed / photo-less ...) has an
  unknown location - no distance, never measured to a sister branch, left out of "Near me", listed under "All jobs"
  without a distance (product decision); a posting for the whole gym (no branch): the gym's nearest PUBLISHED branch;
  remote: none; an unpublished branch's name and city are never shown (the posting's own location text is);
* "Near me" (default): only postings within the radius chip (5 / 10 / 20 / 30 km; default = maximum = the limit),
  nearest first (ties: newest), remote postings in their own block ("Include remote jobs", remote=0 hides them);
  "All jobs": every posting newest first, a distance chip only within the limit; nothing farther is ever shown;
  labels are rounded UP (5.04 km reads "5.1 km") so they always agree with the radius chips;
* job links carry the list's whole search, so the job page (and the enquiry flow) lead back to that very list;
* the browser only supplies coordinates; distances, filter, order, paging and the total are computed by PostgreSQL.

Query budget (SQL statements per request, ``app.db.QueryCounter``) - pinned by ``test_query_budget_is_exact``:
  /jobs as a guest, any view / page / number of postings ... 1  WITH near_branches (box scan + haversine, once),
                                                                  gym_nearest, placed_jobs (place once per posting),
                                                                  ranked_jobs (count(*) OVER ()), jobs_page (clamped)
                                                                  SELECT page rows + gym + published own branch
  /jobs signed in ......................................... 2  (+1: the session/user lookup of load_session)
  /jobs/{id} .............................................. 2  (posting + gym + branch + its distance; "more roles")
"""

from __future__ import annotations

import html as htmllib
import json
import math
import re
import time
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import event, func, select, text

from app import db as dbmod
from app.core.geo import EARTH_RADIUS_KM, haversine_km
from app.db import QueryCounter
from app.models import JobPosting
from app.models.enums import EmploymentType, EmployeeRole
from app.routers.public import ENQUIRY_TOKEN_FIELD, enquiry_form_token
from tests.conftest import login_as, post_form
from tests.factories import INDIRANAGAR, KORAMANGALA, MYSURU, WHITEFIELD, make_branch, make_customer, make_gym, make_owner

HERE = f"lat={INDIRANAGAR[0]}&lon={INDIRANAGAR[1]}"  # the visitor stands at the Indiranagar landmark
HERE_Q = {"lat": [str(INDIRANAGAR[0])], "lon": [str(INDIRANAGAR[1])]}
KM_PER_DEGREE_LAT = EARTH_RADIUS_KM * math.pi / 180  # along a meridian the app's haversine is exactly R * dlat

GUEST_STATEMENTS = 1
SESSION_STATEMENTS = 1
DETAIL_STATEMENTS = 2


def offset(point: tuple[float, float], north_km: float, east_km: float = 0.0) -> tuple[float, float]:
    lat = point[0] + north_km / KM_PER_DEGREE_LAT
    return lat, point[1] + east_km / (KM_PER_DEGREE_LAT * math.cos(math.radians(lat)))


NORTH_29 = offset(INDIRANAGAR, 28.6)  # reads "29 km" (rounded up); away from a whole km, so float noise can't matter
NORTH_31 = offset(INDIRANAGAR, 31)
CORNER = offset(INDIRANAGAR, 25, 25)  # inside the 30 km bounding box, but ~35 km away
FAR_AWAY = (19.0760, 72.8777)  # Mumbai: no posting within 30 km


# ------------------------------------------------------------------ data helpers
async def gym_with(db, name, *branches, online=False, offline=True):
    """A gym with branches given as (name, coords, kwargs-for-make_branch)."""
    gym = await make_gym(db, await make_owner(db), name=name, online=online, offline=offline)
    made = [await make_branch(db, gym, name=bname, coords=coords, **kw) for bname, coords, kw in branches]
    return gym, made


async def post_job(db, gym, title, *, branch=None, location="Bengaluru", role=EmployeeRole.TRAINER,
                   etype=EmploymentType.FULL_TIME):
    job = JobPosting(gym_id=gym.id, branch_id=branch.id if branch else None, title=title, role=role,
                     employment_type=etype, location=location, salary_min=Decimal("20000"),
                     salary_max=Decimal("30000"), currency=gym.currency, requirements="- Certified coach",
                     description="Coach members.", is_open=True)
    db.add(job)
    await db.commit()
    return job


async def board(db):
    """The visitor stands at Indiranagar. Postings, oldest first (so newest-first is the reverse):

    B1  Indiranagar Trainer   own branch right here ......................................... 10 m
    A2  Alpha Floor Manager   no branch -> Alpha's nearest published branch (Koramangala) ..... 4.43 km
    A1  Whitefield Coach      own branch Whitefield, although Koramangala is nearer ............ 12 km
    E29 Edge Coach 29         own branch 28.6 km due north .................................... 28.6 km
    E31 Edge Coach 31         own branch 31 km due north (sister branch at 28.6 km) ........... 31 km - never shown
    C35 Corner Coach          own branch in the corner of the 30 km box ....................... ~35 km - never shown
    M   Mysuru Coach          own branch in Mysuru ............................................ 132 km - never shown
    R   Remote Nutritionist   "Remote (India)" at a gym with a branch right here .............. remote, no distance"""
    gym_a, (whitefield, koramangala) = await gym_with(
        db, "Alpha Fitness", ("Whitefield Hub", WHITEFIELD, {}), ("Koramangala Hub", KORAMANGALA, {}))
    gym_b, (indiranagar,) = await gym_with(db, "Beta Barbell", ("Indiranagar Club", INDIRANAGAR, {}))
    gym_e, (north_29, north_31, corner) = await gym_with(
        db, "Edge Gym", ("North 29 Club", NORTH_29, {}), ("North 31 Club", NORTH_31, {}), ("Corner Club", CORNER, {}))
    gym_m, (mysuru,) = await gym_with(db, "Mysuru Muscle", ("Mysuru Main", MYSURU, {"city": "Mysuru"}))
    gym_c, (studio,) = await gym_with(db, "Gamma Online", ("Gamma Studio", INDIRANAGAR, {}), online=True)
    jobs = {
        "B1": await post_job(db, gym_b, "Indiranagar Trainer", branch=indiranagar),
        "A2": await post_job(db, gym_a, "Alpha Floor Manager", role=EmployeeRole.MANAGER),
        "A1": await post_job(db, gym_a, "Whitefield Coach", branch=whitefield),
        "E29": await post_job(db, gym_e, "Edge Coach 29", branch=north_29),
        "E31": await post_job(db, gym_e, "Edge Coach 31", branch=north_31),
        "C35": await post_job(db, gym_e, "Corner Coach", branch=corner),
        "M": await post_job(db, gym_m, "Mysuru Coach", branch=mysuru, location="Mysuru"),
        "R": await post_job(db, gym_c, "Remote Nutritionist", location="Remote (India)", branch=studio,
                            role=EmployeeRole.NUTRITIONIST, etype=EmploymentType.CONTRACT),
    }
    return jobs, {"gym_a": gym_a, "gym_e": gym_e}


NEAR = ["Indiranagar Trainer", "Alpha Floor Manager", "Whitefield Coach", "Edge Coach 29"]
NEWEST = ["Remote Nutritionist", "Mysuru Coach", "Corner Coach", "Edge Coach 31", "Edge Coach 29", "Whitefield Coach",
          "Alpha Floor Manager", "Indiranagar Trainer"]
NEAR_CHIPS = {"Indiranagar Trainer": "10 m away", "Alpha Floor Manager": "4.5 km away · nearest branch: Koramangala Hub",
              "Whitefield Coach": "12 km away", "Edge Coach 29": "29 km away"}


# ------------------------------------------------------------------ page helpers
CARD = re.compile(r'<a href="(/jobs/\d+[^"]*)" class="group card[^"]*">(.*?)</a>\s*</li>', re.S)


def job_cards(page: str) -> list[tuple[str, str, str, int]]:
    """(title, href, card html, offset) of every job card on a /jobs page, in page order."""
    out = []
    for m in CARD.finditer(page):
        title = re.search(r"<h[23][^>]*>(.*?)</h[23]>", m.group(2), re.S).group(1).strip()
        out.append((htmllib.unescape(title), htmllib.unescape(m.group(1)), m.group(2), m.start()))
    return out


def titles(page: str) -> list[str]:
    return [t for t, *_ in job_cards(page)]


def split_titles(page: str) -> tuple[list[str], list[str]]:
    """(cards before, cards inside) the "Remote positions" block."""
    at = page.find('id="remote-jobs"')
    cards = job_cards(page)
    if at < 0:
        return [t for t, *_ in cards], []
    return [t for t, _, _, pos in cards if pos < at], [t for t, _, _, pos in cards if pos > at]


def distance_of(card: str) -> str | None:
    m = re.search(r'data-job-distance>.*?<span class="min-w-0 truncate leading-tight">(.*?)</span>', card, re.S)
    return htmllib.unescape(m.group(1)) if m else None


def chips(page: str) -> dict[str, str | None]:
    return {t: distance_of(card) for t, _, card, _ in job_cards(page)}


def location_badge(card: str) -> str | None:
    """The map-pin location badge of a job card ("<branch>, <city>" or the posting's location text)."""
    m = re.search(r'<span class="badge badge-zinc">(?:<svg\b[^>]*/>|<svg\b.*?</svg>)([^<]*)</span>', card, re.S)
    return htmllib.unescape(m.group(1)) if m else None


def label_km(label: str) -> float:
    """'5.1 km away' -> 5.1, '10 m away · nearest branch: X' -> 0.01."""
    number, unit = re.match(r"(\d+(?:\.\d+)?) (km|m) away", label).groups()
    return float(number) / (1000 if unit == "m" else 1)


def detail_distance(page: str) -> str | None:
    m = re.search(r'data-job-distance>.*?<span class="min-w-0">(.*?)</span></p>', page, re.S)
    return htmllib.unescape(m.group(1)) if m else None


def shown_km(page: str) -> list[float]:
    """Every "<number> km" in the visible page - none may exceed the 30 km limit."""
    return [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*km\b", page)]


def query_of(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def hrefs(page: str) -> list[str]:
    return [htmllib.unescape(h) for h in re.findall(r'href="([^"]*)"', page)]


def link(page: str, label: str) -> str:
    m = re.search(r'<a href="([^"]*)"[^>]*>\s*' + re.escape(label) + r"\s*</a>", page)
    assert m, label
    return htmllib.unescape(m.group(1))


def rel_link(page: str, rel: str) -> str | None:
    m = re.search(r'<a href="([^"]*)"[^>]*rel="' + rel + '"', page)
    return htmllib.unescape(m.group(1)) if m else None


def form_html(page: str, marker: str) -> str:
    start = page.rindex("<form", 0, page.index(marker))
    return page[start:page.index("</form>", start)]


def hidden(form: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, value in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form):
        out.setdefault(name, []).append(htmllib.unescape(value))
    return out


def assert_within_limit(page: str) -> None:
    assert all(km <= 30 for km in shown_km(page)), shown_km(page)
    for far in ("31 km", "35 km", "36 km", "132 km", "133 km"):
        assert far not in page, far


# ------------------------------------------------------------------ pure helpers
def test_remote_rule_keeps_the_old_behaviour():
    from app.routers.public import JobView, is_remote_location

    for t in ("Remote", "remote", " REMOTE ", "Remote (India)", "remote - IST hours", "Online", "anywhere",
              "Work from home", "WFH", "　Online ", "\tremote\n"):
        assert is_remote_location(t), t
    for t in ("Bengaluru", "Bengaluru - Indiranagar", "", None, "Hybrid (remote 2 days)", "On-site"):
        assert not is_remote_location(t), t
    assert JobView(JobPosting(location="Remote")).remote and not JobView(JobPosting(location="Pune")).remote


async def test_remote_rule_sql_twin_matches_python(db):
    from app.routers.public import is_remote_location, remote_job_clause

    gym, _ = await gym_with(db, "Rule Gym")
    texts = ["Remote", " remote ", "REMOTE (EU)", "Remotely", "online", "Online classes", "anywhere", "Anywhere, India",
             "work from home", "Work From Home", "wfh", "WFH ", "Bengaluru", "Hybrid - remote", " Remote ",
             " wfh", "Ŕemote", "worK from home", "Remote　", "x"]
    for i, t in enumerate(texts):
        await post_job(db, gym, f"Rule {i}", location=t)
    sql_remote = set((await db.execute(select(JobPosting.location).where(remote_job_clause()))).scalars().all())
    assert sql_remote == {t for t in texts if is_remote_location(t)}
    assert "Remotely" in sql_remote and "Bengaluru" not in sql_remote


def test_limit_and_radius_chips_come_from_settings(monkeypatch):
    from app.config import get_settings
    from app.routers.public import job_radii, jobs_max_km, km_param

    assert jobs_max_km() == get_settings().offline_max_distance_km == 30
    assert [km_param(r) for r in job_radii(jobs_max_km())] == ["5", "10", "20", "30"]
    monkeypatch.setattr(get_settings(), "offline_max_distance_km", 15.0)
    assert jobs_max_km() == 15 and [km_param(r) for r in job_radii(jobs_max_km())] == ["5", "10", "15"]


def test_job_distance_labels_round_up_and_never_exceed_the_limit(monkeypatch):
    from app.config import get_settings
    from app.routers.public import job_km_label, km_label

    cases = {0.0: "10 m", 0.004: "10 m", 0.0951: "100 m", 0.994: "1.0 km", 1.0: "1.0 km", 4.43: "4.5 km",
             4.96: "5.0 km", 5.0: "5.0 km", 5.04: "5.1 km", 9.91: "10 km", 10.0: "10 km", 10.3: "11 km",
             20.4: "21 km", 29.2: "30 km", 30.0: "30 km", None: None}
    assert {d: job_km_label(d) for d in cases} == cases
    assert km_label(5.04) == "5.0 km" and km_label(10.3) == "10 km"  # the directory's labels are unchanged
    monkeypatch.setattr(get_settings(), "offline_max_distance_km", 12.5)  # a limit that is not a whole km
    assert job_km_label(12.3) == "12.5 km" and job_km_label(11.2) == "12 km" and job_km_label(9.97) == "10 km"


def destination(lat: float, lon: float, km: float, bearing: float) -> tuple[float, float]:
    d, b, p1, l1 = km / EARTH_RADIUS_KM, math.radians(bearing), math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


@pytest.mark.parametrize("lat", [-89.95, -80, -45, -12.5, 0, 12.9716, 45, 66.5, 80, 89.7, 89.95])
@pytest.mark.parametrize("lon", [-179.97, -120, 0.0, 77.6412, 179.97])
def test_radius_box_contains_the_whole_circle(lat, lon):
    from app.routers.public import radius_box

    for km in (5, 10, 20, 30):
        box = radius_box(lat, lon, km)
        for step in range(96):
            p_lat, p_lon = destination(lat, lon, km, step * 3.75)
            assert abs(haversine_km(lat, lon, p_lat, p_lon) - km) < 1e-6
            assert box.lat_min <= p_lat <= box.lat_max, (km, step, box)
            if box.lon_min is not None:
                assert box.lon_min <= p_lon <= box.lon_max, (km, step, box)


def test_radius_box_is_tight_away_from_poles_and_antimeridian():
    from app.routers.public import radius_box

    box = radius_box(*INDIRANAGAR, 30)
    assert box.lon_min is not None and 0.5 < box.lat_max - box.lat_min < 0.55 and 0.5 < box.lon_max - box.lon_min < 0.6
    assert radius_box(89.9, 10, 30).lon_min is None  # reaches the pole: every longitude
    assert radius_box(10, 179.9, 30).lon_min is None and radius_box(10, -179.9, 30).lon_min is None  # antimeridian
    lat, lon = CORNER
    assert box.lat_min <= lat <= box.lat_max and box.lon_min <= lon <= box.lon_max
    assert haversine_km(*INDIRANAGAR, *CORNER) > 35  # in the box, far outside the circle


# ------------------------------------------------------------------ near me: 30 km cap, order, remote block
async def test_near_me_lists_only_jobs_within_30_km_nearest_first_then_remote_block(client, db):
    await board(db)
    r = await client.get(f"/jobs?{HERE}")
    assert r.status_code == 200
    page = r.text
    nearby, remote = split_titles(page)
    assert nearby == NEAR
    assert remote == ["Remote Nutritionist"]
    assert chips(page) == {**NEAR_CHIPS, "Remote Nutritionist": None}  # remote: never a distance
    remote_card = job_cards(page)[-1][2]
    assert "Remote</span>" in remote_card and "away" not in remote_card
    for gone in ("Edge Coach 31", "Corner Coach", "Mysuru Coach"):  # farther than 30 km: not listed at all
        assert gone not in page
    assert_within_limit(page)
    assert "4 jobs within 30 km · 1 remote" in page
    assert '<span data-location-line>Showing jobs within 30 km of your location</span>' in page
    assert query_of(link(page, "Clear location")) == {}
    for _, href, _, _ in job_cards(page):  # the location follows the visitor to the job pages
        assert query_of(href) == HERE_Q
    assert "distance-sort" not in page and "data-lat=" not in page  # the browser never measures or sorts


async def test_ties_are_newest_first(client, db):
    gym, (club,) = await gym_with(db, "Tie Gym", ("Tie Club", KORAMANGALA, {}))
    await post_job(db, gym, "Older Tie")
    await post_job(db, gym, "Own Branch Tie", branch=club)
    await post_job(db, gym, "Newer Tie")
    assert titles((await client.get(f"/jobs?{HERE}")).text) == ["Newer Tie", "Own Branch Tie", "Older Tie"]


async def test_all_jobs_view_lists_everything_newest_first_with_chips_only_within_30_km(client, db):
    await board(db)
    r = await client.get(f"/jobs?{HERE}&view=all")
    page = r.text
    assert titles(page) == NEWEST and 'id="remote-jobs"' not in page
    assert chips(page) == {**NEAR_CHIPS, "Remote Nutritionist": None, "Mysuru Coach": None, "Corner Coach": None,
                           "Edge Coach 31": None}
    assert_within_limit(page)
    assert "8 open positions" in page
    assert "Showing all jobs · distances shown up to 30 km from your location" in page
    assert 'name="km"' not in page and "Include remote jobs" not in page  # near-me options belong to "Near me"


async def test_view_toggle_and_radius_chips(client, db):
    await board(db)
    page = (await client.get(f"/jobs?{HERE}")).text
    assert re.search(r'class="tab tab-active" aria-current="page">Near me</a>', page)
    assert query_of(link(page, "All jobs")) == {**HERE_Q, "view": ["all"]}
    chip_links = {label: htmllib.unescape(h) for h, label in
                  re.findall(r'<a href="([^"]*)" class="tab !px-3 [^"]*"[^>]*>(\d+) km</a>', page)}
    assert list(chip_links) == ["5", "10", "20", "30"]
    for label, href in chip_links.items():
        assert query_of(href) == ({**HERE_Q} if label == "30" else {**HERE_Q, "km": [label]})
    assert re.search(r'class="tab !px-3 tab-active" aria-current="true">30 km</a>', page)

    all_page = (await client.get(f"/jobs?{HERE}&view=all")).text
    assert re.search(r'class="tab tab-active" aria-current="page">All jobs</a>', all_page)
    assert query_of(link(all_page, "Near me")) == HERE_Q

    expected = {"5": NEAR[:2], "10": NEAR[:2], "20": NEAR[:3], "30": NEAR}
    for km, want in expected.items():
        page = (await client.get(f"/jobs?{HERE}&km={km}")).text
        assert split_titles(page) == (want, ["Remote Nutritionist"]), km
        assert f'<span data-location-line>Showing jobs within {km} km of your location</span>' in page
        assert re.search(rf'class="tab !px-3 tab-active" aria-current="true">{km} km</a>', page), km
        assert f"{len(want)} jobs within {km} km · 1 remote" in page
        assert_within_limit(page)


async def test_distance_labels_agree_with_the_radius_chips(client, db):
    """Review finding: labels rounded to the nearest ('5.0 km away' for 5.04 km, '10 km away' for 10.3 km) made jobs
    vanish under the chip their label matched. Labels are rounded UP: a job is listed under a chip exactly when its
    label is within it - checked on both sides of every chip."""
    edges = (4.96, 5.04, 9.96, 10.3, 19.6, 20.4, 29.7)
    gym, branches = await gym_with(db, "Edge Rounding Gym", *((f"Edge {d} Club", offset(INDIRANAGAR, d), {}) for d in edges))
    for d, branch in zip(edges, branches):
        await post_job(db, gym, f"Edge {d} Coach", branch=branch)
    all_chips = chips((await client.get(f"/jobs?{HERE}")).text)
    assert all_chips == {"Edge 4.96 Coach": "5.0 km away", "Edge 5.04 Coach": "5.1 km away", "Edge 9.96 Coach": "10 km away",
                         "Edge 10.3 Coach": "11 km away", "Edge 19.6 Coach": "20 km away", "Edge 20.4 Coach": "21 km away",
                         "Edge 29.7 Coach": "30 km away"}
    for km in (5, 10, 20, 30):
        listed = titles((await client.get(f"/jobs?{HERE}&km={km}")).text)
        assert listed == [f"Edge {d} Coach" for d in edges if d <= km], km
        assert all((label_km(label) <= km) == (title in listed) for title, label in all_chips.items()), km


@pytest.mark.parametrize("junk", ["7", "50", "31", "100", "abc", "-5", "5.0", "30.5", "", "1e9", "nan"])
async def test_any_other_radius_means_the_30_km_limit(client, db, junk):
    await board(db)
    page = (await client.get(f"/jobs?{HERE}&km={junk}")).text
    assert split_titles(page) == (NEAR, ["Remote Nutritionist"])
    assert "Showing jobs within 30 km of your location" in page and re.search(r'tab-active" aria-current="true">30 km<', page)
    assert_within_limit(page)


async def test_remote_toggle(client, db):
    await board(db)
    page = (await client.get(f"/jobs?{HERE}&remote=0")).text  # "Include remote jobs" unticked
    assert split_titles(page) == (NEAR, [])
    assert "Remote Nutritionist" not in page and "4 jobs within 30 km</p>" in page
    assert re.search(r'<input type="checkbox" name="remote" value="1" class="checkbox" data-autosubmit>', page)
    toggle = form_html(page, "Include remote jobs")
    assert hidden(toggle) == {**HERE_Q, "remote": ["0"]}

    page = (await client.get(f"/jobs?{HERE}&km=10&remote=0&remote=1")).text  # ticked again: hidden 0 + checkbox 1
    assert split_titles(page) == (NEAR[:2], ["Remote Nutritionist"])
    assert re.search(r'name="remote" value="1" class="checkbox" checked data-autosubmit>', page)
    assert hidden(form_html(page, "Include remote jobs")) == {**HERE_Q, "km": ["10"], "remote": ["0"]}

    for junk in ("yes", "", "2"):  # anything but remote=0 keeps the default (included)
        page = (await client.get(f"/jobs?{HERE}&remote={junk}")).text
        assert split_titles(page)[1] == ["Remote Nutritionist"], junk


async def test_own_branch_vs_gym_nearest_branch_vs_unpublished_branches(client, db):
    gym_d, (secret, shut, few, _kora) = await gym_with(
        db, "Delta Gym", ("Secret Site", INDIRANAGAR, {"verified": False}), ("Shut Site", INDIRANAGAR, {"is_open": False}),
        ("Few Photos Site", INDIRANAGAR, {"photos": 2}), ("Delta Koramangala", KORAMANGALA, {}))
    for title, own in (("Draft Branch Role", secret), ("Closed Branch Role", shut), ("Photoless Branch Role", few),
                       ("No Branch Role", None)):
        await post_job(db, gym_d, title, branch=own)
    gym_x, (draft_only,) = await gym_with(db, "Xray Gym", ("Xray Draft", INDIRANAGAR, {"verified": False}))
    await post_job(db, gym_x, "Draft Only Role", branch=draft_only)
    await post_job(db, gym_x, "Draft Gym Role")
    gym_f, (hidden_site,) = await gym_with(db, "Foxtrot Gym", ("Hidden Site", INDIRANAGAR, {}), online=True, offline=False)
    await post_job(db, gym_f, "Offline Off Role", branch=hidden_site)
    gym_m, (mysuru_main, _here_draft) = await gym_with(
        db, "Mysore Gym", ("Mysore Main", MYSURU, {"city": "Mysuru"}), ("Mysore Draft", INDIRANAGAR, {"verified": False}))
    await post_job(db, gym_m, "Mysore Draft Role", branch=_here_draft)

    page = (await client.get(f"/jobs?{HERE}")).text
    measured = "4.5 km away · nearest branch: Delta Koramangala"
    assert chips(page) == {"No Branch Role": measured}  # only the whole-gym posting borrows the gym's nearest branch
    unknown = ("Draft Branch Role", "Closed Branch Role", "Photoless Branch Role")  # own branch set, not published
    for hidden_role in (*unknown, "Draft Only Role", "Draft Gym Role", "Offline Off Role", "Mysore Draft Role"):
        assert hidden_role not in page  # location unknown, or no published branch within 30 km: not "near"
    assert "10 m away" not in page
    for name in ("Secret Site", "Shut Site", "Few Photos Site", "Xray Draft", "Hidden Site", "Mysore Draft"):
        assert f"nearest branch: {name}" not in page

    all_page = (await client.get(f"/jobs?{HERE}&view=all")).text
    c = chips(all_page)
    assert c["No Branch Role"] == measured
    assert all(c[t] is None for t in (*unknown, "Draft Only Role", "Draft Gym Role", "Offline Off Role",
                                      "Mysore Draft Role"))  # still listed, never with a distance
    assert "10 m away" not in all_page and "132 km" not in all_page and "133 km" not in all_page

    draft_role = await db.scalar(select(JobPosting.id).where(JobPosting.title == "Draft Branch Role"))
    detail = (await client.get(f"/jobs/{draft_role}?{HERE}")).text
    assert detail_distance(detail) is None and "from you" not in detail and "Delta Koramangala" not in detail

    # an unpublished branch is never named anywhere: the badge shows the posting's own location text instead
    no_location = (await client.get("/jobs")).text
    for listing in (page, all_page, no_location):
        badges = {t: location_badge(card) for t, _, card, _ in job_cards(listing)}
        assert badges and set(badges.values()) == {"Bengaluru"}, badges  # no posting here has a published branch
        for name in ("Secret Site", "Shut Site", "Few Photos Site", "Xray Draft", "Hidden Site", "Mysore Draft"):
            assert f"{name}," not in listing and f"nearest branch: {name}" not in listing, name
    for name in ("Secret Site", "Shut Site", "Few Photos Site"):
        assert name not in detail, name


async def test_a_far_posting_never_borrows_a_nearer_sister_branch(client, db):
    gym, (far, _near) = await gym_with(db, "Sister Gym", ("Far Club", NORTH_31, {}), ("Near Club", KORAMANGALA, {}))
    job = await post_job(db, gym, "Far Club Coach", branch=far)
    await post_job(db, gym, "Any Branch Coach")
    page = (await client.get(f"/jobs?{HERE}")).text
    assert titles(page) == ["Any Branch Coach"]
    all_page = (await client.get(f"/jobs?{HERE}&view=all")).text
    assert chips(all_page) == {"Any Branch Coach": "4.5 km away · nearest branch: Near Club", "Far Club Coach": None}
    detail = (await client.get(f"/jobs/{job.id}?{HERE}")).text
    assert detail_distance(detail) is None and "from you" not in detail and "31 km" not in detail


async def test_an_unpublished_own_branch_means_the_location_is_unknown(client, db):
    """Product decision: a posting whose own branch is set but NOT published (draft, closed, too few photos) has an
    unknown location - no distance, never the distance of a published sister branch (even one right next to the
    visitor), so "Near me" leaves it out at every radius; "All jobs" and its job page still show it, without a distance.
    Whole-gym and remote postings of the same gym are unaffected."""
    gym, (club, draft, shut, few) = await gym_with(
        db, "Unknown Place Gym", ("Here Club", INDIRANAGAR, {}), ("Draft Site", KORAMANGALA, {"verified": False}),
        ("Shut Site", INDIRANAGAR, {"is_open": False}), ("Few Photos Site", WHITEFIELD, {"photos": 2}))
    unknown = {}
    for title, own in (("Draft Site Role", draft), ("Shut Site Role", shut), ("Few Photos Role", few)):
        unknown[title] = await post_job(db, gym, title, branch=own)
    await post_job(db, gym, "Whole Gym Role")
    await post_job(db, gym, "Remote Draft Role", branch=draft, location="Remote")
    await post_job(db, gym, "Here Club Role", branch=club)

    for km in ("5", "10", "20", "30"):
        page = (await client.get(f"/jobs?{HERE}&km={km}")).text
        assert split_titles(page) == (["Here Club Role", "Whole Gym Role"], ["Remote Draft Role"]), km
        assert f"2 jobs within {km} km · 1 remote" in page, km
    everything = (await client.get(f"/jobs?{HERE}&view=all")).text
    assert chips(everything) == {"Here Club Role": "10 m away", "Remote Draft Role": None,
                                 "Whole Gym Role": "10 m away · nearest branch: Here Club",
                                 **{t: None for t in unknown}}  # listed, never with a (borrowed) distance
    for title, job in unknown.items():
        detail = (await client.get(f"/jobs/{job.id}?{HERE}")).text
        assert detail_distance(detail) is None and "from you" not in detail and "nearest branch" not in detail, title
        badge = {t: location_badge(card) for t, _, card, _ in job_cards(everything)}[title]
        assert badge == "Bengaluru", title  # the posting's own location text, never the unpublished branch


async def test_a_draft_branch_in_another_city_is_never_named_next_to_a_borrowed_distance(client, db):
    """Review finding: a posting tied to a DRAFT branch 'Mysuru Opening Soon' (Mysuru) was listed as near with the
    card reading 'Mysuru Opening Soon, Mysuru' next to '10 m away · nearest branch: Indiranagar Club', and the job
    page / its meta description published the draft branch's name and city. Product decision since: an unpublished
    own branch makes the posting's location unknown - it is never measured to a sister branch (so it is not "near"
    the Indiranagar visitor at all) and is listed under "All jobs" without a distance; the draft branch itself is
    never named anywhere - the posting's own location text ('Mysuru') is shown instead."""
    gym, (club, draft) = await gym_with(db, "Barbell Co", ("Indiranagar Club", INDIRANAGAR, {}),
                                        ("Mysuru Opening Soon", MYSURU, {"verified": False, "city": "Mysuru"}))
    launch = await post_job(db, gym, "Mysuru Launch Trainer", branch=draft, location="Mysuru")
    club_job = await post_job(db, gym, "Club Trainer", branch=club)

    near = (await client.get(f"/jobs?{HERE}&km=5")).text
    cards = {t: card for t, _, card, _ in job_cards(near)}
    assert list(cards) == ["Club Trainer"]  # the Mysuru posting no longer borrows the Indiranagar club's distance
    assert location_badge(cards["Club Trainer"]) == "Indiranagar Club, Bengaluru"  # a published branch is named
    assert chips(near) == {"Club Trainer": "10 m away"}
    everything = (await client.get(f"/jobs?{HERE}&view=all")).text
    all_cards = {t: card for t, _, card, _ in job_cards(everything)}
    assert location_badge(all_cards["Mysuru Launch Trainer"]) == "Mysuru"  # listed, with its own location text
    assert chips(everything) == {"Mysuru Launch Trainer": None, "Club Trainer": "10 m away"}
    for url in (f"/jobs?{HERE}&km=5", f"/jobs?{HERE}&view=all", "/jobs", f"/jobs/{launch.id}?{HERE}", f"/jobs/{launch.id}",
                f"/jobs/{club_job.id}?{HERE}"):
        page = (await client.get(url)).text
        assert "Mysuru Opening Soon" not in page, url  # not in the badge, the job page, its meta tags or "more roles"
    detail = (await client.get(f"/jobs/{launch.id}?{HERE}")).text
    assert re.search(r'<meta name="description" content="Mysuru Launch Trainer at Barbell Co \(Mysuru\)\.', detail)
    assert re.search(r">Location</dt>\s*<dd[^>]*>Mysuru</dd>", detail)
    more = (await client.get(f"/jobs/{club_job.id}?{HERE}")).text
    assert "Mysuru · Full time" in more  # "more roles at this gym": the posting's own location text as well


# ------------------------------------------------------------------ empty states
async def test_empty_near_me_states_link_to_30_km_and_all_jobs(client, db):
    await board(db)
    far = f"lat={FAR_AWAY[0]}&lon={FAR_AWAY[1]}"
    page = (await client.get(f"/jobs?{far}&km=10")).text  # only the remote block is left
    assert split_titles(page) == ([], ["Remote Nutritionist"])
    assert "No jobs within 10 km of you" in page and "The remote positions below work from anywhere." in page
    assert query_of(link(page, "Search within 30 km")) == {"lat": [str(FAR_AWAY[0])], "lon": [str(FAR_AWAY[1])]}
    assert query_of(link(page, "Show all jobs"))["view"] == ["all"]

    page = (await client.get(f"/jobs?{far}&km=10&remote=0&q=coach")).text  # nothing at all
    assert titles(page) == []
    assert "No jobs within 10 km of you" in page and "Nothing matches your search that close to you." in page
    widen = link(page, "Search within 30 km")
    assert query_of(widen) == {"q": ["coach"], "lat": [str(FAR_AWAY[0])], "lon": [str(FAR_AWAY[1])], "remote": ["0"]}
    everything = link(page, "Show all jobs")
    assert query_of(everything) == {"q": ["coach"], "lat": [str(FAR_AWAY[0])], "lon": [str(FAR_AWAY[1])],
                                    "view": ["all"], "km": ["10"], "remote": ["0"]}
    all_page = (await client.get(everything)).text
    assert titles(all_page) == ["Mysuru Coach", "Corner Coach", "Edge Coach 31", "Edge Coach 29", "Whitefield Coach"]
    assert all(c is None for c in chips(all_page).values())  # all of them are far from Mumbai

    page = (await client.get(f"/jobs?{far}&remote=0")).text  # already at the limit: only "Show all jobs"
    assert "No jobs within 30 km of you" in page and "Search within" not in page and "Show all jobs" in page


# ------------------------------------------------------------------ where the location comes from
async def test_signed_in_member_saved_location_is_the_default(client, db, anon_client_factory):
    await board(db)
    member = await make_customer(db, latitude=INDIRANAGAR[0] + 0.001, longitude=INDIRANAGAR[1])
    await login_as(client, member)
    page = (await client.get("/jobs")).text
    assert split_titles(page) == (NEAR, ["Remote Nutritionist"])
    assert "Showing jobs within 30 km of your saved location" in page
    assert "Clear location" not in page  # a saved location can't be cleared from the URL
    assert all("lat" not in query_of(href) for _, href, _, _ in job_cards(page))
    page = (await client.get(f"/jobs?lat={WHITEFIELD[0]}&lon={WHITEFIELD[1]}")).text  # a shared location wins
    assert titles(page)[0] == "Whitefield Coach" and "Showing jobs within 30 km of your location" in page

    city_only = await make_customer(db, city="Bengaluru")
    other = anon_client_factory("203.0.113.61")
    await login_as(other, city_only)
    page = (await other.get("/jobs")).text  # a city is not a point: newest first, no distances
    assert titles(page) == NEWEST and "Showing jobs" not in page and all(c is None for c in chips(page).values())


async def test_guest_without_location_sees_newest_first_and_the_use_my_location_control(client, db):
    await board(db)
    page = (await client.get("/jobs")).text
    assert titles(page) == NEWEST
    assert all(c is None for c in chips(page).values()) and "away" not in page
    panel = form_html(page, 'id="job-q"')
    assert 'role="search"' in panel and 'data-component="geo-form"' in panel  # the location submits the search
    assert re.search(r'<select id="job-role".*<select id="job-type".*<button type="button" class="btn btn-secondary[^"]*" '
                     r'data-component="geolocate"[^>]*>Use my location</button>', panel, re.S)
    assert hidden(panel) == {"lat": [""], "lon": [""]}
    assert "Use my location</span> to see the jobs within 30 km of you" in page
    for absent in ("Showing jobs", "Clear location", "Near me</a>", "All jobs</a>", 'name="km"', "Include remote jobs",
                   'id="remote-jobs"', "distance-sort"):
        assert absent not in page, absent
    assert "8 open positions" in page


@pytest.mark.parametrize("bad", ["lat=999&lon=abc", "lat=0&lon=0", "lat=nan&lon=77.6", "lat=1e400&lon=77.6",
                                 "lat=12.97&lon=", "lat=12.97&lon=181", f"lat={'1' * 40}&lon=77.6", "lat=-91&lon=10"])
async def test_invalid_coordinates_are_ignored(client, db, bad):
    await board(db)
    r = await client.get(f"/jobs?{bad}&km=5&remote=0&view=near")
    assert r.status_code == 200
    assert titles(r.text) == NEWEST
    assert "Showing jobs" not in r.text and "away" not in r.text and "Include remote jobs" not in r.text
    assert all("lat" not in query_of(href) for _, href, _, _ in job_cards(r.text))


@pytest.mark.parametrize("view", ["bogus", "ALL", "", "near", "all2", "<all>"])
async def test_unknown_view_means_near_me(client, db, view):
    await board(db)
    page = (await client.get(f"/jobs?{HERE}&view={view}")).text
    assert split_titles(page) == (NEAR, ["Remote Nutritionist"])


# ------------------------------------------------------------------ links + pager keep the whole search
async def test_pager_and_every_link_keep_the_search(client, db):
    gym, (branch,) = await gym_with(db, "Paging Gym", ("Paging Club", KORAMANGALA, {}))
    for i in range(12):
        await post_job(db, gym, f"Coach {i:02d}", branch=branch)
    await post_job(db, gym, "Desk Associate", role=EmployeeRole.RECEPTIONIST)
    await post_job(db, gym, "Remote Coach", location="Remote")
    search = f"q=coach&role=trainer&type=full_time&{HERE}&km=10&remote=0"
    expected = {"q": ["coach"], "role": ["trainer"], "type": ["full_time"], **HERE_Q, "km": ["10"], "remote": ["0"]}
    page = (await client.get(f"/jobs?{search}")).text
    assert titles(page) == [f"Coach {i:02d}" for i in range(11, 1, -1)]  # same distance: newest first
    assert "12 jobs within 10 km" in page and "Page <span" in page
    nxt = rel_link(page, "next")
    assert query_of(nxt) == {**expected, "page": ["2"]}
    page2 = (await client.get(nxt)).text
    assert titles(page2) == ["Coach 01", "Coach 00"]
    assert query_of(rel_link(page2, "prev")) == expected  # page 1 is implicit
    out_of_range = (await client.get(f"/jobs?{search}&page=99")).text  # clamped to the last page
    assert titles(out_of_range) == ["Coach 01", "Coach 00"] and re.search(r">2</span> of 2 · 12 positions", out_of_range)
    assert all(query_of(href) == expected for _, href, _, _ in job_cards(page))  # cards carry the search ...
    page2_cards = job_cards(page2)
    assert all(query_of(href) == {**expected, "page": ["2"]} for _, href, _, _ in page2_cards)  # ... and the page
    job_page = (await client.get(page2_cards[0][1])).text
    assert query_of(link(job_page, "Jobs")) == {**expected, "page": ["2"]}  # back to page 2 of this very list

    panel = form_html(page, 'id="job-q"')  # the search panel carries the near-me options
    assert hidden(panel) == {**HERE_Q, "km": ["10"], "remote": ["0"]}
    assert hidden(form_html(page, "Include remote jobs")) == {"q": ["coach"], "role": ["trainer"], "type": ["full_time"],
                                                             **HERE_Q, "km": ["10"], "remote": ["0"]}
    assert query_of(link(page, "All jobs")) == {**expected, "view": ["all"]}
    assert query_of(link(page, "20 km")) == {**expected, "km": ["20"]}
    assert query_of(link(page, "30 km")) == {k: v for k, v in expected.items() if k != "km"}
    assert query_of(link(page, "Clear location")) == {"q": ["coach"], "role": ["trainer"], "type": ["full_time"]}
    assert query_of(link(page, "Clear filters")) == {**HERE_Q, "km": ["10"], "remote": ["0"]}

    all_page = (await client.get(f"/jobs?{search}&view=all")).text  # "All jobs" keeps the options for the way back
    assert query_of(rel_link(all_page, "next")) == {**expected, "view": ["all"], "page": ["2"]}
    assert hidden(form_html(all_page, 'id="job-q"')) == {**HERE_Q, "view": ["all"], "km": ["10"], "remote": ["0"]}
    assert query_of(link(all_page, "Near me")) == expected


# ------------------------------------------------------------------ job page
async def test_job_page_shows_about_distance_only_within_the_limit(client, db):
    jobs, ctx = await board(db)
    page = (await client.get(f"/jobs/{jobs['A2'].id}?{HERE}")).text
    assert detail_distance(page) == "About 4.5 km from you · nearest branch: Koramangala Hub"
    links = hrefs(page)
    assert f"/jobs?{HERE}" in links  # back to the list with the location
    assert f"/jobs/{jobs['A1'].id}?{HERE}" in links and f"/gyms/{ctx['gym_a'].slug}?{HERE}" in links
    more = page[page.index('id="more-roles"'):page.index("</section>", page.index('id="more-roles"'))]
    assert "Whitefield Coach" in more and " km" not in more and "away" not in more  # "more roles": no distances
    assert f'action="/jobs/{jobs["A2"].id}/enquire?lat={INDIRANAGAR[0]}&amp;lon={INDIRANAGAR[1]}"' in page

    assert detail_distance((await client.get(f"/jobs/{jobs['A1'].id}?{HERE}")).text) == "About 12 km from you"
    assert detail_distance((await client.get(f"/jobs/{jobs['E29'].id}?{HERE}")).text) == "About 29 km from you"
    assert detail_distance((await client.get(f"/jobs/{jobs['B1'].id}?{HERE}")).text) == "About 10 m from you"
    for key in ("E31", "C35", "M", "R"):  # beyond 30 km (or remote): no distance at all
        page = (await client.get(f"/jobs/{jobs[key].id}?{HERE}")).text
        assert page.count("data-job-distance") == 0 and "from you" not in page and "away" not in page, key
        assert_within_limit(page)
    page = (await client.get(f"/jobs/{jobs['A2'].id}")).text  # no location: no distance, plain links
    assert detail_distance(page) is None and "/jobs?lat" not in page and f'href="/jobs/{jobs["A1"].id}"' in page


def filled_in(job_id: int, data: dict) -> dict:
    """An enquiry form as a person sends it: with the token its page issued, a minute earlier."""
    return {**data, ENQUIRY_TOKEN_FIELD: enquiry_form_token(job_id, issued=time.time() - 60)}


async def test_enquiry_keeps_the_location(client, db):
    jobs, _ = await board(db)
    a2 = jobs["A2"].id
    bad = await post_form(client, f"/jobs/{a2}/enquire?{HERE}", {"name": "A", "email": "x", "message": "short"})
    assert bad.status_code == 400 and detail_distance(bad.text) == "About 4.5 km from you · nearest branch: Koramangala Hub"
    ok = await post_form(client, f"/jobs/{a2}/enquire?{HERE}", filled_in(a2, {
        "name": "Asha Rao", "email": "asha@example.com", "message": "Certified coach, 5 years on the floor."}))
    assert ok.status_code == 303 and ok.headers["location"] == f"/jobs/{a2}?sent=1&{HERE}#enquire"
    sent = await client.get(ok.headers["location"])
    assert "Enquiry sent" in sent.text and f"/jobs?{HERE}" in hrefs(sent.text)


async def test_job_page_and_enquiry_lead_back_to_the_exact_list(client, db):
    """Review finding: job links carried only lat/lon, so from "All jobs" (or a radius / remote / role filter) the
    job page's "Jobs" breadcrumb and "Browse more jobs" led to plain "Near me" - which hides the far job just viewed."""
    await board(db)
    search = f"q=coach&role=trainer&{HERE}&view=all&km=10&remote=0"
    board_q = {"q": ["coach"], "role": ["trainer"], **HERE_Q, "view": ["all"], "km": ["10"], "remote": ["0"]}
    listing = (await client.get(f"/jobs?{search}")).text
    cards = {title: href for title, href, _, _ in job_cards(listing)}
    assert list(cards) == ["Mysuru Coach", "Corner Coach", "Edge Coach 31", "Edge Coach 29", "Whitefield Coach"]
    assert all(query_of(href) == board_q for href in cards.values())

    far = (await client.get(cards["Edge Coach 31"])).text  # 31 km away: only "All jobs" lists it
    back = link(far, "Jobs")
    assert urlsplit(back).path == "/jobs" and query_of(back) == board_q
    assert "Edge Coach 31" in titles((await client.get(back)).text)  # the breadcrumb returns to the same list
    more = far[far.index('id="more-roles"'):]
    more_links = re.findall(r'<a href="(/jobs/\d+[^"]*)" class="card-flat card-hover block">', more)
    assert more_links and all(query_of(htmllib.unescape(h)) == board_q for h in more_links)
    action = htmllib.unescape(re.search(r'<form method="post" action="([^"]+)"', far).group(1))
    assert query_of(action) == board_q

    far_id = int(re.search(r"/jobs/(\d+)/enquire", action).group(1))
    ok = await post_form(client, action, filled_in(far_id, {"name": "Asha Rao", "email": "asha@example.com",
                                                            "message": "Certified coach, 5 years on the floor."}))
    assert ok.status_code == 303 and query_of(ok.headers["location"]) == {"sent": ["1"], **board_q}
    sent = (await client.get(ok.headers["location"])).text
    assert "Enquiry sent" in sent and query_of(link(sent, "Browse more jobs")) == board_q
    assert query_of(link(sent, "Send another enquiry")) == board_q

    unsafe = (await client.get(cards["Edge Coach 31"].replace("q=coach", "q=%22%3E%3Cscript%3Ealert(1)%3C%2Fscript%3E"))).text
    assert "<script>alert(1)" not in unsafe and query_of(link(unsafe, "Jobs"))["q"] == ['"><script>alert(1)</script>']


# ------------------------------------------------------------------ safety
async def test_location_line_and_chips_are_xss_safe(client, db):
    gym, (_branch,) = await gym_with(db, "Xss Gym", ('<img src=x onerror="alert(1)">', KORAMANGALA, {}))
    job = await post_job(db, gym, "Plain Role")
    payload = '"><script>alert(1)</script>'
    r = await client.get("/jobs", params={"q": "plain", "role": payload, "type": payload, "lat": str(INDIRANAGAR[0]),
                                          "lon": str(INDIRANAGAR[1]), "view": payload, "km": payload, "remote": payload})
    page = r.text
    assert r.status_code == 200
    assert "<script>alert(1)" not in page and '<img src=x onerror="alert(1)">' not in page
    assert "nearest branch: &lt;img src=x onerror=&#34;alert(1)&#34;&gt;" in page
    assert re.search(r"<span data-location-line>Showing jobs within 30 km of your location</span>", page)
    r = await client.get("/jobs", params={"q": payload, "lat": str(INDIRANAGAR[0]), "lon": str(INDIRANAGAR[1])})
    page = r.text
    assert "<script>alert(1)" not in page
    assert link(page, "Clear location") == "/jobs?q=%22%3E%3Cscript%3Ealert%281%29%3C%2Fscript%3E"
    assert '<input type="hidden" name="q" value="&#34;&gt;&lt;script&gt;alert(1)&lt;/script&gt;">' in page
    detail = (await client.get(f"/jobs/{job.id}?{HERE}")).text
    assert '<img src=x onerror="alert(1)">' not in detail and "nearest branch: &lt;img src=x" in detail


async def test_pages_stay_csp_clean(client, db):
    jobs, _ = await board(db)
    for url in (f"/jobs?{HERE}", f"/jobs?{HERE}&view=all", f"/jobs?lat={FAR_AWAY[0]}&lon={FAR_AWAY[1]}&remote=0",
                "/jobs", f"/jobs/{jobs['A2'].id}?{HERE}"):
        page = (await client.get(url)).text
        assert not re.search(r"\sstyle\s*=", page), url
        assert not re.search(r"\son[a-z]+\s*=", page), url
        for tag in re.findall(r"<script\b[^>]*>", page):
            assert "nonce=" in tag, (url, tag)


# ------------------------------------------------------------------ performance: one statement, measured once
async def test_query_budget_is_exact(client, db, anon_client_factory):
    gym0, (b0,) = await gym_with(db, "Count Gym 0", ("Count Club 0", INDIRANAGAR, {}))
    first = await post_job(db, gym0, "Count Role 0", branch=b0)
    member = await make_customer(db, latitude=INDIRANAGAR[0], longitude=INDIRANAGAR[1])
    signed_in = anon_client_factory("203.0.113.90")
    await login_as(signed_in, member)
    urls = {
        "near": (client, f"/jobs?{HERE}"),
        "near, filtered": (client, f"/jobs?{HERE}&km=10&remote=0&q=count&role=trainer"),
        "near, page 2": (client, f"/jobs?{HERE}&page=2"),
        "near, page out of range": (client, f"/jobs?{HERE}&page=999"),
        "all jobs": (client, f"/jobs?{HERE}&view=all"),
        "no location": (client, "/jobs"),
        "signed in (saved location)": (signed_in, "/jobs"),
        "job page": (client, f"/jobs/{first.id}?{HERE}"),
    }

    async def measure() -> dict[str, int]:
        out = {}
        for name, (c, url) in urls.items():
            with QueryCounter() as qc:
                r = await c.get(url)
            assert r.status_code == 200, url
            out[name] = qc.count
        return out

    expected = {name: GUEST_STATEMENTS for name in urls}
    expected["signed in (saved location)"] = GUEST_STATEMENTS + SESSION_STATEMENTS
    expected["job page"] = DETAIL_STATEMENTS
    assert await measure() == expected  # 1 posting

    for i in range(1, 7):  # 6 gyms x 6 postings: own / no / draft branches, sister branches 22-32 km away, remote
        gym, (own, other, draft) = await gym_with(
            db, f"Count Gym {i}", (f"Count Club {i}", KORAMANGALA, {}), (f"Count Far {i}", offset(INDIRANAGAR, 20 + i * 2), {}),
            (f"Count Draft {i}", WHITEFIELD, {"verified": False}))
        for j, branch in enumerate((own, None, draft, other, own)):
            await post_job(db, gym, f"Count Role {i}.{j}", branch=branch)
        await post_job(db, gym, f"Count Remote {i}", location="Remote")
    await post_job(db, gym0, "Count Sibling", branch=b0)
    await post_job(db, gym0, "Count Sibling Remote", location="Work from home")
    await post_job(db, gym0, "Count No Branch")
    assert await db.scalar(select(func.count(JobPosting.id))) == 40
    assert await measure() == expected  # 40 postings: the very same statement count

    page = (await client.get(f"/jobs?{HERE}")).text
    assert len(titles(page)) == 10 and re.search(r">1</span> of \d+ · \d+ positions", page)
    assert_within_limit(page)


async def statements_of(client, url: str) -> list[tuple[str, object]]:
    """The SQL statements (+ parameters) one request executes."""
    captured: list[tuple[str, object]] = []

    def grab(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        captured.append((statement, parameters))

    engine = dbmod.get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", grab)
    try:
        r = await client.get(url)
    finally:
        event.remove(engine, "before_cursor_execute", grab)
    assert r.status_code == 200, url
    return captured


async def test_far_distances_never_leave_the_database(client, db):
    jobs, _ = await board(db)
    for url, nulls in ((f"/jobs?{HERE}", 1), (f"/jobs?{HERE}&view=all", 4), (f"/jobs/{jobs['C35'].id}?{HERE}", 1),
                       (f"/jobs/{jobs['E31'].id}?{HERE}", 1), (f"/jobs/{jobs['A1'].id}?{HERE}", 0)):
        executed = await statements_of(client, url)
        statement, params = executed[0]  # the listing / the posting itself: its distance comes with it
        assert "near_branches" in statement and all("asin(" not in s for s, _ in executed[1:]), url  # "more roles": none
        async with dbmod.get_engine().connect() as conn:
            result = await conn.exec_driver_sql(statement, params)
            keys, rows = list(result.keys()), result.all()
        km = [row[keys.index("km")] for row in rows]
        assert all(v is None or 0 <= v <= 30 for v in km), (url, km)
        assert sum(v is None for v in km) == nulls, (url, km)  # remote / beyond 30 km: NULL straight from SQL


async def explain_analyze(statement: str, params: object) -> list[tuple[dict, tuple[str, ...]]]:
    """EXPLAIN (ANALYZE, VERBOSE) of a captured statement: every plan node with the CTE / subplan names it sits in."""
    async with dbmod.get_engine().connect() as conn:
        plan = (await conn.exec_driver_sql("EXPLAIN (ANALYZE, VERBOSE, FORMAT JSON) " + statement, params)).scalar()
    plan = json.loads(plan) if isinstance(plan, str) else plan

    def walk(node: dict, inside: tuple[str, ...]):
        here = inside + ((node["Subplan Name"],) if "Subplan Name" in node else ())
        yield node, here
        for child in node.get("Plans", []):
            yield from walk(child, here)

    return list(walk(plan[0]["Plan"], ()))


def own_text(node: dict) -> str:
    """A plan node's own expressions (output, conditions ...), without its children."""
    return json.dumps({k: v for k, v in node.items() if k != "Plans"})


def box_scans(nodes) -> list[tuple[dict, tuple[str, ...]]]:
    """Scans of ``branches`` that apply the latitude / longitude bounding box."""
    return [(n, where) for n, where in nodes if n.get("Relation Name") == "branches"
            and all(f"{col} {op}" in own_text(n) for col in ("latitude", "longitude") for op in (">=", "<="))]


async def test_one_statement_haversine_once_per_branch_and_a_window_count(client, db):
    """The total is a window function (no COUNT query), and the box + haversine run once per request - in the
    MATERIALIZED CTE near_branches - not once per posting (review finding: they sat in a per-posting LATERAL).

    Only the postings that can be listed are placed: remote ones (unless hidden) and those of a gym with a published
    branch in the radius' box (pagination audit: the near view placed and sorted every open posting of the platform).
    At 30 km that leaves out Mysuru Coach (8 matching -> 7 placed); at 5 km with remote=0 and role=trainer only Beta's
    and Alpha's trainers (6 matching -> 2): Whitefield Coach is placed (Alpha has Koramangala in the box) and then
    dropped by the radius filter, the Edge and Mysuru postings never reach the CTE."""
    await board(db)
    for url, matching in ((f"/jobs?{HERE}", 7), (f"/jobs?{HERE}&km=5&remote=0&role=trainer", 2)):
        captured = await statements_of(client, url)
        assert len(captured) == GUEST_STATEMENTS
        statement, params = captured[0]
        assert "count(*) OVER ()" in statement and "LATERAL" not in statement
        assert statement.count("asin(") == 1  # the haversine is written once ...
        assert "near_branches AS MATERIALIZED" in statement and "placed_jobs AS MATERIALIZED" in statement
        nodes = await explain_analyze(statement, params)
        windows = [o for n, _ in nodes if n["Node Type"] == "WindowAgg" for o in n.get("Output", [])]
        assert any(o.startswith("count(*) OVER") for o in windows), windows
        haversine = [(n, where) for n, where in nodes if "asin(" in own_text(n)]
        assert haversine and all(where[:1] == ("CTE near_branches",) for _, where in haversine), url  # ... computed
        assert all(n["Actual Loops"] == 1 for n, _ in haversine), url  # in the CTE, which runs once per request
        (scan, where), = box_scans(nodes)  # one box scan of branches per request ...
        assert where[:1] == ("CTE near_branches",) and scan["Actual Loops"] == 1, url  # ... executed once
        place = next(n for n, where in nodes if where == ("CTE placed_jobs",))
        assert place["Actual Loops"] == 1 and place["Actual Rows"] == matching, url  # each candidate posting, once


async def test_box_scan_is_one_lat_lon_index_range_scan_per_request(client, db):
    """With a realistic branches table (3,000 verified branches elsewhere in the world, ANALYZEd) the request's one
    box scan uses ix_branches_lat_lon - once, not once per posting - at the default radius and at the small ones."""
    from app.routers.public import radius_box

    _, ctx = await board(db)
    await db.execute(text(
        "INSERT INTO branches (gym_id, name, address_line1, city, country_code, latitude, longitude, verification_status) "
        "SELECT :gym, 'Elsewhere ' || g, 'Somewhere', 'Elsewhere', 'IN', lat, lon, 'verified' FROM ("
        "  SELECT g, -60 + (g * 37 % 1200) / 10.0 AS lat, -179 + (g * 53 % 3580) / 10.0 AS lon"
        "  FROM generate_series(1, 3000) AS g) AS spread "
        "WHERE NOT (lat BETWEEN 11 AND 15 AND lon BETWEEN 76 AND 79)"), {"gym": ctx["gym_a"].id})
    await db.commit()
    await db.execute(text("ANALYZE branches"))
    await db.commit()
    index = await db.scalar(text(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'branches' AND indexname = 'ix_branches_lat_lon'"))
    assert index and index.endswith("(latitude, longitude)")
    box = radius_box(*INDIRANAGAR, 30)
    assert await db.scalar(text(
        "SELECT count(*) FROM branches WHERE latitude BETWEEN :a AND :b AND longitude BETWEEN :c AND :d"),
        {"a": box.lat_min, "b": box.lat_max, "c": box.lon_min, "d": box.lon_max}) == 6  # the board's, none added

    for url, near in ((f"/jobs?{HERE}", NEAR), (f"/jobs?{HERE}&km=5", NEAR[:2]), (f"/jobs?{HERE}&km=10", NEAR[:2])):
        assert split_titles((await client.get(url)).text) == (near, ["Remote Nutritionist"]), url
        statement, params = (await statements_of(client, url))[0]
        nodes = await explain_analyze(statement, params)
        lat_lon = [(n, where) for n, where in nodes if n.get("Index Name") == "ix_branches_lat_lon"]
        assert lat_lon, url  # the box is an index range scan ...
        assert all(where[:1] == ("CTE near_branches",) and n["Actual Loops"] == 1 for n, where in lat_lon), url  # once
        assert all("latitude >=" in n["Index Cond"] and "longitude <=" in n["Index Cond"] for n, _ in lat_lon), url
        (scan, where), = box_scans(nodes)
        assert where[:1] == ("CTE near_branches",) and scan["Actual Loops"] == 1, url


async def test_all_jobs_reads_the_open_postings_from_their_partial_index(client, db):
    """The "All jobs" list (newest first) over the OPEN postings only - the board's own predicate ``is_open IS true`` -
    reads the partial index ix_job_postings_open_recent (created_at DESC, id DESC) INCLUDE (gym_id) - an index-only
    scan: the closed postings, which pile up over the years (here 9 in 10 of 20,000), are never read, and whatever
    the plan sorts is the open postings at most (the window's total counts all of them anyway). Same single statement,
    with and without a location."""
    gym, (club,) = await gym_with(db, "Index Gym", ("Index Club", INDIRANAGAR, {}))
    await db.execute(text(
        "INSERT INTO job_postings (gym_id, branch_id, title, role, employment_type, location, currency, requirements,"
        " is_open, created_at) "
        "SELECT :gym, :branch, 'Posting ' || g, 'trainer', 'full_time', 'Bengaluru', 'INR', '- Certified coach',"
        " g % 10 = 0, now() - make_interval(mins => g) FROM generate_series(1, 20000) AS g"),
        {"gym": gym.id, "branch": club.id})
    await db.commit()
    async with dbmod.get_engine().connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.exec_driver_sql("VACUUM ANALYZE job_postings")
    for url in ("/jobs", "/jobs?page=150", f"/jobs?{HERE}&view=all"):
        captured = await statements_of(client, url)
        assert len(captured) == GUEST_STATEMENTS, url
        statement, params = captured[0]
        nodes = [n for n, _ in await explain_analyze(statement, params)]
        postings = [n for n in nodes if n.get("Relation Name") == "job_postings"]
        ranked = [n for n in postings if n.get("Index Name") == "ix_job_postings_open_recent"]
        assert ranked and all(n["Node Type"] != "Seq Scan" for n in postings), [(n["Node Type"], n.get("Index Name"))
                                                                                  for n in postings]
        assert sum(n["Actual Rows"] * n["Actual Loops"] for n in ranked) == 2000, url  # the open ones only
        sorted_rows = [n["Actual Rows"] * n["Actual Loops"] for n in nodes if n["Node Type"] in ("Sort", "Incremental Sort")]
        assert all(rows <= 2000 for rows in sorted_rows), (url, sorted_rows)  # never the closed history
