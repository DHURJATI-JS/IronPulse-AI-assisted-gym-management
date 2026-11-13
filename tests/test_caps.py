"""Approved load-safety caps (app/core/caps.py): everything a person or a gym creates is capped on the server -
race-safe (an advisory lock per cap and scope, then the count), with a friendly refusal (form error / flash / 409-429,
never a 500), tunable in ONE table (``CAPS_OVERRIDES`` / dedicated settings) - and normal use never meets a cap.

Each test lowers the cap it checks with ``CAPS_OVERRIDES`` so the N+1st create is cheap to reach."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, insert, select, update

from app import db as dbmod
from app.config import get_settings
from app.core import caps
from app.core.utils import utcnow
from app.models import (
    BranchDocument,
    Coupon,
    CustomerProfile,
    Gym,
    IdentityDocument,
    InventoryMovement,
    JobEnquiry,
    Notification,
    Product,
    ShopOrder,
    Task,
)
from app.models.enums import BranchDocType, IdDocType, IdStatus, NotificationCategory, Severity
from app.models.shop import OrderStatus
from app.services import quotas, retention
from app.services import tasks as task_service
from tests.conftest import login_as, make_client, post_form
from tests.factories import make_branch, make_customer, make_gym, make_owner, png_bytes
from tests.test_job_enquiry_spam import form as enquiry_form
from tests.test_job_enquiry_spam import url as enquiry_url
from tests.test_owner_customers import id_files, reg_form
from tests.test_owner_gyms import add_doc, branch_data
from tests.test_public import make_job, published_gym
from tests.test_shop import make_order, make_product, shop_setup
from tests.test_shop_admin import PRODUCT_FORM, form_with_lists
from tests.test_shop_payments import bag, checkout
from tests.test_superadmin import flash_text


@pytest.fixture
def cap_values(monkeypatch: pytest.MonkeyPatch):
    """cap_values(branches_per_gym=1, ...) lowers caps for one test (the CAPS_OVERRIDES setting)."""

    def lower(**values: int) -> None:
        monkeypatch.setattr(get_settings(), "caps_overrides", ",".join(f"{k}={v}" for k, v in values.items()))

    return lower


async def count(db: Any, stmt: Any) -> int:
    return int(await db.scalar(stmt.execution_options(populate_existing=True)) or 0)


# ====================================================================================================== the table
def test_every_cap_has_a_value_a_scope_and_a_friendly_sentence():
    for cap in caps._CAPS:
        assert cap.limit >= 1 and cap.noun and cap.per.startswith("per ")
        text = str(caps.CapReached(cap))
        assert "{limit}" not in text and text.endswith(".") and len(text) < 300, cap.name
        assert caps.CapReached(cap).status_code == (429 if cap.window_hours else 409)


def test_overrides_tune_any_cap_and_ignore_junk(cap_values):
    cap_values(branches_per_gym=300, products_per_gym=5000)
    assert (caps.limit("branches_per_gym"), caps.limit("products_per_gym")) == (300, 5000)
    get_settings().caps_overrides = "nonsense=5,branches_per_gym=0,coupons_per_gym=abc,online_gyms=9"
    assert caps.limit("branches_per_gym") == caps.CAPS["branches_per_gym"].default
    assert caps.limit("coupons_per_gym") == caps.CAPS["coupons_per_gym"].default
    assert caps.limit("online_gyms") == 3  # a cap with its own setting is tuned there, never by CAPS_OVERRIDES


def test_the_refusal_is_a_documented_json_payload():
    exc = caps.CapReached("coupons_per_gym")
    assert exc.payload() == {"error": "Limit reached", "detail": str(exc), "code": "cap_reached",
                             "cap": "coupons_per_gym", "limit": 200}
    day = caps.CapReached("orders_per_member_day")
    assert day.status_code == 429 and day.headers == {"Retry-After": "3600"}


# ====================================================================================================== race safety
async def test_parallel_creates_never_pass_a_cap(db, cap_values):
    """8 transactions create a coupon for one gym at the same moment; the cap is 3: exactly 3 commit."""
    cap_values(coupons_per_gym=3)
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    start = asyncio.Event()

    async def create(n: int) -> bool:
        async with dbmod.get_sessionmaker()() as s:
            await start.wait()
            try:
                await caps.enforce(s, "coupons_per_gym", gym.id, select(Coupon.id).where(Coupon.gym_id == gym.id))
            except caps.CapReached:
                return False
            await asyncio.sleep(0.02)  # widen the window between the count and the insert
            s.add(Coupon(gym_id=gym.id, code=f"RACE{n}", percent_off=10))
            await s.commit()
            return True

    tasks = [asyncio.create_task(create(n)) for n in range(8)]
    start.set()
    results = await asyncio.gather(*tasks)
    assert sum(results) == 3
    assert await count(db, select(func.count(Coupon.id)).where(Coupon.gym_id == gym.id)) == 3


async def test_parallel_coupon_forms_at_the_cap(db, cap_values):
    cap_values(coupons_per_gym=2)
    owner, gym, _ = await shop_setup(db)
    clients = [make_client() for _ in range(5)]
    try:
        for c in clients:
            await login_as(c, owner)
        results = await asyncio.gather(*(post_form(c, "/owner/shop/coupons", {
            "gym_id": gym.id, "code": f"PAR{i}X", "kind": "percent", "percent_off": "10", "is_active": "1"})
            for i, c in enumerate(clients)))
    finally:
        for c in clients:
            await c.aclose()
    assert sorted(r.status_code for r in results) == [303, 303, 409, 409, 409]
    assert all("at most 2 coupons" in r.text for r in results if r.status_code == 409)
    assert await count(db, select(func.count(Coupon.id)).where(Coupon.gym_id == gym.id)) == 2


# ====================================================================================================== gyms and branches
async def test_gyms_per_owner(client, db, cap_values):
    cap_values(gyms_per_owner=1)
    owner = await make_owner(db)
    await make_gym(db, owner)
    await login_as(client, owner)
    r = await post_form(client, "/owner/gyms/new", {"name": "Second Gym", "country_code": "IN", "currency": "INR"})
    assert r.status_code == 409 and "at most 1 gyms from one account" in r.text.replace("&#39;", "'")
    assert await count(db, select(func.count(Gym.id)).where(Gym.owner_id == owner.id)) == 1


async def test_branches_per_gym(client, db, cap_values):
    cap_values(branches_per_gym=1)
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    await make_branch(db, gym)
    await login_as(client, owner)
    r = await post_form(client, f"/owner/gyms/{gym.id}/branches/new", branch_data(name="One Too Many"))
    assert r.status_code == 409 and "A gym can have at most 1 branches (closed ones included)" in r.text


async def test_branch_documents_and_the_gym_storage_quota(client, db, cap_values):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    branch = await make_branch(db, gym, verified=False, photos=0)
    await add_doc(db, branch, BranchDocType.TAX_REGISTRATION, text="one")
    await add_doc(db, branch, BranchDocType.LIABILITY_INSURANCE, text="two")
    await login_as(client, owner)
    upload = {"file": ("fire.png", png_bytes(text="fire"), "image/png")}
    cap_values(branch_documents=2)
    r = await post_form(client, f"/owner/branches/{branch.id}/documents", {"doc_type": "fire_safety_certificate"},
                        files=upload)
    assert r.status_code == 400 and "A branch can hold at most 2 documents" in r.text
    cap_values(storage_mb_per_gym=1)  # the gym already keeps (almost) 1 MB
    await db.execute(update(BranchDocument).where(BranchDocument.branch_id == branch.id).values(size_bytes=524_000))
    await db.commit()
    r = await post_form(client, f"/owner/branches/{branch.id}/documents", {"doc_type": "fire_safety_certificate"},
                        files={"file": ("fire.png", png_bytes(text="fire 2"), "image/png")})
    assert r.status_code == 400 and "1 MB storage quota" in r.text
    assert await count(db, select(func.count(BranchDocument.id)).where(BranchDocument.branch_id == branch.id)) == 2
    used, quota = await quotas.gym_usage(db, gym.id)
    assert used == 1_048_000 and quota == caps.MB


async def test_branch_photos_count_towards_the_gym_quota(client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    branch = await make_branch(db, gym, photos=0)
    await login_as(client, owner)
    r = await post_form(client, f"/owner/branches/{branch.id}/photos", {"kind": "other"},
                        files=[("photos", ("a.png", png_bytes((200, 200)), "image/png"))])
    assert r.status_code == 303
    used, _ = await quotas.gym_usage(db, gym.id)
    assert used > 0  # the stored photo's size is recorded


# ====================================================================================================== shop
async def test_products_per_gym_and_coupons_per_gym(client, db, cap_values):
    cap_values(products_per_gym=1, coupons_per_gym=1)
    owner, gym, _ = await shop_setup(db)
    await make_product(db, gym)
    db.add(Coupon(gym_id=gym.id, code="FIRST", percent_off=10))
    await db.commit()
    await login_as(client, owner)
    r = await form_with_lists(client, f"/owner/gyms/{gym.id}/shop/products/new", PRODUCT_FORM)
    assert r.status_code == 409 and "can list at most 1 products" in r.text
    r = await post_form(client, "/owner/shop/coupons", {"gym_id": gym.id, "code": "SECOND", "kind": "percent",
                                                        "percent_off": "10", "is_active": "1"})
    assert r.status_code == 409 and "at most 1 coupons" in r.text
    assert await count(db, select(func.count(Product.id)).where(Product.gym_id == gym.id)) == 1


async def test_manual_stock_changes_per_product_per_day(client, db, cap_values):
    cap_values(stock_changes_per_product_day=2)
    owner, gym, _ = await shop_setup(db)
    p = await make_product(db, gym, stock=10)
    await login_as(client, owner)
    for _ in range(2):
        r = await post_form(client, f"/owner/shop/products/{p.id}/stock",
                            {"mode": "restock", "quantity": 1, "reason": "New delivery"})
        assert "Stock updated" in flash_text(r)
    r = await post_form(client, f"/owner/shop/products/{p.id}/stock", {"mode": "restock", "quantity": 1, "reason": "More"})
    assert "changed 2 times in the last 24 hours" in flash_text(r)
    assert await count(db, select(Product.stock).where(Product.id == p.id)) == 12


async def test_unpaid_orders_and_orders_per_day(client, db, cap_values):
    owner, gym, branch = await shop_setup(db)
    _, other_gym, other_branch = await shop_setup(db)
    cust = await make_customer(db)
    product = await make_product(db, gym)
    other = await make_product(db, other_gym)
    await make_order(db, cust, other_gym, [(other, 1)], status=OrderStatus.PENDING_PAYMENT)
    await bag(db, cust, product, 1)
    await login_as(client, cust)
    cap_values(open_orders_per_member=1)
    r = await checkout(client, gym, branch)
    assert r.status_code == 400 and "You have 1 unpaid orders" in r.text
    cap_values(orders_per_member_day=1)
    r = await checkout(client, gym, branch)
    assert r.status_code == 400 and "placed 1 orders in the last 24 hours" in r.text.replace("&#39;", "'")
    assert await count(db, select(func.count(ShopOrder.id)).where(ShopOrder.customer_id == cust.id)) == 1
    cap_values()  # normal use: one more order goes through and supersedes nothing at the other store
    assert (await checkout(client, gym, branch)).status_code == 303


# ====================================================================================================== accounts
async def _old_id_card(db: Any, user: Any, *, size: int = 0) -> None:
    db.add(IdentityDocument(user_id=user.id, doc_type=IdDocType.AADHAAR, country_code="IN", front_key="private/x/0.bin",
                            status=IdStatus.REJECTED, size_bytes=size, created_at=utcnow() - timedelta(days=3)))
    await db.commit()


async def test_id_uploads_per_account_and_the_member_storage_quota(client, db, cap_values):
    cust = await make_customer(db, id_status=IdStatus.REJECTED)
    await _old_id_card(db, cust, size=600_000)
    await login_as(client, cust)
    files = {"id_front": ("front.png", png_bytes((300, 200), text="ID"), "image/png")}
    cap_values(id_documents_per_user=1)
    r = await post_form(client, "/account/identity", {"id_doc_type": "aadhaar", "id_country": "IN"}, files=files)
    assert r.status_code == 400 and "reached 1 ID card uploads" in r.text
    cap_values(storage_mb_per_member=1)
    await db.execute(update(IdentityDocument).where(IdentityDocument.user_id == cust.id).values(size_bytes=1_048_000))
    await db.commit()
    r = await post_form(client, "/account/identity", {"id_doc_type": "aadhaar", "id_country": "IN"}, files=files)
    assert r.status_code == 400 and "1 MB storage quota" in r.text
    assert await count(db, select(func.count(IdentityDocument.id)).where(IdentityDocument.user_id == cust.id)) == 1
    cap_values()
    r = await post_form(client, "/account/identity", {"id_doc_type": "aadhaar", "id_country": "IN"}, files=files)
    assert r.status_code == 303
    size = await count(db, select(IdentityDocument.size_bytes).order_by(IdentityDocument.id.desc()).limit(1))
    assert size > 0  # the new upload's bytes are recorded for the quota


async def test_members_registered_per_owner_per_day(client, db, cap_values):
    cap_values(members_added_per_owner_day=1)
    owner = await make_owner(db)
    await make_gym(db, owner)
    await login_as(client, owner)
    assert (await post_form(client, "/owner/customers/new", reg_form(), files=id_files())).status_code == 303
    r = await post_form(client, "/owner/customers/new", reg_form(full_name="Second Member", email="second@example.com",
                                                                 phone="98765 00000"), files=id_files())
    assert r.status_code == 400 and "registered 1 members in the last 24 hours" in r.text.replace("&#39;", "'")
    assert await count(db, select(func.count(CustomerProfile.user_id)).where(
        CustomerProfile.registered_by_id == owner.id)) == 1


async def test_tasks_per_person_per_day(db, cap_values):
    cap_values(tasks_per_person_day=2)
    from app.models.enums import TaskKind

    user = await make_customer(db)
    user_id = user.id
    data = task_service.TaskInput(title="Stretch", notes=None, kind=TaskKind.TASK, due_at=None, remind_at=None,
                                  all_day=False)
    for _ in range(2):
        await task_service.create_task(db, user, data)
        await db.commit()
    with pytest.raises(task_service.TaskLimitError) as refused:
        await task_service.create_task(db, user, data)
    await db.rollback()
    assert "added 2 tasks in the last 24 hours" in refused.value.message
    assert await count(db, select(func.count(Task.id)).where(Task.user_id == user_id)) == 2


async def test_job_enquiries_per_posting_per_day(db, anon_client_factory, cap_values):
    cap_values(enquiries_per_job_day=1)
    _, gym, _ = await published_gym(db)
    job = await make_job(db, gym)
    first, second = anon_client_factory("203.0.113.1"), anon_client_factory("203.0.113.2")
    assert (await post_form(first, enquiry_url(job), enquiry_form(job))).status_code == 303
    r = await post_form(second, enquiry_url(job), enquiry_form(job, email="other@example.com",
                                                               message="A different message from another person here."))
    assert r.status_code == 429 and "inbox is full for today" in r.text and r.headers.get("retry-after")
    assert await count(db, select(func.count(JobEnquiry.id))) == 1


async def test_notifications_beyond_the_newest_are_trimmed(db, cap_values):
    user, other = await make_customer(db), await make_customer(db)
    now = utcnow()
    await db.execute(insert(Notification), [
        {"user_id": u.id, "title": f"n{i}", "body": "b", "category": NotificationCategory.SYSTEM,
         "severity": Severity.INFO, "created_at": now - timedelta(minutes=10 - i)}
        for u in (user, other) for i in range(6)])
    await db.commit()
    cap_values(notifications_kept=4)
    report = await retention.run_retention()
    assert report.complete and report.deleted["notifications_over_cap"] == 4
    kept = (await db.execute(select(Notification.title).where(Notification.user_id == user.id)
                             .order_by(Notification.created_at))).scalars().all()
    assert kept == ["n2", "n3", "n4", "n5"]  # the newest 4, read or not


# ====================================================================================================== normal use
async def test_normal_use_never_meets_a_cap(client, db):
    owner = await make_owner(db)
    await login_as(client, owner)
    r = await post_form(client, "/owner/gyms/new", {"name": "Everyday Gym", "country_code": "IN", "currency": "INR"})
    assert r.status_code == 303, r.text[:300]
    gym = await db.scalar(select(Gym).where(Gym.owner_id == owner.id))
    for i in range(3):
        r = await post_form(client, f"/owner/gyms/{gym.id}/branches/new", branch_data(name=f"Branch {i}"))
        assert r.status_code == 303, r.text[:300]
    await make_branch(db, gym)
    for i in range(3):
        r = await form_with_lists(client, f"/owner/gyms/{gym.id}/shop/products/new", {**PRODUCT_FORM, "name": f"Whey {i}",
                                                                                         "sku": f"W{i}"},
                                  files=[("images", ("p.png", png_bytes((80, 80)), "image/png"))])
        assert r.status_code == 303, r.text[:300]
        r = await post_form(client, "/owner/shop/coupons", {"gym_id": gym.id, "code": f"EVERY{i}", "kind": "percent",
                                                            "percent_off": "10", "is_active": "1"})
        assert r.status_code == 303
    assert await count(db, select(func.count(Product.id)).where(Product.gym_id == gym.id)) == 3
    assert (await quotas.gym_usage(db, gym.id))[0] > 0
    assert await count(db, select(func.count(InventoryMovement.id))) == 3  # opening stock of each product
    assert Decimal("0") <= Decimal((await quotas.gym_usage(db, gym.id))[0]) < caps.limit("storage_mb_per_gym") * caps.MB


# ====================================================================================================== the docs
def test_the_readme_limits_table_states_every_cap():
    """README "Limits" (ONE table) has a row naming each cap of the CAPS table and stating its value in words."""
    from tests.test_limits_docs import _rows

    rows = _rows()
    for cap in caps._CAPS:
        named = [r for r in rows if f"`{cap.name}`" in r[2]]
        assert named, f"no README Limits row names `{cap.name}`"
        assert cap.phrase in named[0][1], f"the row of `{cap.name}` should say {cap.phrase!r}"
        if cap.setting:
            assert f"`{cap.setting.upper()}`" in named[0][2]


def test_env_example_documents_the_cap_settings_without_pinning_them():
    from dotenv import dotenv_values

    from app.config import BASE_DIR, Settings

    path = BASE_DIR / ".env.example"
    pinned, text = dotenv_values(path), path.read_text(encoding="utf-8")
    for key in ("MAX_ONLINE_GYMS", "MAX_OFFLINE_GYMS"):
        assert key not in pinned and f"# {key}={Settings.model_fields[key.lower()].default}" in text, key
    assert "CAPS_OVERRIDES" not in pinned and "# CAPS_OVERRIDES=" in text
