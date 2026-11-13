"""Pagination audit - the public site loads bounded data whatever the size of the platform or of a chain:

* home: the location-less featured block, the trainer teaser and the site figures come from an in-process TTL cache
  (cleared when this process commits a change to what they show); no unused COUNT; with a location, one box scan;
* gym cards: per-gym aggregates computed in SQL - a 150-branch chain loads no branch rows at all;
* /gyms near me: the gyms with a branch inside the box first (nearest first), the rest A-Z; the city filter: the
  busiest cities, cached and capped;
* gym page: 12 branch cards + "View all N locations" -> /gyms/{slug}/branches?page= (12 per page); branch page: its
  own branch by id + the 6 nearest sisters.
Every list below is seeded with 150 rows (one INSERT ... SELECT) and checked to load only one page, with LIMIT in SQL.
"""

from __future__ import annotations

import re
from contextlib import contextmanager

from sqlalchemy import event, func, select, text

from app import db as dbmod
from app.db import QueryCounter
from app.models import Branch, BranchPhoto, Gym
from tests.conftest import login_as
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
)

KM_PER_DEGREE = 111.195  # the app's haversine: along a meridian, 1 degree of latitude


# ------------------------------------------------------------------ helpers
async def bulk_branches(db, gym, n: int, *, cities=("Bengaluru",), origin=INDIRANAGAR, step_km: float = 0.05,
                        photos: int = 3, features: int = 0, prefix: str = "Site") -> None:
    """``n`` PUBLISHED branches of ``gym`` in a few INSERT ... SELECTs: branch g is g * step_km due north of
    ``origin``, in ``cities[(g - 1) % len(cities)]``, with ``photos`` photos, opening hours and ``features`` features."""
    await db.execute(text(
        "INSERT INTO branches (gym_id, name, address_line1, city, country_code, latitude, longitude, timezone, "
        "verification_status, verified_at, is_open) "
        "SELECT :gym, :prefix || ' ' || lpad(g::text, 4, '0'), g || ' Main Road', "
        "(CAST(:cities AS text[]))[1 + (g - 1) % :nc], 'IN', "
        "CAST(:lat AS double precision) + g * CAST(:dlat AS double precision), CAST(:lon AS double precision), "
        "'Asia/Kolkata', 'verified', now(), true FROM generate_series(1, :n) AS g"),
        {"gym": gym.id, "prefix": prefix, "cities": list(cities), "nc": len(cities), "lat": origin[0],
         "dlat": step_km / KM_PER_DEGREE, "lon": origin[1], "n": n})
    params = {"gym": gym.id, "like": prefix + " %", "photos": photos, "features": features}
    mine = "WHERE b.gym_id = :gym AND b.name LIKE :like"
    await db.execute(text(
        "INSERT INTO branch_photos (branch_id, image_key, sort_order) "
        "SELECT b.id, 'public/branch/' || md5(b.id::text || '-' || p), p "
        f"FROM branches b CROSS JOIN generate_series(1, :photos) AS p {mine}"), params)
    await db.execute(text(
        "INSERT INTO branch_hours (branch_id, weekday, opens_at, closes_at) SELECT b.id, d, '06:00', '20:00' "
        f"FROM branches b CROSS JOIN generate_series(0, 6) AS d {mine}"), params)
    await db.execute(text(
        "INSERT INTO branch_features (branch_id, name) SELECT b.id, 'Feature ' || f "
        f"FROM branches b CROSS JOIN generate_series(1, :features) AS f {mine}"), params)
    await db.commit()


async def chain(db, name: str, n: int, **kw) -> Gym:
    gym = await make_gym(db, await make_owner(db), name=name, online=False, offline=True)
    await make_plans(db, gym)
    await bulk_branches(db, gym, n, **kw)
    return gym


@contextmanager
def loaded(*models):
    """Count the ORM instances of ``models`` loaded from rows while the block runs."""
    counts = {m: 0 for m in models}
    listeners = []
    for model in models:
        def on_load(target, context, model=model):  # noqa: ANN001
            counts[model] += 1

        event.listen(model, "load", on_load)
        listeners.append((model, on_load))
    try:
        yield counts
    finally:
        for model, fn in listeners:
            event.remove(model, "load", fn)


async def statements_of(client, url: str) -> list[str]:
    captured: list[str] = []

    def grab(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        captured.append(statement)

    engine = dbmod.get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", grab)
    try:
        r = await client.get(url)
    finally:
        event.remove(engine, "before_cursor_execute", grab)
    assert r.status_code == 200, url
    return captured


def cards_in(html: str) -> int:
    return html.count('class="pub-gym-card group"')


def card_of(html: str, name: str) -> str:
    """The gym card (home / directory) showing ``name``."""
    cards = html.split('class="pub-gym-card group"')[1:]
    found = [c[:c.index("</a>")] for c in cards if f">{name}</h3>" in c[:c.index("</a>")]]
    assert len(found) == 1, name
    return found[0]


def branch_cards_in(html: str) -> int:
    return html.count('class="pub-gym-card group sm:flex-row"')


def at(north_km: float, point=INDIRANAGAR) -> tuple[float, float]:
    return point[0] + north_km / KM_PER_DEGREE, point[1]


# ------------------------------------------------------------------ gym cards: per-gym aggregates
async def test_gym_cards_carry_per_gym_aggregates_never_the_chains_branch_rows(client, db):
    await chain(db, "Mega Chain", 150, cities=("Agra", "Bengaluru", "Chennai", "Delhi", "Erode"))
    small = await make_gym(db, await make_owner(db), name="Solo Gym", online=False)
    await make_branch(db, small, coords=WHITEFIELD, city="Whitefield")
    here = f"lat={INDIRANAGAR[0]}&lon={INDIRANAGAR[1]}"
    for url in ("/gyms", f"/gyms?{here}", "/"):
        with loaded(Branch, BranchPhoto) as rows, QueryCounter() as qc:
            r = await client.get(url)
        assert r.status_code == 200 and cards_in(r.text) == 2, url
        assert rows == {Branch: 0, BranchPhoto: 0}, url  # aggregates only: no branch or photo row reaches Python
        mega = card_of(r.text, "Mega Chain")
        assert "150 verified branches" in mega, url
        assert all(c in mega for c in ("Agra", "Bengaluru", "Chennai")) and "+2 more" in mega and "Delhi" not in mega, url
        assert qc.count < 12, (url, qc.count)
    near = (await client.get(f"/gyms?{here}")).text
    assert near.index("Mega Chain") < near.index("Solo Gym")  # nearest branch 50 m away vs ~12 km
    assert "50 m" in near  # the card's distance: the chain's nearest branch, computed in SQL


async def test_directory_and_home_statement_counts_do_not_grow_with_a_chain(client, db):
    gym = await chain(db, "Growing Chain", 2, cities=("Bengaluru", "Mysuru"))
    here = f"lat={INDIRANAGAR[0]}&lon={INDIRANAGAR[1]}"

    async def counts() -> tuple[int, ...]:
        from app.core.state import reset_all

        out = []
        for url in ("/gyms", f"/gyms?{here}", "/", f"/?{here}"):
            await reset_all()  # cold caches: the statements behind the cached blocks are counted too
            with QueryCounter() as qc:
                assert (await client.get(url)).status_code == 200
            out.append(qc.count)
        return tuple(out)

    small = await counts()
    await bulk_branches(db, gym, 148, cities=("Agra", "Bengaluru", "Chennai", "Delhi"), prefix="More")
    assert await counts() == small


# ------------------------------------------------------------------ home: cached blocks, no unused COUNT, near me
async def test_home_blocks_are_cached_and_cleared_when_a_gym_changes(client, db):
    gym = await chain(db, "Cached Chain", 3)
    await make_employee(db, gym)
    cold = await statements_of(client, "/")
    assert cold and not any(re.match(r"SELECT count\(gyms\.id\)", s) for s in cold)  # the unused total is skipped
    with QueryCounter() as warm:
        r = await client.get("/")
    assert warm.count == 0 and "Cached Chain" in r.text  # featured gyms, trainers and figures: all from memory
    with QueryCounter() as about:
        assert (await client.get("/about")).status_code == 200
    assert about.count == 0  # /about shares the cached figures

    newcomer = await make_gym(db, await make_owner(db), name="Brand New Studio", online=True, offline=False)  # commit
    r = await client.get("/")
    assert "Brand New Studio" in r.text  # this process cleared its blocks on commit: no stale home page
    newcomer.name = "Renamed Studio"
    await db.commit()
    assert "Renamed Studio" in (await client.get("/")).text


async def test_home_with_a_location_lists_nearby_gyms_then_the_cached_block(client, db):
    near = await chain(db, "Neighbourhood Gym", 1, origin=KORAMANGALA, step_km=0.001)
    far = await make_gym(db, await make_owner(db), name="Mysuru Iron", online=False)
    await make_branch(db, far, coords=MYSURU, city="Mysuru")
    await make_gym(db, await make_owner(db), name="Cloud Coaching", online=True, offline=False)
    url = f"/?lat={KORAMANGALA[0]}&lon={KORAMANGALA[1]}"
    statements = await statements_of(client, url)
    page = (await client.get(url)).text
    assert page.index("Neighbourhood Gym") < page.index("Mysuru Iron") and "Cloud Coaching" in page
    assert "10 m" in page  # the nearby card shows the visitor's distance; the others come from the shared block
    assert any("near_branches AS MATERIALIZED" in s and "latitude BETWEEN" in s for s in statements)
    assert near.id


# ------------------------------------------------------------------ /gyms: near me and the city filter
async def test_directory_near_me_lists_the_box_nearest_first_then_the_rest_a_to_z(client, db):
    for name, coords, city in (("Zulu Near", KORAMANGALA, "Bengaluru"), ("Yankee Here", INDIRANAGAR, "Bengaluru"),
                               ("Alpha Far", MYSURU, "Mysuru")):
        gym = await make_gym(db, await make_owner(db), name=name, online=False)
        await make_branch(db, gym, coords=coords, city=city)
    await make_gym(db, await make_owner(db), name="Bravo Online", online=True, offline=False)
    url = f"/gyms?lat={INDIRANAGAR[0]}&lon={INDIRANAGAR[1]}"
    page = (await client.get(url)).text
    order = [page.index(n) for n in ("Yankee Here", "Zulu Near", "Alpha Far", "Bravo Online")]
    assert order == sorted(order)  # within 100 km nearest first, then A-Z (Mysuru is ~132 km away)
    assert "132 km" in card_of(page, "Alpha Far")  # a far card still shows its distance (measured for this page only)
    statements = await statements_of(client, url)
    listing = next(s for s in statements if "near_gyms" in s)
    assert "near_branches AS MATERIALIZED" in listing and listing.count("latitude BETWEEN") == 1


async def test_city_filter_lists_the_busiest_cities_cached_and_capped(client, db, monkeypatch):
    from app.routers import public

    monkeypatch.setattr(public, "DIRECTORY_CITIES", 3)
    sizes = {"Ajmer": 5, "Bhopal": 4, "Cuttack": 3, "Dehradun": 2, "Erode": 1}
    gym = await make_gym(db, await make_owner(db), name="Many Cities", online=False)
    for city, n in sizes.items():
        await bulk_branches(db, gym, n, cities=(city,), prefix=city)
    first = await statements_of(client, "/gyms")
    page = (await client.get("/gyms")).text
    options = re.findall(r'<option value="([^"]+)"', page[page.index('id="gym-city"'):page.index("</select>", page.index('id="gym-city"'))])
    assert options == ["Ajmer", "Bhopal", "Cuttack"]  # the 3 busiest, A-Z - never every city of the platform
    second = await statements_of(client, "/gyms")
    city_sql = "GROUP BY lower(trim(branches.city))"
    assert sum(city_sql in s for s in first) == 1 and sum(city_sql in s for s in second) == 0  # cached
    assert "LIMIT" in next(s for s in first if city_sql in s)
    picked = (await client.get("/gyms?city=Erode")).text  # a city outside the list still filters ...
    assert cards_in(picked) == 1 and '<option value="Erode" selected>' in picked  # ... and shows as selected


# ------------------------------------------------------------------ gym page + all locations + branch page
async def test_gym_page_shows_one_page_of_branch_cards_and_links_to_every_location(client, db):
    gym = await chain(db, "Big Box Fitness", 150, cities=("Agra", "Bengaluru"), features=7)
    here = f"lat={INDIRANAGAR[0]}&lon={INDIRANAGAR[1]}"
    with loaded(Branch, BranchPhoto) as rows:
        r = await client.get(f"/gyms/{gym.slug}?{here}")
    html = r.text
    assert r.status_code == 200 and branch_cards_in(html) == 12
    assert rows[Branch] == 12 and rows[BranchPhoto] == 0  # one page of branches; photos as plain data (<= 5 each)
    assert "150 verified branches" in html and "showing 12" in html
    assert f'href="/gyms/{gym.slug}/branches?{here.replace("&", "&amp;")}"' in html and "View all 150 locations" in html
    assert html.index("Site 0001") < html.index("Site 0002")  # nearest first (branch g is g x 50 m north)
    assert "Site 0013" not in html
    assert html.count('<span class="chip">Feature ') == 12 * 5 and html.count('<span class="chip">+2</span>') == 12
    assert "In-person plans are available to you" in html  # the 30 km rule: the nearest branch (first card)


async def test_gym_page_city_fallback_covers_cities_beyond_the_first_cards(client, db):
    gym = await chain(db, "Northern Chain", 20, cities=("Aizawl",))
    await bulk_branches(db, gym, 1, cities=("Mysuru",), origin=MYSURU, prefix="Zeta")
    member = await make_customer(db, city="Mysore")  # an alias of Mysuru, no coordinates
    await login_as(client, member)
    html = (await client.get(f"/gyms/{gym.slug}")).text
    assert branch_cards_in(html) == 12 and "Zeta 0001" not in html  # the Mysuru branch is not on the first page ...
    assert "A branch is in Mysuru" in html and f"/subscribe/{gym.slug}?plan=offline&amp;branch=" in html  # ... yet counts


async def test_all_locations_page_is_paginated_in_sql(client, db):
    gym = await chain(db, "Paged Chain", 150)
    here = f"lat={INDIRANAGAR[0]}&lon={INDIRANAGAR[1]}"
    with loaded(Branch) as rows:
        first = await client.get(f"/gyms/{gym.slug}/branches?{here}")
    assert first.status_code == 200 and branch_cards_in(first.text) == 12 and rows[Branch] == 12
    assert "Site 0001" in first.text and "Site 0013" not in first.text
    nxt = re.search(r'<a href="([^"]+)" class="btn btn-secondary btn-sm" rel="next"', first.text).group(1)
    assert "page=2" in nxt and "lat=" in nxt and "lon=" in nxt  # the pager keeps the location
    assert len(re.findall(r'aria-label="Page \d+"', first.text)) <= 9  # a window of page links, never 13
    statements = await statements_of(client, f"/gyms/{gym.slug}/branches?{here}&page=13")
    page_sql = next(s for s in statements if "count(*) OVER ()" in s)
    assert "LIMIT" in page_sql and "OFFSET" in page_sql  # one page in SQL, never the chain sliced in Python
    last = (await client.get(f"/gyms/{gym.slug}/branches?{here}&page=13")).text
    assert branch_cards_in(last) == 6 and "Site 0150" in last
    assert branch_cards_in((await client.get(f"/gyms/{gym.slug}/branches?page=999")).text) == 6  # past the end
    assert (await client.get("/gyms/no-such-gym/branches")).status_code == 404
    online_only = await make_gym(db, await make_owner(db), name="No Sites", online=True, offline=False)
    assert (await client.get(f"/gyms/{online_only.slug}/branches")).status_code == 404

    async def count(url: str) -> int:
        with QueryCounter() as qc:
            assert (await client.get(url)).status_code == 200
        return qc.count

    assert await count(f"/gyms/{gym.slug}/branches?page=2") == await count(f"/gyms/{gym.slug}/branches?page=7")


async def test_branch_page_loads_its_branch_and_the_six_nearest_sisters(client, db):
    gym = await chain(db, "Sister Chain", 150, step_km=0.2)
    home = await db.scalar(select(Branch).where(Branch.gym_id == gym.id, Branch.name == "Site 0075"))
    with loaded(Branch) as rows:
        r = await client.get(f"/gyms/{gym.slug}/branches/{home.id}")
    html = r.text
    assert r.status_code == 200 and rows[Branch] == 7  # this branch + 6 sisters, never the 150
    start = html.index('id="others-title"')
    others = html[start:html.index("</section>", start)]
    names = re.findall(r"Site (\d{4})", others)
    assert sorted(set(names)) == ["0072", "0073", "0074", "0076", "0077", "0078"]  # nearest to this branch
    assert "View all 150 locations" in others and f'href="/gyms/{gym.slug}/branches"' in others
    total = await db.scalar(select(func.count(Branch.id)).where(Branch.gym_id == gym.id))
    assert total == 150
