"""Pagination audit - the shop loads bounded data whatever the size of the catalogue, the platform or a member's lists:

* catalogue: a COUNT capped at 12,000 ("12,000+"), a window of pager links (never one per page), one LIMIT page;
  the store filter lists the 20 stores with the most products (+ a typeahead with LIMIT 20 on a trigram index) and a
  store's own page runs no stores query; the category counts are cached 60 s (cleared on product changes);
* search: trigram ILIKE (GIN) UNION a top-200 semantic CTE (HNSW) instead of a distance over the whole table;
* wishlist (24/page, at most 500 saved), bag (at most 50 products, 30 per store), orders (20/page), reviews (keyset,
  20 at a time), pickup branches (3 + a count), order events (<= 50), safety conditions (<= 50, safety-first);
* the hourly low-stock digest works through owners in bounded chunks, one transaction each.
"""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta

from sqlalchemy import event, func, select, text

from app import db as dbmod
from app.core.utils import utcnow
from app.db import QueryCounter
from app.models import AuditLog, CartItem, HealthCondition, Notification, Product, ProductReview, ShopOrder, WishlistItem
from app.models.enums import ConditionKind
from app.models.shop import ProductCategory
from app.services import shop as svc
from tests.conftest import api, login_as, post_form
from tests.factories import make_branch, make_customer, make_gym, make_owner
from tests.test_shop import _reset_shop_ai, make_order, make_product, shop_setup  # noqa: F401 - autouse fixture


# ------------------------------------------------------------------ helpers
async def bulk_products(db, gym, n: int, *, prefix: str = "Bulk", stock: int = 10, category: str = "equipment") -> None:
    """``n`` active products of ``gym`` in one INSERT ... SELECT (no embedding)."""
    await db.execute(text(
        "INSERT INTO products (gym_id, name, slug, category, description, price, currency, stock, is_active) "
        "SELECT :gym, :prefix || ' ' || lpad(g::text, 5, '0'), lower(:prefix) || '-' || g, :category, "
        "'A product seeded in bulk.', 100 + g % 50, :currency, :stock, true FROM generate_series(1, :n) AS g"),
        {"gym": gym.id, "prefix": prefix, "category": category, "currency": gym.currency, "stock": stock, "n": n})
    await db.commit()


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


async def statements_of(client, url: str, *, status: int = 200) -> list[tuple[str, object]]:
    captured: list[tuple[str, object]] = []

    def grab(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        captured.append((statement, parameters))

    engine = dbmod.get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", grab)
    try:
        r = await client.get(url)
    finally:
        event.remove(engine, "before_cursor_execute", grab)
    assert r.status_code == status, url
    return captured


def product_cards(html: str) -> int:
    return html.count('class="shop-card group"')


def page_links(html: str) -> list[int]:
    return [int(n) for n in re.findall(r'aria-label="Page (\d+)"', html)]


def store_options(html: str) -> list[str]:
    select_html = html[html.index('id="shop-gym"'):html.index("</select>", html.index('id="shop-gym"'))]
    return re.findall(r'<option value="\d+"[^>]*>([^<]+)</option>', select_html)


# ------------------------------------------------------------------ catalogue: capped count, windowed pager, one page
async def test_catalogue_counts_to_12000_at_most_and_links_a_window_of_pages(client, db):
    _, gym, _ = await shop_setup(db)
    await bulk_products(db, gym, svc.COUNT_CAP + 50)  # 12,050 products: more than the 500 pages can show
    with loaded(Product) as rows:
        executed = await statements_of(client, "/shop")
        r = await client.get("/shop")
    html = r.text
    assert "12,000+ products" in html and product_cards(html) == svc.PAGE_SIZE
    assert rows[Product] <= 2 * svc.PAGE_SIZE  # one page per request, never the catalogue
    assert page_links(html) == [1, 2, 3, 500]  # 1 2 3 … 500: a window, not 500 links
    count_sql, count_params = next((s, p) for s, p in executed if s.startswith("SELECT count(*)"))
    assert "LIMIT" in count_sql and svc.COUNT_CAP + 1 in tuple(count_params)  # the COUNT stops at 12,001
    page_sql = next(s for s, _ in executed if "FROM products JOIN gyms" in s and "OFFSET" in s)
    assert "LIMIT" in page_sql

    last = (await client.get("/shop?page=500")).text
    assert product_cards(last) == svc.PAGE_SIZE and page_links(last) == [1, 498, 499, 500]
    assert 'rel="next"' not in last  # page 500 is the last one: no endless "Next" to page 501
    assert page_links((await client.get("/shop?page=9999")).text)[-1] == 500  # clamped
    middle = (await client.get("/shop?page=250&sort=price_asc")).text
    assert page_links(middle) == [1, 248, 249, 250, 251, 252, 500]
    assert re.search(r'href="/shop\?sort=price_asc&amp;page=251"[^>]*rel="next"', middle)  # links keep the filters
    assert (await client.get("/shop?gym=99999999999999999999")).status_code == 200  # not a BIGINT: ignored


# ------------------------------------------------------------------ store filter + typeahead
async def test_store_filter_lists_the_top_20_stores_and_the_typeahead_finds_the_rest(client, db):
    gyms = []
    for i in range(25):
        gym = await make_gym(db, await make_owner(db), name=f"Store {i + 1:02d}")
        await bulk_products(db, gym, i + 1, prefix=f"Item S{i + 1:02d}")  # Store 25 sells the most
        gyms.append(gym)
    iron = await make_gym(db, await make_owner(db), name="Iron Works")
    await bulk_products(db, iron, 1, prefix="Anvil")
    alpha = await make_gym(db, await make_owner(db), name="Alpha Iron House")
    await bulk_products(db, alpha, 1, prefix="Kettle")
    await make_gym(db, await make_owner(db), name="Iron Without Products")  # sells nothing: never a store

    html = (await client.get("/shop")).text
    names = store_options(html)
    assert names == [f"Store {i:02d}" for i in range(6, 26)]  # the 20 with the most products (6..25), A-Z - never all
    assert 'data-component="dropdown shop-store-search"' in html and "Find another store" in html

    store_page = await statements_of(client, f"/shop/gym/{gyms[0].slug}")
    assert not any("GROUP BY gyms.id" in s for s, _ in store_page)  # a store's own page: no stores query at all
    picked = (await client.get(f"/shop?gym={gyms[0].id}")).text
    assert '<option value="%d" selected>Store 01</option>' % gyms[0].id in picked  # found via the typeahead

    found = (await client.get("/api/shop/stores?q=iron")).json()
    assert [s["name"] for s in found["stores"]] == ["Iron Works", "Alpha Iron House"]  # prefix matches first
    many = (await client.get("/api/shop/stores?q=store")).json()["stores"]
    assert len(many) == svc.STORE_SEARCH_LIMIT == 20 and many[0]["name"] == "Store 01"
    assert (await client.get("/api/shop/stores?q=s")).json()["stores"] == []  # too short
    assert (await client.get("/api/shop/stores")).json()["stores"] == []
    assert (await client.get("/api/shop/stores?q=%25_")).status_code == 200  # wildcards are escaped
    executed = await statements_of(client, "/api/shop/stores?q=works")
    assert len(executed) == 1 and "LIMIT" in executed[0][0] and "ILIKE" in executed[0][0]
    assert await statements_of(client, "/api/shop/stores?q=works") == []  # cached for a minute


async def test_store_typeahead_predicate_is_served_by_the_gym_name_trigram_index(client, db):
    """The typeahead filters on the bare ``gyms.name ILIKE '%q%'`` - the predicate the GIN trigram index serves (the
    planner switches to it once a seq scan of the gyms table costs more; a few thousand short names still scan)."""
    index = await db.scalar(text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_gyms_name_trgm'"))
    assert index and "USING gin (name gin_trgm_ops)" in index
    owner = await make_owner(db)
    gym = await make_gym(db, owner, name="Trigram Barbell Club")
    await bulk_products(db, gym, 1)
    statement, params = (await statements_of(client, "/api/shop/stores?q=barbell"))[0]
    assert "gyms.name ILIKE" in statement and "LIMIT" in statement
    pattern = next(p for p in tuple(params) if isinstance(p, str) and "barbell" in p)
    async with dbmod.get_engine().connect() as conn:
        await conn.exec_driver_sql("SET LOCAL enable_seqscan = off")
        plan = "\n".join(r[0] for r in (await conn.exec_driver_sql(
            "EXPLAIN SELECT id FROM gyms WHERE name ILIKE $1 ESCAPE '\\'", (pattern,))).all())
    assert "Bitmap Index Scan on ix_gyms_name_trgm" in plan, plan
    assert [s["name"] for s in (await client.get("/api/shop/stores?q=barbell")).json()["stores"]] == ["Trigram Barbell Club"]


def test_gym_name_trigram_index_is_declared_by_the_models_alone():
    """app.db.create_all / create_indexes_concurrently (the pre-deploy CONCURRENTLY build) and scripts/init_db.py only
    import app.models: an index declared in app.services.shop was invisible to them (a fresh interpreter, not this
    test process, which has imported the whole app)."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    code = ("import sys, app.models\nfrom app.models import Gym\nassert 'app.services.shop' not in sys.modules\n"
            "print(sorted(ix.name for ix in Gym.__table__.indexes))")
    proc = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parent.parent, capture_output=True,
                          text=True, timeout=120, env={**os.environ, "ENVIRONMENT": "test"})
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "ix_gyms_name_trgm" in proc.stdout, proc.stdout


# ------------------------------------------------------------------ category counts: cached, cleared on changes
async def test_category_counts_are_cached_and_cleared_when_products_change(client, db):
    _, gym, _ = await shop_setup(db)
    p = await make_product(db, gym, category=ProductCategory.APPAREL)
    group_by = "GROUP BY products.category"
    first = await statements_of(client, "/shop")
    assert sum(group_by in s for s, _ in first) == 1
    assert not any(group_by in s for s, _ in await statements_of(client, "/shop"))  # cached

    p.stock = 3  # a stock change does not move a facet: the counts stay cached
    await db.commit()
    assert not any(group_by in s for s, _ in await statements_of(client, "/shop"))
    await make_product(db, gym, category=ProductCategory.APPAREL)  # a new product: cleared on commit
    html = (await client.get("/shop")).text
    assert re.search(r"Apparel<span class=\"shop-chip-count\">2</span>", html)
    p.is_active = False
    await db.commit()
    assert re.search(r"Apparel<span class=\"shop-chip-count\">1</span>", (await client.get("/shop")).text)


# ------------------------------------------------------------------ search: trigram + top-K semantic CTE
async def test_search_unions_trigram_matches_with_a_top_k_semantic_cte(client, db, monkeypatch):
    _, gym, _ = await shop_setup(db)
    for i in range(4):
        await make_product(db, gym, name=f"Pulse Band {i}", category=ProductCategory.WEARABLES,
                           description="Heart rate tracking strap with heart rate zones and a heart rate monitor.")
    await make_product(db, gym, name="Heart Rate Cable", category=ProductCategory.ACCESSORIES, description="A cable.")
    executed = await statements_of(client, "/shop?q=heart+rate")
    page_sql, params = next((s, p) for s, p in executed if "semantic AS MATERIALIZED" in s and "OFFSET" in s)
    assert "ILIKE" in page_sql and "UNION" in page_sql and "ORDER BY products.embedding <=>" in page_sql
    assert svc.SEMANTIC_TOP_K in tuple(params)  # ORDER BY embedding <=> :q LIMIT 200 - never every product
    assert any("hnsw.iterative_scan" in s for s, _ in executed)
    html = (await client.get("/shop?q=heart+rate")).text
    assert product_cards(html) == 5 and html.index("Heart Rate Cable") < html.index("Pulse Band")  # text match first

    monkeypatch.setattr(svc, "SEMANTIC_TOP_K", 2)
    html = (await client.get("/shop?q=heart+rate")).text
    assert product_cards(html) == 3  # the trigram match + only the 2 nearest semantic matches


async def test_search_plan_uses_the_trigram_and_hnsw_indexes_on_a_realistic_catalogue(client, db):
    """3,000 products with embeddings (ANALYZEd): the text half is a GIN trigram bitmap scan and the semantic CTE an
    HNSW index scan - neither reads the whole table."""
    _, gym, _ = await shop_setup(db)
    await make_product(db, gym, name="Heart Rate Strap", category=ProductCategory.WEARABLES,
                       description="Chest strap that tracks heart rate.")
    await db.execute(text(
        "INSERT INTO products (gym_id, name, slug, category, description, price, currency, stock, is_active, embedding) "
        "SELECT :gym, 'Filler ' || g, 'filler-' || g, 'equipment', 'Filler product.', 100, 'INR', 5, true, "
        "(SELECT CAST(array_agg(random() - 0.5) AS vector(384)) FROM generate_series(1, 384) AS i WHERE g > 0) "
        "FROM generate_series(1, 3000) AS g"), {"gym": gym.id})
    await db.commit()
    await db.execute(text("ANALYZE products"))
    await db.commit()
    try:
        executed = await statements_of(client, "/shop?q=heart+rate")
        page_sql, params = next((s, p) for s, p in executed if "semantic AS MATERIALIZED" in s and "OFFSET" in s)
        async with dbmod.get_engine().connect() as conn:
            await conn.exec_driver_sql("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)")  # as requests do
            plan = "\n".join(r[0] for r in (await conn.exec_driver_sql("EXPLAIN " + page_sql, params)).all())
        assert "ix_products_name_trgm" in plan and "ix_products_embedding_hnsw" in plan, plan
        assert "Heart Rate Strap" in (await client.get("/shop?q=heart+rate")).text
    finally:  # leave no 3,000-row statistics behind for the tests that follow
        await db.rollback()
        await db.execute(text("DELETE FROM products WHERE name LIKE 'Filler %'"))
        await db.commit()
        await db.execute(text("ANALYZE products"))
        await db.commit()


# ------------------------------------------------------------------ wishlist: paginated + capped
async def test_wishlist_is_paginated_in_sql_and_capped(client, db, monkeypatch):
    _, gym, _ = await shop_setup(db)
    await bulk_products(db, gym, 31, prefix="Wish")
    cust = await make_customer(db)
    await db.execute(text("INSERT INTO wishlist_items (user_id, product_id) SELECT :u, id FROM products "
                          "WHERE name LIKE 'Wish %' AND name <> 'Wish 00031'"), {"u": cust.id})
    await db.commit()
    await login_as(client, cust)
    await client.get("/shop/wishlist")  # session touch
    with loaded(Product) as rows, QueryCounter() as first:
        page1 = (await client.get("/shop/wishlist")).text
    assert product_cards(page1) == svc.WISHLIST_PAGE_SIZE and rows[Product] == svc.WISHLIST_PAGE_SIZE
    assert "30 saved products · showing 1–24" in page1 and page_links(page1) == [1, 2]
    with QueryCounter() as second:
        page2 = (await client.get("/shop/wishlist?page=2")).text
    assert product_cards(page2) == 6 and first.count == second.count

    monkeypatch.setattr(svc, "WISHLIST_MAX", 30)
    extra = await db.scalar(select(Product).where(Product.name == "Wish 00031"))
    r = await api(client, "POST", f"/shop/wishlist/{extra.id}/toggle")
    assert r.status_code == 400 and "wishlist is full" in r.json()["detail"]
    assert await db.scalar(select(func.count(WishlistItem.id)).where(WishlistItem.user_id == cust.id)) == 30
    saved = await db.scalar(select(Product).where(Product.name == "Wish 00001"))
    r = await api(client, "POST", f"/shop/wishlist/{saved.id}/toggle")  # removing still works at the cap
    assert r.status_code == 200 and r.json()["wishlisted"] is False


# ------------------------------------------------------------------ bag: at most 50 products, 30 per store
async def test_bag_holds_a_bounded_number_of_products(client, db, monkeypatch):
    monkeypatch.setattr(svc, "MAX_CART_LINES", 3)
    monkeypatch.setattr(svc, "MAX_STORE_LINES", 2)
    _, store_a, _ = await shop_setup(db)
    _, store_b, _ = await shop_setup(db)
    a = [await make_product(db, store_a, name=f"A {i}") for i in range(3)]
    b = [await make_product(db, store_b, name=f"B {i}") for i in range(2)]
    cust = await make_customer(db)
    await login_as(client, cust)
    for p in (a[0], a[1]):
        assert (await api(client, "POST", "/shop/cart/add", json={"product_id": p.id})).status_code == 200
    r = await api(client, "POST", "/shop/cart/add", json={"product_id": a[2].id})  # third product of store A
    assert r.status_code == 400 and "2 different products from" in r.json()["detail"]
    assert (await api(client, "POST", "/shop/cart/add", json={"product_id": b[0].id})).status_code == 200
    r = await api(client, "POST", "/shop/cart/add", json={"product_id": b[1].id})  # fourth product in the bag
    assert r.status_code == 400 and "bag is full" in r.json()["detail"]
    r = await api(client, "POST", "/shop/cart/add", json={"product_id": a[0].id, "quantity": 2})  # more of one: fine
    assert r.status_code == 200 and r.json()["quantity"] == 3
    assert await db.scalar(select(func.count(CartItem.id)).where(CartItem.user_id == cust.id)) == 3

    db.add(CartItem(user_id=cust.id, product_id=b[1].id, quantity=1))  # a bag filled before the cap existed
    await db.commit()
    with loaded(CartItem) as rows:
        assert (await client.get("/shop/cart")).status_code == 200
    assert rows[CartItem] == 3  # LIMIT MAX_CART_LINES


# ------------------------------------------------------------------ orders list, order events, reviews, pickup
async def test_orders_are_paginated_and_the_timeline_reads_at_most_50_events(client, db):
    _, gym, _ = await shop_setup(db)
    p = await make_product(db, gym)
    cust = await make_customer(db)
    orders = [await make_order(db, cust, gym, [(p, 1)]) for _ in range(25)]
    await login_as(client, cust)
    await client.get("/shop/orders")  # session touch
    with loaded(ShopOrder) as rows, QueryCounter() as first:
        page1 = (await client.get("/shop/orders")).text
    assert page1.count('href="/shop/orders/') == svc.ORDERS_PAGE_SIZE and rows[ShopOrder] == svc.ORDERS_PAGE_SIZE
    assert page_links(page1) == [1, 2] and "25 orders" in page1
    with QueryCounter() as second:
        page2 = (await client.get("/shop/orders?page=2")).text
    assert page2.count('href="/shop/orders/') == 5 and first.count == second.count
    assert orders[0].order_number in page2 and orders[-1].order_number in page1  # newest first

    order = orders[0]
    await db.execute(text(
        "INSERT INTO audit_logs (action, entity_type, entity_id, detail, created_at) SELECT 'shop.order_status', "
        "'shop_order', :o, CAST(:d AS jsonb), now() - make_interval(mins => 70 - g) FROM generate_series(1, 60) AS g"),
        {"o": order.id, "d": json.dumps({"to": "processing"})})
    await db.commit()
    with loaded(AuditLog) as events:
        assert (await client.get(f"/shop/orders/{order.id}")).status_code == 200
    assert events[AuditLog] == 50  # the 50 oldest events (each status' first one), never the whole history


async def test_reviews_come_20_at_a_time_and_pickup_branches_three_and_a_count(client, db):
    owner, gym, _ = await shop_setup(db)
    for i in range(9):
        await make_branch(db, gym, name=f"Pickup {i:02d}")
    p = await make_product(db, gym, name="Reviewed Bar", fulfillment="pickup")
    start = utcnow() - timedelta(days=30)
    for i in range(25):
        reviewer = await make_customer(db, password=None, full_name=f"Reviewer {i:02d}")
        db.add(ProductReview(product_id=p.id, user_id=reviewer.id, rating=4, title=f"Review number {i:02d}",
                             created_at=start + timedelta(hours=i)))
    await db.commit()
    url = svc.product_url(p)
    with loaded(ProductReview) as rows:
        first = (await client.get(url)).text
    assert rows[ProductReview] == 21  # one window: 20 shown + 1 to know there are more
    assert first.count("Review number") == 20 and "Review number 24" in first and "Review number 04" not in first
    older = re.search(r'<a href="([^"]+)" class="btn btn-secondary btn-sm" rel="next" data-older-reviews', first).group(1)
    second = (await client.get(older.replace("&amp;", "&"))).text
    assert second.count("Review number") == 5 and "Review number 04" in second and "Review number 05" not in second
    assert "data-older-reviews" not in second and "Newest reviews" in second

    assert "Pickup at 10 branches" in first  # a count ...
    assert first.count("Pickup · Pickup") + first.count("Pickup · Branch") == 3 and "… and 7 more branches" in first


async def test_safety_panel_reads_at_most_50_conditions_safety_relevant_first(client, db):
    _, gym, _ = await shop_setup(db)
    creatine = await make_product(db, gym, name="Creatine Monohydrate", is_supplement=True,
                                  description="Micronised creatine for strength.")
    cust = await make_customer(db)
    db.add_all(HealthCondition(customer_id=cust.id, kind=ConditionKind.INJURY, name=f"Old sprain {i:02d}")
               for i in range(70))
    await db.commit()
    db.add(HealthCondition(customer_id=cust.id, kind=ConditionKind.CONDITION, name="Chronic kidney disease",
                           tag="kidney"))  # added last: still read first
    await db.commit()
    await login_as(client, cust)
    with loaded(HealthCondition) as rows:
        html = (await client.get(svc.product_url(creatine))).text
    assert rows[HealthCondition] == svc.MAX_CONDITIONS == 50
    assert "Creatine and kidney disease" in html  # the tagged condition is never pushed out


async def test_one_tag_repeated_over_and_over_never_pushes_a_caution_or_an_allergy_out(client, db):
    """Padding with ONE safety tag (55 differently named rows, all "migraine") ahead of the rows that matter: the
    safety panel still reads one row of every tag first (the kidney caution), then the allergies and medications -
    the repeats are what the MAX_CONDITIONS cut leaves out."""
    _, gym, _ = await shop_setup(db)
    whey = await make_product(db, gym, name="Creatine Whey Blend", is_supplement=True, allergens=["milk"],
                              description="Whey protein with creatine.")
    cust = await make_customer(db)
    db.add_all(HealthCondition(customer_id=cust.id, kind=ConditionKind.CONDITION, name=f"Migraine episode {i:02d}",
                               tag="migraine") for i in range(55))
    await db.commit()
    db.add_all([HealthCondition(customer_id=cust.id, kind=ConditionKind.CONDITION, name="Chronic kidney disease",
                                tag="kidney"),
                HealthCondition(customer_id=cust.id, kind=ConditionKind.ALLERGY, name="Milk"),
                HealthCondition(customer_id=cust.id, kind=ConditionKind.MEDICATION, name="Lisinopril")])
    await db.commit()
    loaded_rows = await svc.load_conditions(db, cust)
    assert len(loaded_rows) == svc.MAX_CONDITIONS
    assert [c.name for c in loaded_rows[:5]] == ["Migraine episode 00", "Chronic kidney disease", "Milk", "Lisinopril",
                                                 "Migraine episode 01"]
    await login_as(client, cust)
    html = (await client.get(svc.product_url(whey))).text
    assert "Creatine and kidney disease" in html and "Contains milk" in html and "Lisinopril" in html


def member_context(prompt: str) -> dict[str, str]:
    """The MEMBER CONTEXT lines of a shop AI prompt -> {label: value}."""
    block = prompt.split("MEMBER CONTEXT:\n", 1)[1].split("\n\n", 1)[0]
    return dict(line.split(": ", 1) for line in block.splitlines())


def named_items(value: str) -> list[str]:
    """The items a MEMBER CONTEXT line names ('"A" [tag]; "B" (+3 more saved, not shown)' -> 2 items)."""
    value = value.split(" (+", 1)[0]
    return [] if value == "none listed" or value.endswith("(size limit)") else value.split("; ")


async def test_ai_prompt_names_at_most_10_conditions_those_behind_its_safety_notes_first(client, db, fake_ai):
    """The product Q&A prompt lists at most PROMPT_CONDITIONS (10) of the member's conditions, allergies and medications
    together - those behind this product's safety notes first (a caution it carries, an allergen it contains, any
    medication with a supplement) - so a long health profile never grows the prompt (tokens, cost, latency). The
    deterministic safety notes still read every condition loaded (<= MAX_CONDITIONS)."""
    _, gym, _ = await shop_setup(db)
    creatine = await make_product(db, gym, name="Creatine Monohydrate", is_supplement=True, allergens=["milk"],
                                  description="Micronised creatine for strength.")
    cust = await make_customer(db)
    db.add_all(HealthCondition(customer_id=cust.id, kind=ConditionKind.INJURY, name=f"Old sprain {i:02d}")
               for i in range(20))
    db.add_all(HealthCondition(customer_id=cust.id, kind=ConditionKind.MEDICATION, name=f"Medicine {i:02d}")
               for i in range(15))
    db.add(HealthCondition(customer_id=cust.id, kind=ConditionKind.CONDITION, name="Migraine", tag="migraine"))
    db.add(HealthCondition(customer_id=cust.id, kind=ConditionKind.ALLERGY, name="Milk"))
    await db.commit()
    db.add(HealthCondition(customer_id=cust.id, kind=ConditionKind.CONDITION, name="Chronic kidney disease",
                           tag="kidney"))  # added last: still named first
    await db.commit()
    provider = fake_ai(responses=[json.dumps({"answer": "Ask your doctor first.", "suitable_for_you": "unsure"})])
    await login_as(client, cust)
    r = await api(client, "POST", f"/api/shop/products/{creatine.id}/ask", json={"question": "Can I take it daily?"})
    assert r.status_code == 200
    member = member_context(provider.prompts[0]["prompt"])
    named = [x for key in ("health conditions", "allergies/intolerances", "medications")
             for x in named_items(member[key])]
    assert len(named) == svc.PROMPT_CONDITIONS == 10
    # the creatine caution, by the member's own words - Migraine and the sprains are counted, not named
    assert member["health conditions"] == '"Chronic kidney disease" [Kidney disease] (+21 more saved, not shown)'
    assert member["allergies/intolerances"] == '"Milk" [Milk / dairy]'  # an allergen of the product
    # the first 8 of 15 medications, and the count of the rest
    assert member["medications"] == "; ".join(f'"Medicine {i:02d}"' for i in range(8)) + " (+7 more saved, not shown)"
    assert "sprain" not in provider.prompts[0]["prompt"]  # past the cap
    flags = r.json()["safety_flags"]
    assert any(f.startswith("Creatine and kidney disease") for f in flags)  # the safety notes are unchanged
    assert any(f.startswith("Contains milk") for f in flags)


GOLD_WHEY_INGREDIENTS = "Whey protein isolate (milk), cocoa powder, natural flavour, soy lecithin, sucralose."


async def test_ai_prompt_names_saved_items_the_app_has_no_tag_for_and_never_says_none_listed(client, db, fake_ai):
    """F05: the product AI dropped saved allergies and conditions it couldn't map to a tag or an allergen key
    ('Sucralose', 'CKD stage 3') and told the model "none listed", so it answered as if the member had said nothing.
    The prompt now names the member's own words; the safety notes flag an allergy the listing itself mentions; and a
    row saved without a tag gets the caution the shared normaliser gives its words today."""
    _, gym, _ = await shop_setup(db)
    whey = await make_product(db, gym, name="Gold Whey", is_supplement=True, allergens=["milk", "soy"],
                              caution_tags=["kidney"], ingredients=GOLD_WHEY_INGREDIENTS)
    cust = await make_customer(db)
    db.add_all([
        HealthCondition(customer_id=cust.id, kind=ConditionKind.ALLERGY, name="Sucralose"),
        HealthCondition(customer_id=cust.id, kind=ConditionKind.CONDITION, name="CKD stage 3"),
        HealthCondition(customer_id=cust.id, kind=ConditionKind.CONDITION, name="Chronic kidney disease"),  # no tag
    ])
    await db.commit()
    provider = fake_ai(responses=[json.dumps({"answer": "Please check with your doctor.", "suitable_for_you": "yes"})])
    await login_as(client, cust)
    r = await api(client, "POST", f"/api/shop/products/{whey.id}/ask",
                  json={"question": "Is this whey safe for me with my health profile?"})
    assert r.status_code == 200
    member = member_context(provider.prompts[0]["prompt"])
    assert member["allergies/intolerances"] == '"Sucralose"'  # was: "none listed"
    # both rows were saved without a tag: the shared normaliser reads "CKD" as kidney disease too (integrate step)
    assert member["health conditions"] == '"CKD stage 3" [Kidney disease]; "Chronic kidney disease" [Kidney disease]'
    assert member["medications"] == "none listed"  # true: none saved
    body = r.json()
    flags = body["safety_flags"]
    assert any(f.startswith("Check the ingredients") and "“sucralose”" in f and "Sucralose" in f for f in flags)
    assert any(f.startswith("Caution: kidney disease") for f in flags)  # the untagged kidney row's caution
    assert body["suitable_for_you"] == "unsure"  # the deterministic warnings beat the AI's "yes"
    html = (await client.get(svc.product_url(whey))).text  # the product page's safety panel says the same
    assert "Check the ingredients" in html and "Caution: kidney disease" in html


def test_allergy_mentions_match_whole_words_and_skip_declared_allergens():
    bar = Product(name="Peanut Crunch Bar", category=ProductCategory.NUTRITION, description="A bar.", allergens=["soy"],
                  ingredients="Peanuts, oats, honey, soy lecithin, stevia.", caution_tags=[], is_supplement=False)
    rows = [HealthCondition(kind=ConditionKind.ALLERGY, name=n)
            for n in ("Severe allergy to stevia", "Soya lecithin", "Peanut allergy", "Oat milk", "Dust mites", "Honeydew")]
    found = svc.allergy_mentions(bar, rows)
    assert found == [("stevia", "Severe allergy to stevia"), ("peanut", "Peanut allergy"), ("oat", "Oat milk")]
    # "Soya lecithin": the soy the product declares already has its "Contains soy" flag; "Honeydew" is not "honey"
    assert len(svc.allergy_mentions(bar, rows * 5)) == svc.MAX_ALLERGY_MENTIONS


def test_prompt_conditions_order_without_safety_flags_and_the_cap():
    """No condition is behind a safety note of a shaker bottle: tagged conditions first, then allergies, then
    medications (each in the order read), then allergies no allergen key covers, then untagged notes - never dropped
    for having no tag (F05), and never more than the cap."""
    shaker = Product(name="Shaker Bottle", category=ProductCategory.ACCESSORIES, description="A shaker.",
                     is_supplement=False, allergens=[], caution_tags=[], nutrition_facts=None)
    conditions = [HealthCondition(kind=ConditionKind.MEDICATION, name=f"Med {i}") for i in range(5)]
    conditions += [HealthCondition(kind=ConditionKind.INJURY, name="Sore calves after long runs"),  # no tag fits
                   HealthCondition(kind=ConditionKind.ALLERGY, name="Nut allergy")]  # an allergen key, no tag saved
    conditions += [HealthCondition(kind=ConditionKind.CONDITION, name=f"Tagged {t}", tag=t)
                   for t in ("asthma", "migraine", "thyroid")]
    assert [c.name for c in svc.prompt_conditions(shaker, conditions, limit=6)] == [
        "Nut allergy", "Tagged asthma", "Tagged migraine", "Tagged thyroid", "Med 0", "Med 1"]  # "Nut allergy": the
    # normaliser's food_allergy tag (a row saved without its tag is read as the writers would have saved it)
    assert len(svc.prompt_conditions(shaker, conditions * 5)) == svc.PROMPT_CONDITIONS
    note = HealthCondition(kind=ConditionKind.INJURY, name="Sore calves after long runs")
    assert svc.prompt_conditions(shaker, [note]) == [note]  # the member's own words still reach the AI
    stevia = HealthCondition(kind=ConditionKind.ALLERGY, name="Stevia")  # no allergen key, no tag
    assert [c.name for c in svc.prompt_conditions(shaker, [note, stevia, *conditions])] == [
        "Nut allergy", "Tagged asthma", "Tagged migraine", "Tagged thyroid", "Med 0", "Med 1", "Med 2", "Med 3",
        "Med 4", "Stevia"]  # the untagged notes come last - the cap cuts them first


async def test_ids_beyond_bigint_are_a_404_not_a_server_error(client, db):
    huge = "9" * 20  # parses as an int, but asyncpg refuses it for a BIGINT column
    assert (await client.get(f"/shop/p/{huge}")).status_code == 404
    assert (await client.get(f"/shop/p/{huge}/some-slug")).status_code == 404
    assert (await client.get(f"/shop?gym={huge}")).status_code == 200
    await login_as(client, await make_customer(db))
    assert (await client.get(f"/shop/orders/{huge}")).status_code == 404
    assert (await client.get(f"/shop/orders/{huge}/receipt")).status_code == 404
    r = await client.get(f"/shop/checkout?gym={huge}")
    assert r.status_code == 303 and r.headers["location"] == "/shop/cart"
    assert (await post_form(client, "/shop/cart/add", {"product_id": huge})).status_code == 404


# ------------------------------------------------------------------ low-stock digest: bounded chunks
async def test_low_stock_digest_works_through_owners_in_bounded_transactions(db, monkeypatch):
    monkeypatch.setattr(svc, "LOW_STOCK_CHUNK", 2)
    owners = []
    for i in range(5):
        owner, gym, _ = await shop_setup(db)
        await make_product(db, gym, name=f"Low {i}", stock=1, low_stock_threshold=3)
        owners.append(owner)
    big_owner, big_gym, _ = await shop_setup(db)
    for i in range(8):
        await make_product(db, big_gym, name=f"Scarce {i}", stock=i, low_stock_threshold=10)

    transactions = []
    real_scope = svc.session_scope

    @asynccontextmanager
    async def counting_scope():
        async with real_scope() as session:
            transactions.append(session)
            yield session

    monkeypatch.setattr(svc, "session_scope", counting_scope)
    executed: list[str] = []

    def grab(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if "due_owners" in statement:
            executed.append(statement)
            assert 2 in tuple(parameters)  # LIMIT 2 owners per chunk

    engine = dbmod.get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", grab)
    try:
        assert await svc.run_low_stock_digests() == 6
    finally:
        event.remove(engine, "before_cursor_execute", grab)
    assert len(transactions) == 4 == len(executed)  # 6 owners, 2 per transaction (+ the empty last chunk)
    notified = (await db.execute(select(Notification.user_id, Notification.body)
                                 .where(Notification.title == svc.LOW_STOCK_TITLE))).all()
    assert sorted(u for u, _ in notified) == sorted([*(o.id for o in owners), big_owner.id])
    body = next(b for u, b in notified if u == big_owner.id)
    assert body.startswith("8 products are running low: Scarce 0 (out of stock); Scarce 1 (1 left)")
    assert "Scarce 5 (5 left) and 2 more." in body and "Scarce 6" not in body

    transactions.clear()
    assert await svc.run_low_stock_digests() == 0  # notified within 20 h: skipped (indexed NOT EXISTS)
    assert len(transactions) == 1
    assert await db.scalar(select(func.count(Notification.id)).where(Notification.title == svc.LOW_STOCK_TITLE)) == 6


async def test_low_stock_periodic_commits_per_chunk_outside_the_lock_transaction(db, monkeypatch):
    monkeypatch.setattr(svc, "LOW_STOCK_CHUNK", 1)
    for i in range(3):
        _, gym, _ = await shop_setup(db)
        await make_product(db, gym, name=f"Last {i}", stock=0, low_stock_threshold=2)
    async with dbmod.session_scope() as lock_holder:
        await svc._low_stock_digest(lock_holder)  # what run_periodic calls, inside its advisory-lock transaction
        # the digests are already committed by their own transactions, whatever happens to this one
        async with dbmod.get_sessionmaker()() as other:
            n = await other.scalar(select(func.count(Notification.id)).where(Notification.title == svc.LOW_STOCK_TITLE))
        assert n == 3
