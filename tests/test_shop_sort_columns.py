"""Catalogue sorts at scale (pagination audit leftovers, item a): the default order ("Featured": in stock first, then
newest), "Top rated" - and "Newest" and both price orders - read an index in order: EXPLAIN shows no sort of the
matching products at 100,000 products, with the page's statement count unchanged.

* ``products.in_stock``: a STORED generated column (``stock > 0``) - in step whoever writes the stock (ORM, raw SQL, COPY);
* ``products.rating_avg`` / ``review_count``: the summary of a product's VISIBLE reviews, used by "Top rated" and every
  product card (no aggregate per row any more). Rewritten in the SAME transaction as every review insert, edit,
  deletion and moderation (the ORM flush hook in app/services/shop.py; ``refresh_product_ratings`` for Core statements),
  under a row lock taken BEFORE the aggregate (concurrent reviews never lose a count), backfilled once for existing
  rows and reconciled hourly by an idempotent, chunked run (``backfill_product_ratings``);
* ``sync_schema`` adds the columns + indexes to an existing database and ``create_indexes_concurrently`` builds them.
"""

from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal

from sqlalchemy import delete, event, func, insert, select, text

from app import db as dbmod
from app.core import ttl_cache
from app.db import QueryCounter
from app.models import Product, ProductReview
from app.models.shop import OrderStatus
from app.services import shop as svc
from app.services.jobs import registered_periodic, run_periodic
from tests.conftest import login_as, post_form
from tests.factories import make_customer
from tests.test_shop import _reset_shop_ai, make_order, make_product, shop_setup  # noqa: F401 - autouse fixture

FEATURED_INDEX = "ix_products_catalogue_featured"
RATING_INDEX = "ix_products_catalogue_rating"
NEWEST_INDEX = "ix_products_catalogue_newest"
PRICE_INDEX = "ix_products_catalogue_price"
BIG = 100_000


# ------------------------------------------------------------------ helpers
async def summary(product_id: int) -> tuple[Decimal | None, int]:
    """(rating_avg, review_count) as committed - read on a connection of its own."""
    async with dbmod.get_engine().connect() as conn:
        row = (await conn.execute(text("SELECT rating_avg, review_count FROM products WHERE id = :id"),
                                  {"id": product_id})).one()
    return row[0], row[1]


async def in_stock_of(product_id: int) -> bool:
    async with dbmod.get_engine().connect() as conn:
        return (await conn.execute(text("SELECT in_stock FROM products WHERE id = :id"), {"id": product_id})).scalar_one()


async def buyer(db, gym, product):
    """A member with a completed order for ``product`` (a verified purchaser, allowed to review it)."""
    member = await make_customer(db)
    await make_order(db, member, gym, [(product, 1)], status=OrderStatus.COMPLETED, completed_days_ago=1)
    return member


async def statements_of(client, url: str) -> list[tuple[str, object]]:
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


def page_statement(captured: list[tuple[str, object]]) -> tuple[str, object]:
    return next((s, p) for s, p in captured if "FROM products JOIN gyms" in s and "OFFSET" in s)


async def explain(statement: str, params: object) -> dict:
    async with dbmod.get_engine().connect() as conn:
        plan = (await conn.exec_driver_sql("EXPLAIN (ANALYZE, FORMAT JSON) " + statement, params)).scalar()
    plan = json.loads(plan) if isinstance(plan, str) else plan
    return plan[0]["Plan"]


def main_nodes(node: dict):
    """Every plan node outside SubPlans / InitPlans (the cover image's own LIMIT 1 lookup is one per product row)."""
    yield node
    for child in node.get("Plans", []):
        if child.get("Parent Relationship") not in ("SubPlan", "InitPlan"):
            yield from main_nodes(child)


def assert_read_in_index_order(plan: dict, index: str, *, max_rows: int) -> None:
    nodes = list(main_nodes(plan))
    sorts = [n for n in nodes if n["Node Type"] in ("Sort", "Incremental Sort")]
    assert not sorts, f"the catalogue page sorts: {[n.get('Sort Key') for n in sorts]}"
    scans = [n for n in nodes if n.get("Relation Name") == "products"]
    assert [n.get("Index Name") for n in scans] == [index], [(n["Node Type"], n.get("Index Name")) for n in scans]
    assert scans[0]["Actual Rows"] * scans[0].get("Actual Loops", 1) <= max_rows  # one page read, not the catalogue


async def big_catalogue(db, gym, n: int = BIG) -> None:
    """``n`` products in one INSERT ... SELECT: 1 in 7 out of stock, 1 in 50 inactive, 1 in 3 rated (1-5 stars, 1-20
    reviews), all different creation times; then VACUUM ANALYZE (the planner sees the real table)."""
    await db.execute(text(
        "INSERT INTO products (gym_id, name, slug, category, description, price, currency, stock, is_active, created_at,"
        " rating_avg, review_count) "
        "SELECT :gym, 'Bulk ' || g, 'bulk-' || g, (ARRAY['supplements','apparel','equipment','accessories'])[1 + g % 4],"
        " 'Seeded in bulk.', 100 + g % 50, :currency, CASE WHEN g % 7 = 0 THEN 0 ELSE 10 END, g % 50 <> 0,"
        " now() - make_interval(secs => g), CASE WHEN g % 3 = 0 THEN round(1 + (g % 400) / 100.0, 4) END,"
        " CASE WHEN g % 3 = 0 THEN 1 + g % 20 ELSE 0 END "
        "FROM generate_series(1, :n) AS g"), {"gym": gym.id, "currency": gym.currency, "n": n})
    await db.commit()
    async with dbmod.get_engine().connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.exec_driver_sql("VACUUM ANALYZE products")
        await conn.exec_driver_sql("ANALYZE gyms")


# ------------------------------------------------------------------ in_stock: computed by PostgreSQL
async def test_in_stock_is_computed_by_postgres_whoever_writes_the_stock(db):
    _, gym, _ = await shop_setup(db)
    sold_out = await make_product(db, gym, stock=0)
    stocked = await make_product(db, gym, stock=4)
    assert (await in_stock_of(sold_out.id), await in_stock_of(stocked.id)) == (False, True)
    stocked.stock = 0  # the ORM (a sale, an owner's adjustment ...)
    await db.commit()
    assert await in_stock_of(stocked.id) is False
    await db.execute(text("UPDATE products SET stock = 3 WHERE id = :id"), {"id": sold_out.id})  # raw SQL / COPY
    await db.commit()
    assert await in_stock_of(sold_out.id) is True
    assert Product.__table__.c.in_stock.computed.persisted is True  # STORED: it can be indexed
    loaded = await db.scalar(select(Product).where(Product.id == stocked.id))
    assert "in_stock" not in loaded.__dict__  # SQL only: never loaded with a Product (deferred, raiseload)


# ------------------------------------------------------------------ the review summary follows every review change
async def test_review_summary_follows_every_review_change(client, db, anon_client_factory):
    owner, gym, _ = await shop_setup(db)
    p = await make_product(db, gym, name="Summarised Whey")
    other = await make_product(db, gym, name="Untouched Bar")
    first, second = await buyer(db, gym, p), await buyer(db, gym, p)
    assert await summary(p.id) == (None, 0)

    await login_as(client, first)
    assert (await post_form(client, f"/shop/p/{p.id}/review", {"rating": 4, "title": "Good"})).status_code == 303
    assert await summary(p.id) == (Decimal("4.0000"), 1)  # a new review
    second_client = anon_client_factory("203.0.113.61")
    await login_as(second_client, second)
    await post_form(second_client, f"/shop/p/{p.id}/review", {"rating": 1, "title": "Clumpy"})
    assert await summary(p.id) == (Decimal("2.5000"), 2)
    await post_form(client, f"/shop/p/{p.id}/review", {"rating": 5, "title": "Better than I said"})
    assert await summary(p.id) == (Decimal("3.0000"), 2)  # an edited review

    clumpy = await db.scalar(select(ProductReview).where(ProductReview.user_id == second.id))
    owner_client = anon_client_factory("203.0.113.62")
    await login_as(owner_client, owner)
    await post_form(owner_client, f"/owner/shop/reviews/{clumpy.id}/toggle", {})  # the owner hides it
    assert await summary(p.id) == (Decimal("5.0000"), 1)
    await post_form(owner_client, f"/owner/shop/reviews/{clumpy.id}/toggle", {})  # ... and shows it again
    assert await summary(p.id) == (Decimal("3.0000"), 2)

    async with dbmod.get_sessionmaker()() as s:  # an ORM deletion
        await s.delete(await s.get(ProductReview, clumpy.id))
        await s.commit()
    assert await summary(p.id) == (Decimal("5.0000"), 1)
    assert await summary(other.id) == (None, 0)

    page = (await client.get("/shop")).text  # cards read the stored summary
    start = page.index(">Summarised Whey</a>")
    assert re.search(r"5\.0 · 1</span>", page[start:page.index("</article>", start)])


async def test_the_summary_is_written_in_the_reviews_own_transaction(db):
    """Written by the flush, inside the transaction: invisible to others until COMMIT, gone with a ROLLBACK - and a
    Product loaded in that session shows the new values at once (no lazy load)."""
    _, gym, _ = await shop_setup(db)
    p = await make_product(db, gym)
    member = await make_customer(db)
    async with dbmod.get_sessionmaker()() as s:
        loaded = await s.get(Product, p.id)
        s.add(ProductReview(product_id=p.id, user_id=member.id, rating=2))
        await s.flush()
        assert (loaded.rating_avg, loaded.review_count) == (Decimal("2.0000"), 1)
        assert await s.scalar(select(Product.review_count).where(Product.id == p.id)) == 1
        assert await summary(p.id) == (None, 0)  # not committed: nobody else sees it
        await s.rollback()
    assert await summary(p.id) == (None, 0)
    assert await db.scalar(select(func.count(ProductReview.id))) == 0


async def test_a_review_moved_to_another_product_updates_both(db):
    _, gym, _ = await shop_setup(db)
    a, b = await make_product(db, gym), await make_product(db, gym)
    member = await make_customer(db)
    review = ProductReview(product_id=a.id, user_id=member.id, rating=3)
    db.add(review)
    await db.commit()
    assert await summary(a.id) == (Decimal("3.0000"), 1)
    review.product_id = b.id
    await db.commit()
    assert (await summary(a.id), await summary(b.id)) == ((None, 0), (Decimal("3.0000"), 1))


async def test_concurrent_reviews_never_lose_a_count(db):
    """Two members review the same product at the same moment, in two transactions. The second flush waits for the
    first transaction's lock on the product row and only THEN aggregates (a statement of its own, so its snapshot
    includes the first review): 2 reviews, 3.0 stars. Aggregating first and waiting in the UPDATE would keep the
    snapshot from before the first COMMIT and store 1 review."""
    _, gym, _ = await shop_setup(db)
    p = await make_product(db, gym)
    a, b = await make_customer(db), await make_customer(db)
    maker = dbmod.get_sessionmaker()
    async with maker() as s1, maker() as s2:
        s1.add(ProductReview(product_id=p.id, user_id=a.id, rating=5))
        await s1.flush()  # locks the product row, writes (5.0, 1) - not committed yet
        s2.add(ProductReview(product_id=p.id, user_id=b.id, rating=1))
        second = asyncio.create_task(s2.flush())
        await asyncio.sleep(0.3)
        assert not second.done()  # waiting for the first transaction's row lock
        await s1.commit()
        await asyncio.wait_for(second, 10)
        await s2.commit()
    assert await summary(p.id) == (Decimal("3.0000"), 2)


async def test_a_review_written_meanwhile_never_deadlocks_the_summary(db):
    """Writing a review row takes FOR KEY SHARE on its product (the foreign-key check), held until COMMIT. Here the
    second member's row is written first, then the first member's flush locks the product for its summary: FOR UPDATE
    would wait for that key-share lock - and the second transaction's own summary lock then for the first one's: a
    deadlock, one review aborted. FOR NO KEY UPDATE doesn't conflict with a key-share lock: the first flush goes through
    at once, the second recompute waits for its COMMIT and counts both reviews."""
    _, gym, _ = await shop_setup(db)
    p = await make_product(db, gym)
    a, b = await make_customer(db), await make_customer(db)
    maker = dbmod.get_sessionmaker()
    async with maker() as s1, maker() as s2:
        await s2.execute(insert(ProductReview).values(product_id=p.id, user_id=b.id, rating=1))  # key share, no summary yet
        s1.add(ProductReview(product_id=p.id, user_id=a.id, rating=5))
        first = asyncio.create_task(s1.flush())
        done, _ = await asyncio.wait({first}, timeout=3)
        if not done:  # blocked by s2's key-share lock: release it so the session can be closed, then fail
            await s2.rollback()
            await first
        assert done, "the summary lock waited for another transaction's foreign-key check (a deadlock in the making)"
        second = asyncio.create_task(svc.refresh_product_ratings(s2, [p.id]))
        await asyncio.sleep(0.3)
        assert not second.done()  # the two summary writers still take turns ...
        await s1.commit()
        assert await asyncio.wait_for(second, 10) == 1  # ... and the second one sees the first one's review
        await s2.commit()
    assert await summary(p.id) == (Decimal("3.0000"), 2)


async def test_core_statements_refresh_the_summary_themselves(db):
    """A Core DELETE of reviews (or rows an ON DELETE CASCADE removes) bypasses the flush hook: such code calls
    refresh_product_ratings in its own transaction."""
    _, gym, _ = await shop_setup(db)
    p = await make_product(db, gym)
    members = [await make_customer(db) for _ in range(3)]
    db.add_all([ProductReview(product_id=p.id, user_id=m.id, rating=r) for m, r in zip(members, (5, 4, 1), strict=True)])
    await db.commit()
    assert await summary(p.id) == (Decimal("3.3333"), 3)
    await db.execute(delete(ProductReview).where(ProductReview.rating == 1))
    assert await svc.refresh_product_ratings(db, [p.id, p.id]) == 1
    await db.commit()
    assert await summary(p.id) == (Decimal("4.5000"), 2)


# ------------------------------------------------------------------ backfill / reconcile: once, idempotent, chunked
async def raw_reviews(db, rows: list[tuple[int, int, int, bool]]) -> None:
    """(product, member, stars, hidden) inserted with raw SQL - like COPY, a restore, or rows from before the columns."""
    for pid, uid, stars, hidden in rows:
        await db.execute(text("INSERT INTO product_reviews (product_id, user_id, rating, is_hidden) "
                              "VALUES (:p, :u, :r, :h)"), {"p": pid, "u": uid, "r": stars, "h": hidden})
    await db.commit()


async def xmins() -> dict[int, str]:
    async with dbmod.get_engine().connect() as conn:
        return dict((await conn.execute(text("SELECT id, xmin::text FROM products"))).all())


async def test_backfill_brings_every_product_in_line_once_and_is_idempotent(db):
    _, gym, _ = await shop_setup(db)
    products = [await make_product(db, gym) for _ in range(8)]
    members = [await make_customer(db) for _ in range(3)]
    p = [x.id for x in products]
    await raw_reviews(db, [(p[0], members[0].id, 5, False), (p[0], members[1].id, 2, False),
                           (p[1], members[0].id, 4, False), (p[2], members[0].id, 3, False),
                           (p[2], members[1].id, 1, True),  # hidden: never counted
                           (p[4], members[2].id, 1, False), (p[6], members[0].id, 2, True)])
    await db.execute(text("UPDATE products SET review_count = 9, rating_avg = 4.5 WHERE id = :id"), {"id": p[5]})
    await db.commit()
    assert [await summary(i) for i in p[:3]] == [(None, 0)] * 3  # stale: written past the hook

    fixed = await svc.backfill_product_ratings(batch=3, budget_s=None)  # 3 chunks of 3 products
    assert fixed == 5  # p0, p1, p2, p4 (reviews) and p5 (a summary without reviews); p3, p6 (hidden only), p7 in line
    assert [await summary(i) for i in p] == [
        (Decimal("3.5000"), 2), (Decimal("4.0000"), 1), (Decimal("3.0000"), 1), (None, 0), (Decimal("1.0000"), 1),
        (None, 0), (None, 0), (None, 0)]

    before = await xmins()
    assert await svc.backfill_product_ratings(batch=3, budget_s=None) == 0  # idempotent ...
    assert await xmins() == before  # ... and writes nothing at all once the summaries are right


async def test_the_reconcile_run_skips_a_product_locked_right_now(db):
    _, gym, _ = await shop_setup(db)
    busy, idle = await make_product(db, gym), await make_product(db, gym)
    member = await make_customer(db)
    await raw_reviews(db, [(busy.id, member.id, 4, False), (idle.id, member.id, 2, False)])
    async with dbmod.get_sessionmaker()() as other:  # e.g. a checkout holding the row
        await other.execute(select(Product.id).where(Product.id == busy.id).with_for_update())
        assert await svc.backfill_product_ratings(budget_s=None) == 1  # SKIP LOCKED: never waits for it
        assert (await summary(busy.id), await summary(idle.id)) == ((None, 0), (Decimal("2.0000"), 1))
        await other.rollback()
    assert await svc.backfill_product_ratings(budget_s=None) == 1  # the next run catches up
    assert await summary(busy.id) == (Decimal("4.0000"), 1)


async def test_the_reconcile_locks_and_rewrites_at_most_200_products_per_transaction(db, monkeypatch):
    """A summary rewrite is not a HOT update (its columns are indexed): every index of products - HNSW too, ~5 ms a
    product at 100,000 products - gets a new entry, and a checkout waits for a product the run holds. So the one-off
    backfill of a big catalogue goes RATINGS_BATCH (200) products per transaction, never a thousand at once."""
    _, gym, _ = await shop_setup(db)
    await db.execute(text(
        "INSERT INTO products (gym_id, name, slug, category, description, price, currency, review_count) "
        "SELECT :gym, 'Drifted ' || g, 'drifted-' || g, 'equipment', 'A stale summary.', 10, :currency, 3 "
        "FROM generate_series(1, 450) AS g"), {"gym": gym.id, "currency": gym.currency})
    await db.commit()
    sizes: list[int] = []
    real = svc._refresh_ratings_sync

    def spy(session, product_ids, **kwargs):  # noqa: ANN001, ANN003 - one call = one reconcile transaction
        ids = list(product_ids)
        sizes.append(len(ids))
        return real(session, ids, **kwargs)

    monkeypatch.setattr(svc, "_refresh_ratings_sync", spy)
    assert await svc.backfill_product_ratings(budget_s=None) == 450
    assert sizes == [200, 200, 50] and svc.RATINGS_BATCH == 200
    assert await db.scalar(select(func.count()).where(Product.review_count != 0)) == 0


async def test_the_hourly_periodic_runs_the_backfill_under_its_lock(db):
    task = registered_periodic()[svc.RATINGS_TASK]
    assert task.every_seconds == svc.RATINGS_EVERY_S == 3600
    _, gym, _ = await shop_setup(db)
    p = await make_product(db, gym)
    member = await make_customer(db)
    await raw_reviews(db, [(p.id, member.id, 3, False)])
    assert await run_periodic(task) is True
    assert await summary(p.id) == (Decimal("3.0000"), 1)


async def test_a_purged_reviewer_is_reconciled(db):
    """Reviews removed by the ON DELETE CASCADE of an account purge (a Core DELETE of users) bypass the flush hook:
    the hourly reconcile brings the product back in line."""
    from app.models import User

    _, gym, _ = await shop_setup(db)
    p = await make_product(db, gym)
    keep, gone = await make_customer(db), await make_customer(db)
    db.add_all([ProductReview(product_id=p.id, user_id=keep.id, rating=4),
                ProductReview(product_id=p.id, user_id=gone.id, rating=2)])
    await db.commit()
    assert await summary(p.id) == (Decimal("3.0000"), 2)
    await db.execute(delete(User).where(User.id == gone.id))
    await db.commit()
    assert await svc.backfill_product_ratings(budget_s=None) == 1
    assert await summary(p.id) == (Decimal("4.0000"), 1)


async def test_the_account_purge_rewrites_the_summary_of_the_products_its_member_reviewed(db):
    """The hourly account purge removes a member's reviews through ON DELETE CASCADE (past the flush hook): it notes
    the products they reviewed and rewrites their summary in the purge's own transaction - the cards and the "Top
    rated" order never count a purged member's stars, and the reconcile run finds nothing left to fix."""
    from datetime import timedelta

    from app.core.utils import utcnow
    from app.services.accounts import purge_due_accounts

    _, gym, _ = await shop_setup(db)
    p, q = await make_product(db, gym), await make_product(db, gym)
    keep, gone = await make_customer(db), await make_customer(db)
    db.add_all([ProductReview(product_id=p.id, user_id=keep.id, rating=4),
                ProductReview(product_id=p.id, user_id=gone.id, rating=2),
                ProductReview(product_id=q.id, user_id=gone.id, rating=5)])
    await db.commit()
    assert (await summary(p.id), await summary(q.id)) == ((Decimal("3.0000"), 2), (Decimal("5.0000"), 1))
    gone.deleted_at, gone.purge_after = utcnow() - timedelta(days=31), utcnow() - timedelta(minutes=1)
    await db.commit()
    assert (await purge_due_accounts(db))["users"] == 1
    await db.commit()
    assert await summary(p.id) == (Decimal("4.0000"), 1)
    assert await summary(q.id) == (None, 0)
    assert await svc.backfill_product_ratings(budget_s=None) == 0


# ------------------------------------------------------------------ schema: an existing database gets the columns
async def test_sync_schema_adds_the_columns_and_indexes_and_the_backfill_fills_them(db):
    engine = dbmod.get_engine()
    names = (FEATURED_INDEX, RATING_INDEX, NEWEST_INDEX, PRICE_INDEX, "ix_job_postings_open_recent")
    _, gym, _ = await shop_setup(db)
    member = await make_customer(db)
    await db.commit()
    try:
        async with engine.begin() as conn:  # a database from before this change
            for name in names:
                await conn.execute(text(f"DROP INDEX {name}"))
            await conn.execute(text("ALTER TABLE products DROP COLUMN in_stock, DROP COLUMN rating_avg, "
                                    "DROP COLUMN review_count"))
            ids = (await conn.execute(text(
                "INSERT INTO products (gym_id, name, slug, category, description, price, currency, stock) "
                "SELECT :gym, 'Old ' || g, 'old-' || g, 'equipment', 'From before.', 10, 'INR', g % 2 "
                "FROM generate_series(1, 4) AS g RETURNING id"), {"gym": gym.id})).scalars().all()
            await conn.execute(text("INSERT INTO product_reviews (product_id, user_id, rating) VALUES (:p, :u, 4)"),
                               {"p": ids[0], "u": member.id})
        async with engine.begin() as conn:
            applied = await dbmod.sync_schema(conn)
        assert {"column products.in_stock", "column products.rating_avg", "column products.review_count",
                *(f"index {n}" for n in names)} <= set(applied), applied
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT stock, in_stock, rating_avg, review_count FROM products "
                                            "WHERE id = ANY(:ids) ORDER BY id"), {"ids": ids})).all()
        assert [tuple(r) for r in rows] == [(1, True, None, 0), (0, False, None, 0), (1, True, None, 0), (0, False, None, 0)]
        assert await svc.backfill_product_ratings(budget_s=None) == 1  # the one-off backfill
        assert await summary(ids[0]) == (Decimal("4.0000"), 1)
        async with engine.begin() as conn:
            assert await dbmod.sync_schema(conn) == []  # idempotent

        async with engine.begin() as conn:  # a large production database: built CONCURRENTLY before the deploy
            for name in names:
                await conn.execute(text(f"DROP INDEX {name}"))
        assert set(names) <= set(await dbmod.create_indexes_concurrently(engine))
    finally:
        async with engine.begin() as conn:  # never leave the session's schema behind
            await dbmod.sync_schema(conn)
    async with engine.connect() as conn:
        present = set((await conn.execute(text("SELECT indexname FROM pg_indexes WHERE indexname = ANY(:n)"),
                                          {"n": list(names)})).scalars())
    assert present == set(names)


# ------------------------------------------------------------------ the orders, at 100,000 products
async def test_featured_and_top_rated_are_read_in_index_order_at_100k_products(client, db):
    _, gym, _ = await shop_setup(db)
    for i in range(3):
        await make_product(db, gym, name=f"Few {i}", stock=i)
    urls = ("/shop", "/shop?sort=rating", "/shop?in_stock=1", "/shop?page=300", "/shop?sort=rating&page=120",
            "/shop?category=apparel", "/shop?sort=newest", "/shop?sort=price_asc", "/shop?sort=price_desc&page=40")
    small = {}
    for url in urls:
        ttl_cache.get_cache("shop.category_counts").reset()
        ttl_cache.get_cache("shop.top_stores").reset()
        with QueryCounter() as qc:
            assert (await client.get(url)).status_code == 200
        small[url] = qc.count

    await big_catalogue(db, gym)
    large = {}
    for url in urls:
        ttl_cache.get_cache("shop.category_counts").reset()
        ttl_cache.get_cache("shop.top_stores").reset()
        with QueryCounter() as qc:
            assert (await client.get(url)).status_code == 200
        large[url] = qc.count
    assert large == small  # the page's statement count is unchanged by the catalogue's size

    for url, index, max_rows in (("/shop", FEATURED_INDEX, 100), ("/shop?in_stock=1", FEATURED_INDEX, 100),
                                 ("/shop?category=apparel", FEATURED_INDEX, 100),
                                 ("/shop?page=300", FEATURED_INDEX, 8000), ("/shop?sort=rating", RATING_INDEX, 100),
                                 ("/shop?sort=rating&page=120", RATING_INDEX, 3500), ("/shop?sort=newest", NEWEST_INDEX, 100),
                                 ("/shop?sort=price_asc", PRICE_INDEX, 100),
                                 ("/shop?sort=price_desc&page=40", PRICE_INDEX, 1200)):
        statement, params = page_statement(await statements_of(client, url))
        if "rating" in url:
            assert "product_reviews" not in statement and "avg(" not in statement  # the stored summary, no aggregate
        assert_read_in_index_order(await explain(statement, params), index, max_rows=max_rows)


async def test_featured_and_top_rated_orders(client, db):
    _, gym, _ = await shop_setup(db)
    members = [await make_customer(db) for _ in range(3)]
    old_out = await make_product(db, gym, name="Old Sold Out", stock=0)
    old_in = await make_product(db, gym, name="Old Stocked")
    new_out = await make_product(db, gym, name="New Sold Out", stock=0)
    new_in = await make_product(db, gym, name="New Stocked")
    reviews = {old_out: (5, 4), old_in: (5, 4, 5), new_out: (2,), new_in: ()}  # 4.5 x2, 4.67 x3, 2.0 x1, none
    for product, stars in reviews.items():
        db.add_all([ProductReview(product_id=product.id, user_id=m.id, rating=s) for m, s in zip(members, stars)])
    await db.commit()

    def names(html: str) -> list[str]:
        return re.findall(r">(Old Sold Out|Old Stocked|New Sold Out|New Stocked)</a>", html)

    assert names((await client.get("/shop")).text) == ["New Stocked", "Old Stocked", "New Sold Out", "Old Sold Out"]
    assert names((await client.get("/shop?sort=rating")).text) == ["Old Stocked", "Old Sold Out", "New Sold Out",
                                                                   "New Stocked"]
    assert names((await client.get("/shop?in_stock=1")).text) == ["New Stocked", "Old Stocked"]
    assert re.search(r"4\.7 · 3</span>", (await client.get("/shop?sort=rating")).text)
