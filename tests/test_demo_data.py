"""Demo gyms, accounts and shops exist in the database only - never hardcoded in the app.

* No file under app/ contains demo records (gym names, people, e-mails, product names/SKUs from dataset.json) or a
  built-in sample catalogue.
* The loader (scripts/demo/loader.py) writes the whole dataset into PostgreSQL, is idempotent, and the data it
  writes is self-consistent (stock, refunds, plans, minors' guardians, suspended member).
* Pages show whatever is in the database: rename/delete rows and the pages follow.
"""

from __future__ import annotations

import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select, update

from app.models import (
    ChatThread,
    CustomerProfile,
    Employee,
    Gym,
    InventoryMovement,
    Plan,
    Product,
    ProductImage,
    Refund,
    ShopOrder,
    Subscription,
    TrainerShift,
    User,
)
from app.models.enums import Role
from app.models.shop import OrderStatus, RefundStatus
from scripts.demo.loader import DATASET_PATH, load_dataset, seed_demo
from tests.conftest import login_as, make_client

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
DATA = load_dataset()
DOMAIN = DATA["email_domain"]


def _demo_strings() -> set[str]:
    """Distinctive values from the dataset that must never appear in application code."""
    out: set[str] = set()
    for key, c in DATA["catalogue"].items():
        out |= {c["name"], c["sku"]}
    for g in DATA["gyms"]:
        out |= {g["name"], g["slug"]}
        for person in [g["owner"], *g.get("staff", []), *g.get("members", [])]:
            out |= {person["name"], f"{person['key']}@{DOMAIN}"}
        for b in g.get("branches", []):
            out.add(b["address"])
        for c in g.get("coupons", []) + g.get("shop", {}).get("coupons", []):
            out.add(c["code"])
    return {s for s in out if len(s) >= 6}


def test_app_code_contains_no_demo_records():
    needles = _demo_strings()
    pattern = re.compile("|".join(re.escape(n) for n in sorted(needles, key=len, reverse=True)), re.IGNORECASE)
    hits = []
    for path in APP.rglob("*"):
        if path.is_dir() or path.suffix not in {".py", ".html", ".js", ".css", ".json", ".txt", ".md"}:
            continue
        if "static/dist" in path.as_posix() or "__pycache__" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if m := pattern.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{lineno}: {m.group(0)!r}")
    assert not hits, "demo data is hardcoded in the app (it must live in the database only):\n" + "\n".join(hits)


def test_app_has_no_builtin_sample_catalogue_or_seed_data():
    from app.main import app
    from app.services import shop

    assert not any(hasattr(shop, name) for name in ("SAMPLE_PRODUCTS", "create_sample_products", "SAMPLE_SKU_PREFIX"))
    paths = {getattr(r, "path", "") for r in app.routes}
    for r in app.routes:  # FastAPI 0.16x wraps included routers
        inner = getattr(r, "original_router", None)
        paths |= {getattr(x, "path", "") for x in getattr(inner, "routes", [])}
    assert not [p for p in paths if p.endswith("/shop/samples")]
    assert not (APP / "services" / "seed.py").exists(), "demo data must not live in the app package"


def test_dataset_references_are_consistent():
    keys = Counter()
    phones = Counter()
    for g in DATA["gyms"]:
        branches = {b["name"] for b in g.get("branches", [])}
        staff = {s["key"] for s in g.get("staff", [])}
        members = {m["key"] for m in g.get("members", [])}
        for person in [g["owner"], *g.get("staff", []), *g.get("members", [])]:
            keys[person["key"]] += 1
            phones[re.sub(r"\D", "", person["phone"])] += 1
        for s in g.get("staff", []):
            for sh in s.get("shifts", []):
                assert sh.get("online") or sh["branch"] in branches, (g["slug"], s["key"], sh)
        for m in g.get("members", []):
            if m.get("age") is not None and m["age"] < 18:
                assert m.get("guardian"), f"{m['key']} is a minor without a guardian"
            for sub in m.get("subscriptions", []):
                if not sub.get("gym"):
                    assert not sub.get("branch") or sub["branch"] in branches
                    assert not sub.get("trainer") or sub["trainer"] in staff
        for o in g.get("shop", {}).get("orders", []):
            assert o["member"] in members
            assert all(k in g["shop"]["products"] for k, _ in o["items"]), o["ref"]
            assert o["fulfillment"] == "delivery" or o["branch"] in branches
            if o["fulfillment"] == "pickup":  # delivery-only products can't be collected
                assert all(DATA["catalogue"][k]["fulfillment"] != "delivery" for k, _ in o["items"]), o["ref"]
    assert not [k for k, n in keys.items() if n > 1], "duplicate person keys (e-mails)"
    assert not [p for p, n in phones.items() if n > 1], "duplicate phone numbers"


async def test_loader_writes_the_whole_dataset_once(db):
    first = await seed_demo(db, password="Demo-Passw0rd!")
    await db.commit()
    people = sum(1 + len(g.get("staff", [])) + len(g.get("members", [])) for g in DATA["gyms"])
    assert first["created"] and first["stats"]["accounts"] == people
    assert await db.scalar(select(func.count(Gym.id))) == len(DATA["gyms"])
    assert await db.scalar(select(func.count(User.id)).where(User.email.ilike(f"%@{DOMAIN}"))) == people
    shop_products = sum(len(g.get("shop", {}).get("products", [])) for g in DATA["gyms"])
    assert await db.scalar(select(func.count(Product.id))) == shop_products
    assert await db.scalar(select(func.count(ProductImage.id))) >= shop_products

    # every product's final stock equals the dataset's, after the seeded orders and restocked refunds
    for g in DATA["gyms"]:
        gym = await db.scalar(select(Gym).where(Gym.slug == g["slug"]))
        for key in g.get("shop", {}).get("products", []):
            c = DATA["catalogue"][key]
            product = await db.scalar(select(Product).where(Product.gym_id == gym.id, Product.sku == c["sku"]))
            assert product.stock == g["shop"].get("stock", {}).get(key, c["stock"]), (g["slug"], key)
            moved = await db.scalar(select(func.coalesce(func.sum(InventoryMovement.change), 0))
                                    .where(InventoryMovement.product_id == product.id))
            assert moved == product.stock, (g["slug"], key)  # the inventory ledger adds up
            assert product.currency == g["currency"]

    # AED prices are converted, plans and shop orders use the gym's currency
    desert = await db.scalar(select(Gym).where(Gym.slug == "desert-strength"))
    whey = await db.scalar(select(Product).where(Product.gym_id == desert.id, Product.sku == "PF-WHEY-ISO-1KG"))
    assert whey.price == Decimal("131.99") and whey.currency == "AED"
    assert not await db.scalar(select(func.count(Plan.id)).where(Plan.gym_id == desert.id, Plan.currency != "AED"))

    # the online-only gym sells online plans only, and delivers everything
    anywhere = await db.scalar(select(Gym).where(Gym.slug == "pulse-anywhere"))
    enabled = (await db.execute(select(Plan.code).where(Plan.gym_id == anywhere.id, Plan.is_enabled.is_(True)))).scalars().all()
    assert enabled and all(code.startswith("online") for code in enabled)
    assert not await db.scalar(select(func.count(Product.id)).where(Product.gym_id == anywhere.id, Product.fulfillment != "delivery"))

    # refunds: one completed partial, one completed full, two requested
    refunds = (await db.execute(select(Refund))).scalars().all()
    assert Counter(r.status for r in refunds) == {RefundStatus.COMPLETED: 2, RefundStatus.REQUESTED: 2}
    partial = await db.scalar(select(ShopOrder).where(ShopOrder.provider_order_id == "DEMO-FF-1004"))
    assert partial.status == OrderStatus.PARTIALLY_REFUNDED and Decimal("0") < partial.refunded_total < partial.total

    # minors have guardians; the suspended member is suspended; trainers have shifts; chats exist
    minors = (await db.execute(select(User).where(User.email.ilike(f"%@{DOMAIN}")))).scalars().all()
    for u in minors:
        if u.role == Role.CUSTOMER and (u.age() or 99) < 18:
            prof = await db.get(CustomerProfile, u.id)
            assert prof.guardian_name and prof.guardian_email, u.email
    tushar = await db.scalar(select(User).where(User.email == f"tushar.malik@{DOMAIN}"))
    assert (await db.get(CustomerProfile, tushar.id)).activity_suspended
    assert await db.scalar(select(func.count(TrainerShift.id))) > 30
    assert await db.scalar(select(func.count(ChatThread.id))) == sum(len(g.get("chats", [])) for g in DATA["gyms"])
    assert await db.scalar(select(func.count(Subscription.id))) == sum(
        len(m.get("subscriptions", [])) for g in DATA["gyms"] for m in g.get("members", []))

    # idempotent: a second run adds nothing
    again = await seed_demo(db, password="Demo-Passw0rd!")
    await db.commit()
    assert not again["created"], again["stats"]
    assert await db.scalar(select(func.count(User.id)).where(User.email.ilike(f"%@{DOMAIN}"))) == people


async def test_pages_show_database_rows_not_hardcoded_data(db):
    await seed_demo(db, password="Demo-Passw0rd!", gyms=["harbor-strength"])
    await db.commit()
    gym = await db.scalar(select(Gym).where(Gym.slug == "harbor-strength"))
    member = await db.scalar(select(User).where(User.email == f"zoya.merchant@{DOMAIN}"))
    async with make_client() as client:
        await login_as(client, member)
        assert "Lever Lifting Belt" in (await client.get("/shop")).text
        assert "Harbor Strength Studio" in (await client.get("/gyms")).text

        # rename in the database -> the pages follow
        await db.execute(update(Product).where(Product.gym_id == gym.id, Product.sku == "IS-BELT-10MM")
                         .values(name="Renamed In Database Belt"))
        await db.execute(update(Gym).where(Gym.id == gym.id).values(name="Renamed In Database Gym"))
        await db.commit()
        shop_page = (await client.get("/shop")).text
        assert "Renamed In Database Belt" in shop_page and "Lever Lifting Belt" not in shop_page
        gyms_page = (await client.get("/gyms")).text
        assert "Renamed In Database Gym" in gyms_page and "Harbor Strength Studio" not in gyms_page

        # delete the rows -> nothing is left on the pages (no fallback catalogue / gyms)
        await db.execute(delete(Product).where(Product.gym_id == gym.id))
        await db.execute(delete(Employee).where(Employee.gym_id == gym.id))
        await db.commit()
        shop_page = (await client.get("/shop")).text
        assert not any(DATA["catalogue"][k]["name"] in shop_page for k in DATA["catalogue"])


def test_dataset_lives_outside_the_app_package():
    assert DATASET_PATH.is_file() and APP not in DATASET_PATH.parents
