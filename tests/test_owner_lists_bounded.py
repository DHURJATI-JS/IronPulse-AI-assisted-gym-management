"""Owner + staff lists are bounded in SQL (app/core/pagination.py): one page / window per request with a constant
number of queries and windowed pagers - plus the bounded batch work of these areas (transactions CSV export in
keyset batches, payroll generation one gym per transaction, the rotating activation sweep)."""

from __future__ import annotations

import csv
import io
import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import event, func, insert, select
from sqlalchemy.orm import joinedload

from app import db as dbmod
from app.core.utils import local_today, utcnow
from app.db import QueryCounter
from app.models import (
    AuditLog,
    Coupon,
    CustomerProfile,
    Employee,
    HealthCondition,
    Job,
    JobEnquiry,
    JobPosting,
    Product,
    ProductReview,
    SalaryStatement,
    ShopOrder,
    Subscription,
    TrainerClient,
    Transaction,
    User,
    VerificationRun,
)
from app.models.enums import (
    ConditionKind,
    EmploymentStatus,
    EmploymentType,
    EmployeeRole,
    IdStatus,
    PurchaseChannel,
    Role,
    ShiftMode,
    StatementStatus,
    SubscriptionStatus,
    TxCategory,
    TxDirection,
    TxStatus,
    VerificationStatus,
    WorkMode,
)
from app.models.shop import OrderStatus, ProductCategory
from app.routers import owner_customers, owner_finance, owner_shop, owner_staff, staff_portal
from app.services import payroll as pr
from app.services import staffing as S
from tests.conftest import login_as
from tests.factories import (
    make_branch,
    make_customer,
    make_employee,
    make_gym,
    make_owner,
    make_plans,
    make_subscription,
    make_user,
)


# ============================================================================================ helpers
class Statements:
    """Every SQL statement (and its parameters) sent while the block runs."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

    def _on(self, conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        self.sent.append((statement, parameters))

    def __enter__(self) -> "Statements":
        event.listen(dbmod.get_engine().sync_engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(dbmod.get_engine().sync_engine, "before_cursor_execute", self._on)

    @property
    def sql(self) -> list[str]:
        return [s for s, _ in self.sent]

    def selects_from(self, table: str) -> list[str]:
        return [s for s in self.sql if s.lstrip().upper().startswith("SELECT") and f"FROM {table}" in s]


async def get_counted(client, url: str) -> tuple[Any, int, Statements]:
    with QueryCounter() as qc, Statements() as st:
        r = await client.get(url)
    assert r.status_code == 200, (url, r.status_code)
    return r, qc.count, st


def page_numbers(html: str) -> list[int]:
    """The numbered links of a ``ui.pager`` (a window - never every page)."""
    return [int(n) for n in re.findall(r'aria-label="Page (\d+)"', html)]


def bound(sql: str, params: Any, clause: str = "LIMIT") -> int | None:
    """The value bound to ``LIMIT $n`` / ``OFFSET $n`` (asyncpg numbers expanded IN lists after it)."""
    m = re.search(rf"\b{clause} \$(\d+)", sql)
    return list(params)[int(m.group(1)) - 1] if m else None


async def seed_customers(db, n: int, *, start: int = 0, prefix: str = "Member") -> list[int]:
    rows = [{"email": f"{prefix.lower()}{i}@bulk.example.com", "full_name": f"{prefix} {i:03d}", "role": Role.CUSTOMER,
             "id_status": IdStatus.VERIFIED, "date_of_birth": date(1990, 1, 1), "timezone": "Asia/Kolkata"}
            for i in range(start, start + n)]
    ids = list((await db.execute(insert(User).returning(User.id), rows)).scalars().all())
    await db.execute(insert(CustomerProfile), [{"user_id": uid, "activity_suspended": False} for uid in ids])
    await db.commit()
    return ids


async def seed_subscriptions(db, customer_ids: list[int], plan, *, status=SubscriptionStatus.ACTIVE,
                             starts: date | None = None, days: int = 30) -> list[int]:
    starts = starts or date.today()
    rows = [{"customer_id": cid, "gym_id": plan.gym_id, "plan_id": plan.id, "status": status,
             "channel": PurchaseChannel.ONLINE_CHECKOUT, "price": plan.price, "currency": plan.currency,
             "starts_on": starts, "ends_on": starts + timedelta(days=days)} for cid in customer_ids]
    ids = list((await db.execute(insert(Subscription).returning(Subscription.id), rows)).scalars().all())
    await db.commit()
    return ids


async def seed_employees(db, gym, n: int, *, start: int = 0, **kw: Any) -> None:
    values = {"role": EmployeeRole.RECEPTIONIST, "status": EmploymentStatus.INVITED, **kw}  # ACTIVE needs an account
    await db.execute(insert(Employee), [
        {"gym_id": gym.id, "invite_email": f"bulk{i}-{gym.id}@example.com", "full_name": f"Bulk Staff {i:03d}",
         "country_code": "IN", "currency": "INR", "monthly_salary": Decimal("20000"), "hire_date": date(2024, 1, 1),
         "photo_key": f"public/staff/{i:032x}.jpg", **values}
        for i in range(start, start + n)
    ])
    await db.commit()


async def owner_gym(db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    return owner, gym


# ============================================================================================ staff list
async def test_staff_list_renders_one_page_of_150_with_constant_queries(client, db):
    owner, gym = await owner_gym(db)
    await seed_employees(db, gym, 10)
    await login_as(client, owner)
    url = "/owner/staff?role=receptionist"
    await client.get(url)
    _, few, _ = await get_counted(client, url)
    await seed_employees(db, gym, 140, start=10)

    r, many, st = await get_counted(client, url)
    assert many == few  # 150 staff cost the same number of queries as 10
    cards = re.findall(r'<a href="/owner/staff/(\d+)" class="card card-hover', r.text)
    assert len(cards) == owner_staff.STAFF_PER_PAGE == 30
    assert "Bulk Staff 000" in r.text and "Bulk Staff 030" not in r.text
    page_sql = [s for s in st.selects_from("employees") if "LIMIT" in s and "count(" not in s.lower()]
    assert page_sql, "the staff page is bounded in SQL"
    assert page_numbers(r.text) == [1, 2, 3, 4, 5]
    assert 'href="/owner/staff?role=receptionist&amp;page=2"' in r.text  # filters survive paging
    assert "Page <span class=\"font-semibold text-ink dark:text-white\">1</span> of 5 · 150 staff members" in r.text

    last = await client.get(url + "&page=5")
    assert "Bulk Staff 149" in last.text and "Bulk Staff 119" not in last.text
    assert len(re.findall(r'<a href="/owner/staff/(\d+)" class="card card-hover', last.text)) == 30


# ============================================================================================ staff activity
async def test_staff_activity_is_a_keyset_window_with_show_older(client, db):
    owner, gym = await owner_gym(db)
    emp = await make_employee(db, gym, role=EmployeeRole.TRAINER)
    base = utcnow() - timedelta(days=40)
    await db.execute(insert(AuditLog), [
        {"action": "staff.create" if i == 0 else "staff.update", "entity_type": "employee", "entity_id": emp.id,
         "actor_id": owner.id, "gym_id": gym.id, "detail": {"summary": f"Change {i:03d}"},
         "created_at": base + timedelta(minutes=i)} for i in range(150)
    ])
    db.add_all([VerificationRun(employee_id=emp.id, outcome=VerificationStatus.VERIFIED, summary=f"Check run {i}",
                                created_at=base + timedelta(minutes=30 * i + 5)) for i in range(4)])  # 5, 35, 65, 95
    await db.commit()
    await login_as(client, owner)

    r, _, st = await get_counted(client, f"/owner/staff/{emp.id}?tab=activity")
    shown = re.findall(r"Change (\d{3})", r.text)
    assert shown == [f"{i:03d}" for i in range(149, 99, -1)]  # the newest 50 of 150
    assert "Check run" not in r.text  # all checks are older than this window
    audit_sql = st.selects_from("audit_logs")
    assert audit_sql and all("LIMIT" in s for s in audit_sql)
    older = re.search(r'href="(/owner/staff/\d+\?tab=activity&amp;before_id=\d+)"[^>]*data-timeline-more', r.text)
    assert older, "a 'Show older activity' link continues the feed"

    r2, count2, _ = await get_counted(client, older.group(1).replace("&amp;", "&"))
    assert re.findall(r"Change (\d{3})", r2.text) == [f"{i:03d}" for i in range(99, 49, -1)]
    assert "Check run 2" in r2.text and "Check run 3" in r2.text and "Check run 1" not in r2.text
    assert "Back to the latest activity" in r2.text
    older2 = re.search(r'href="(/owner/staff/\d+\?tab=activity&amp;before_id=\d+)"[^>]*data-timeline-more', r2.text)
    r3, count3, _ = await get_counted(client, older2.group(1).replace("&amp;", "&"))
    assert re.findall(r"Change (\d{3})", r3.text) == [f"{i:03d}" for i in range(49, -1, -1)]
    assert "Check run 0" in r3.text and "Check run 1" in r3.text and "Check run 2" not in r3.text
    assert "data-timeline-more" not in r3.text and "Staff record created" not in r3.text  # staff.create is logged
    assert count2 == count3  # the cost of a window does not depend on its depth

    # a cursor of another record is ignored (no probing): the newest window is shown
    other = await make_employee(db, gym, role=EmployeeRole.CLEANER)
    db.add(AuditLog(action="staff.update", entity_type="employee", entity_id=other.id, detail={"summary": "Other 999"}))
    await db.commit()
    foreign = await db.scalar(select(AuditLog.id).where(AuditLog.entity_id == other.id))
    r4 = await client.get(f"/owner/staff/{emp.id}?tab=activity&before_id={foreign}")
    assert re.findall(r"Change (\d{3})", r4.text)[0] == "149" and "Other 999" not in r4.text

    # the other tabs never read the audit log
    for tab in ("overview", "schedule", "documents", "profile", "clients"):
        _, _, st_tab = await get_counted(client, f"/owner/staff/{emp.id}?tab={tab}")
        assert not st_tab.selects_from("audit_logs"), tab


# ============================================================================================ trainer clients (owner)
async def _trainer_world(db, clients: int, extra_eligible: int):
    owner, gym = await owner_gym(db)
    branch = await make_branch(db, gym)
    plans = {p.code: p for p in await make_plans(db, gym)}
    trainer = await make_employee(db, gym, work_mode=WorkMode.BOTH, user=await make_user(db, role=Role.STAFF))
    ids = await seed_customers(db, clients + extra_eligible, prefix="Client")
    subs = await seed_subscriptions(db, ids, plans["offline_trainer"])
    await db.execute(insert(TrainerClient), [
        {"employee_id": trainer.id, "customer_id": cid, "subscription_id": sid, "mode": ShiftMode.OFFLINE, "active": True}
        for cid, sid in list(zip(ids, subs))[:clients]
    ])
    await db.commit()
    return owner, gym, branch, plans, trainer, ids


async def test_owner_trainer_clients_are_paginated_and_the_member_picker_is_bounded(client, db):
    owner, gym, branch, plans, trainer, _ = await _trainer_world(db, 10, 10)
    await login_as(client, owner)
    url = f"/owner/staff/{trainer.id}?tab=clients"
    await client.get(url)
    _, few, _ = await get_counted(client, url)
    # grow to 150 assigned clients + 80 members who could be assigned
    more = await seed_customers(db, 210, start=1000, prefix="Grown")
    subs = await seed_subscriptions(db, more, plans["offline_trainer"])
    await db.execute(insert(TrainerClient), [
        {"employee_id": trainer.id, "customer_id": cid, "subscription_id": sid, "mode": ShiftMode.OFFLINE, "active": True}
        for cid, sid in list(zip(more, subs))[:140]
    ])
    await db.commit()

    r, many, st = await get_counted(client, url)
    assert many == few
    assert r.text.count("> Unassign</button>") == owner_staff.CLIENTS_PER_PAGE == 30
    assert "150 active · members with a trainer-included plan" in r.text
    assert "Page <span class=\"font-semibold text-ink dark:text-white\">1</span> of 5 · 150 clients" in r.text
    assert f'href="/owner/staff/{trainer.id}?tab=clients&amp;page=2"' in r.text
    options = re.findall(r'<option value="(\d+)">[^<]+—', r.text)
    assert len(options) == S.ELIGIBLE_LIMIT == 50
    assert "Showing the first 50 of 80 members" in r.text
    for s in st.selects_from("trainer_clients") + st.selects_from("subscriptions"):
        assert "LIMIT" in s or "count(" in s.lower(), s  # never the whole list

    # the search narrows the picker (and keeps the assigned clients paginated)
    r = await client.get(url + "&member_q=Grown%201209")
    assert re.findall(r'<option value="(\d+)">[^<]+—', r.text) and "Grown 1209" in r.text
    assert len(re.findall(r'<option value="(\d+)">[^<]+—', r.text)) == 1
    r = await client.get(url + "&member_q=nobody-here")
    assert "No member matches" in r.text

    # the overview only counts
    r, _, st = await get_counted(client, f"/owner/staff/{trainer.id}")
    assert "150 assigned clients" in r.text and "80 members with a trainer plan can be assigned" in r.text
    assert all("count(" in s.lower() for s in st.selects_from("trainer_clients"))
    page2 = await client.get(url + "&page=2")
    assert page2.text.count("> Unassign</button>") == 30


async def test_eligible_subscriptions_are_filtered_in_sql(db):
    owner, gym = await owner_gym(db)
    branch = await make_branch(db, gym)
    plans = {p.code: p for p in await make_plans(db, gym)}
    online_trainer = await make_employee(db, gym, work_mode=WorkMode.ONLINE, user=await make_user(db, role=Role.STAFF))
    offline_member = await make_customer(db, full_name="Offline Olga")
    online_member = await make_customer(db, full_name="Online Omar")
    mine = await make_customer(db, full_name="Mine Mira")
    await make_subscription(db, offline_member, plans["offline_trainer"], branch=branch)
    await make_subscription(db, online_member, plans["online_trainer"])
    my_sub = await make_subscription(db, mine, plans["combo_trainer"])
    db.add(TrainerClient(employee_id=online_trainer.id, customer_id=mine.id, subscription_id=my_sub.id,
                         mode=ShiftMode.ONLINE, active=True))
    await db.commit()
    with QueryCounter() as qc:
        subs = await S.eligible_subscriptions(db, online_trainer)
    assert [s.customer.full_name for s in subs] == ["Online Omar"] and qc.count == 1  # mode + own clients in SQL
    assert await S.count_eligible_subscriptions(db, online_trainer) == 1
    assert await S.count_eligible_subscriptions(db, online_trainer, q="olga") == 0
    assert len(await S.eligible_subscriptions(db, online_trainer, limit=0)) <= 1


# ============================================================================================ staff portal clients
async def test_staff_portal_clients_are_paginated(client, db):
    owner, gym, branch, plans, trainer, ids = await _trainer_world(db, 12, 0)
    await db.execute(insert(HealthCondition), [{"customer_id": cid, "kind": ConditionKind.ALLERGY, "name": f"Pollen {j}"}
                                               for cid in ids for j in range(3)])
    await db.commit()
    staff_user = await db.get(User, trainer.user_id)
    await login_as(client, staff_user)
    await client.get("/staff/clients")
    _, few, _ = await get_counted(client, "/staff/clients")
    more = await seed_customers(db, 138, start=2000, prefix="Portal")
    subs = await seed_subscriptions(db, more, plans["offline_trainer"])
    await db.execute(insert(TrainerClient), [
        {"employee_id": trainer.id, "customer_id": cid, "subscription_id": sid, "mode": ShiftMode.OFFLINE, "active": True}
        for cid, sid in zip(more, subs)
    ])
    await db.commit()

    r, many, st = await get_counted(client, "/staff/clients")
    assert many == few
    cards = re.findall(r'<a href="/staff/clients/(\d+)" class="card', r.text)
    assert len(cards) == staff_portal.CLIENTS_PER_PAGE == 30
    assert page_numbers(r.text) == [1, 2, 3, 4, 5]
    assert all("LIMIT" in s or "count(" in s.lower() for s in st.selects_from("trainer_clients"))
    flags = [p for s, p in st.sent if "FROM health_conditions" in s]
    assert len(flags) == 1 and len(list(flags[0])) <= staff_portal.CLIENTS_PER_PAGE  # flags of this page's clients only
    r5 = await client.get("/staff/clients?page=5")
    assert len(re.findall(r'<a href="/staff/clients/(\d+)" class="card', r5.text)) == 30


async def test_staff_client_detail_bounds_health_notes_per_kind(client, db):
    owner, gym, branch, plans, trainer, ids = await _trainer_world(db, 1, 0)
    await db.execute(insert(HealthCondition), [{"customer_id": ids[0], "kind": ConditionKind.ALLERGY, "name": f"Allergen {j:02d}"}
                                               for j in range(staff_portal.CONDITIONS_PER_KIND + 5)])
    db.add(HealthCondition(customer_id=ids[0], kind=ConditionKind.INJURY, name="Left knee"))
    await db.commit()
    await login_as(client, await db.get(User, trainer.user_id))
    r, _, st = await get_counted(client, f"/staff/clients/{ids[0]}")
    assert len(re.findall(r"Allergen \d\d", r.text)) == staff_portal.CONDITIONS_PER_KIND
    assert "+ 5 more recorded" in r.text and "Left knee" in r.text
    health = [(s, p) for s, p in st.sent if "health_conditions" in s]
    assert len(health) == 1 and "row_number()" in health[0][0]  # bounded per kind in SQL
    assert staff_portal.CONDITIONS_PER_KIND in list(health[0][1])


# ============================================================================================ hiring
async def test_enquiries_and_postings_are_paginated(client, db):
    owner, gym = await owner_gym(db)
    job = JobPosting(gym_id=gym.id, title="Front desk", role=EmployeeRole.RECEPTIONIST,
                     employment_type=EmploymentType.FULL_TIME, location="Bengaluru", currency="INR",
                     requirements="Smile", is_open=True)
    db.add(job)
    await db.commit()
    await db.execute(insert(JobEnquiry), [{"job_id": job.id, "name": f"Applicant {i:03d}", "email": f"a{i}@example.com",
                                            "message": "Hire me " * 50} for i in range(10)])
    await db.commit()
    await login_as(client, owner)
    url = f"/owner/jobs/{job.id}/enquiries?status=new"
    await client.get(url)
    _, few, _ = await get_counted(client, url)
    await db.execute(insert(JobEnquiry), [{"job_id": job.id, "name": f"Applicant {i:03d}", "email": f"a{i}@example.com",
                                            "message": "Hire me " * 50} for i in range(10, 150)])
    await db.commit()
    r, many, st = await get_counted(client, url)
    assert many == few
    assert len(re.findall(r'action="/owner/enquiries/\d+/status"', r.text)) == owner_staff.ENQUIRIES_PER_PAGE == 25
    assert page_numbers(r.text) == [1, 2, 3, 6]  # 1 2 3 … 6
    assert f'href="/owner/jobs/{job.id}/enquiries?status=new&amp;page=2"' in r.text
    assert any("LIMIT" in s for s in st.selects_from("job_enquiries"))
    assert all("LIMIT" in s or "count(" in s.lower() for s in st.selects_from("job_enquiries"))

    await db.execute(insert(JobPosting), [
        {"gym_id": gym.id, "title": f"Posting {i:02d}", "role": EmployeeRole.CLEANER, "employment_type": EmploymentType.PART_TIME,
         "location": "Bengaluru", "currency": "INR", "requirements": "-", "is_open": False} for i in range(44)
    ])
    await db.commit()
    r, _, st = await get_counted(client, "/owner/jobs?state=closed")
    assert len(re.findall(r'<a href="/owner/jobs/\d+" class="truncate', r.text)) == owner_staff.JOBS_PER_PAGE == 20
    assert page_numbers(r.text) == [1, 2, 3] and 'href="/owner/jobs?state=closed&amp;page=2"' in r.text
    assert any("LIMIT" in s for s in st.selects_from("job_postings"))


# ============================================================================================ owner shop
async def _seed_orders(db, gym, customer, n: int, *, start: int = 0) -> None:
    await db.execute(insert(ShopOrder), [
        {"order_number": f"IPO-BULK-{gym.id}-{i:05d}", "gym_id": gym.id, "customer_id": customer.id,
         "customer_label": customer.full_name, "status": OrderStatus.PAID, "subtotal": Decimal("100"),
         "total": Decimal("118"), "currency": gym.currency, "paid_at": utcnow()} for i in range(start, start + n)
    ])
    await db.commit()


async def test_owner_orders_and_products_use_a_windowed_pager(client, db, monkeypatch):
    monkeypatch.setattr(owner_shop, "ORDERS_PER_PAGE", 2)
    monkeypatch.setattr(owner_shop, "PRODUCTS_PER_PAGE", 2)
    owner, gym = await owner_gym(db)
    cust = await make_customer(db)
    await _seed_orders(db, gym, cust, 10)
    await login_as(client, owner)
    await client.get("/owner/shop/orders?status=paid")
    _, few, _ = await get_counted(client, "/owner/shop/orders?status=paid")
    await _seed_orders(db, gym, cust, 140, start=10)
    r, many, st = await get_counted(client, "/owner/shop/orders?status=paid&page=40")
    assert many == few
    assert page_numbers(r.text) == [1, 38, 39, 40, 41, 42, 75]  # 75 pages, 7 links - never every page number
    assert r.text.count('href="/owner/shop/orders/') == 2
    assert 'href="/owner/shop/orders?status=paid&amp;page=41"' in r.text
    page_sql = [(s, p) for s, p in st.sent if "FROM shop_orders" in s and "LIMIT" in s]
    assert len(page_sql) == 1
    assert (bound(*page_sql[0]), bound(*page_sql[0], "OFFSET")) == (2, 78)  # LIMIT 2 OFFSET 78: page 40 only

    await db.execute(insert(Product), [
        {"gym_id": gym.id, "name": f"Bulk Product {i:03d}", "slug": f"bulk-product-{i}", "description": "Bulk",
         "price": Decimal("100"), "currency": gym.currency, "category": ProductCategory.SUPPLEMENTS} for i in range(150)
    ])
    await db.commit()
    r, _, st = await get_counted(client, "/owner/shop/products?status=active")
    assert page_numbers(r.text) == [1, 2, 3, 75]
    assert len(set(re.findall(r"Bulk Product (\d{3})", r.text))) == 2
    assert 'href="/owner/shop/products?status=active&amp;page=2"' in r.text
    assert any("LIMIT" in s for s in st.selects_from("products"))


async def test_owner_order_page_reads_at_most_50_events(client, db):
    owner, gym = await owner_gym(db)
    cust = await make_customer(db)
    await _seed_orders(db, gym, cust, 1)
    order = await db.scalar(select(ShopOrder))
    await db.execute(insert(AuditLog), [
        {"action": "shop.order.status", "entity_type": "shop_order", "entity_id": order.id,
         "detail": {"from": "paid", "to": "processing"}, "gym_id": gym.id} for _ in range(120)
    ])
    await db.commit()
    await login_as(client, owner)
    r, _, st = await get_counted(client, f"/owner/shop/orders/{order.id}")
    events_sql = [(s, p) for s, p in st.sent if "FROM audit_logs" in s]
    assert len(events_sql) == 1 and "LIMIT" in events_sql[0][0]
    assert owner_shop.ORDER_EVENTS_LIMIT in list(events_sql[0][1])
    assert "Processing" in r.text


async def test_owner_coupons_and_reviews_are_paginated(client, db):
    owner, gym = await owner_gym(db)
    await db.execute(insert(Coupon), [{"gym_id": gym.id, "code": f"BULK{i:03d}", "percent_off": 10, "is_active": True}
                                      for i in range(45)])
    product = Product(gym_id=gym.id, name="Reviewed Whey", slug="reviewed-whey", description="d", price=Decimal("10"),
                      currency=gym.currency)
    db.add(product)
    await db.commit()
    reviewers = await seed_customers(db, 45, prefix="Reviewer")
    await db.execute(insert(ProductReview), [{"product_id": product.id, "user_id": uid, "rating": 1 + i % 5,
                                              "title": f"Review {i:03d}"} for i, uid in enumerate(reviewers)])
    await db.commit()
    await login_as(client, owner)
    r, _, st = await get_counted(client, "/owner/shop/coupons")
    assert len(re.findall(r'action="/owner/shop/coupons/\d+/toggle"', r.text)) == owner_shop.COUPONS_PER_PAGE == 30
    assert page_numbers(r.text) == [1, 2] and any("LIMIT" in s for s in st.selects_from("coupons"))
    r = await client.get("/owner/shop/coupons?page=2")
    assert len(re.findall(r'action="/owner/shop/coupons/\d+/toggle"', r.text)) == 15

    r, _, st = await get_counted(client, "/owner/shop/reviews")
    assert len(re.findall(r'action="/owner/shop/reviews/\d+/toggle"', r.text)) == owner_shop.REVIEWS_PER_PAGE == 30
    assert any("LIMIT" in s for s in st.selects_from("product_reviews"))
    r = await client.get("/owner/shop/reviews?show=low&page=1")  # ratings 1-2: 18 of 45
    assert len(re.findall(r'action="/owner/shop/reviews/\d+/toggle"', r.text)) == 18 and not page_numbers(r.text)


async def test_list_date_filters_never_overflow(client, db):
    owner, gym = await owner_gym(db)
    await login_as(client, owner)
    for url in ("/owner/shop/orders?to=9999-12-31", "/owner/shop/orders?from=0001-01-01",
                "/owner/finance/transactions?to=9999-12-31", "/owner/finance/transactions?from=0001-01-01",
                "/owner/finance/transactions.csv?to=9999-12-31&from=0001-01-01"):
        assert (await client.get(url)).status_code == 200, url


# ============================================================================================ finance
def _tx(gym, i: int, *, completed=None) -> dict[str, Any]:
    return {"gym_id": gym.id, "direction": TxDirection.INCOMING, "category": TxCategory.SUBSCRIPTION,
            "status": TxStatus.SUCCESS, "amount": Decimal("100") + i, "currency": "INR", "description": f"Bulk tx {i:03d}",
            "provider": "test", "completed_at": completed}


async def test_finance_overview_lists_are_hard_capped(client, db):
    owner, gym = await owner_gym(db)
    emp = await make_employee(db, gym, hire_date=date(2015, 1, 1))
    now = utcnow()
    await db.execute(insert(Transaction), [_tx(gym, i, completed=now - timedelta(minutes=150 - i)) for i in range(150)])
    for k in range(20):
        start = date(2020, 1, 1) + timedelta(days=31 * k)
        draft = pr.compute_statement(monthly_salary=emp.monthly_salary, currency=emp.currency, country_code=emp.country_code,
                                     hire_date=emp.hire_date, period_start=start, period_end=start + timedelta(days=27))
        db.add(SalaryStatement(**draft.model_kwargs(), employee_id=emp.id, gym_id=gym.id, status=StatementStatus.DUE,
                               due_date=start + timedelta(days=35)))
    await db.commit()
    await login_as(client, owner)
    r, _, st = await get_counted(client, "/owner/finance")
    assert len(set(re.findall(r"Bulk tx (\d{3})", r.text))) == owner_finance.RECENT_TX_LIMIT == 8
    assert "Bulk tx 149" in r.text and "Bulk tx 141" not in r.text
    recent = [p for s, p in st.sent if "FROM transactions" in s and "LIMIT" in s]
    assert recent and owner_finance.RECENT_TX_LIMIT in list(recent[0])
    pending = [p for s, p in st.sent if "FROM salary_statements" in s and "LIMIT" in s]
    assert pending and owner_finance.PENDING_STATEMENT_LIMIT in list(pending[0])


def _csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text.lstrip("﻿"))))


async def test_csv_export_streams_keyset_batches_in_short_transactions(client, db, monkeypatch):
    monkeypatch.setattr(owner_finance, "EXPORT_BATCH", 2)
    owner, gym = await owner_gym(db)
    tie = utcnow() - timedelta(days=1)
    await db.execute(insert(Transaction), [_tx(gym, i, completed=tie if i in (2, 3, 4) else tie - timedelta(hours=i))
                                           for i in range(7)])
    await db.commit()
    expected = [row.id for row in (await db.execute(
        select(Transaction.id).order_by(func.coalesce(Transaction.completed_at, Transaction.created_at).desc(),
                                        Transaction.id.desc()))).all()]
    await login_as(client, owner)
    with Statements() as st:
        r = await client.get("/owner/finance/transactions.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = _csv(r.text)
    assert rows[0][0] == "id" and [int(x[0]) for x in rows[1:]] == expected  # all 7, in order, no duplicate at the ties
    batches = [(s, p) for s, p in st.sent if "FROM transactions" in s and "LIMIT" in s and "count(" not in s.lower()]
    assert len(batches) == 4  # 2 + 2 + 2 + 1 rows: one bounded statement per batch
    assert [bound(s, p) for s, p in batches] == [2, 2, 2, 2]
    assert all("(coalesce(transactions.completed_at, transactions.created_at), transactions.id) <" in s
               for s, _p in batches[1:])
    timeouts = [s for s in st.sql if "set_config('statement_timeout'" in s]
    assert len(timeouts) == 4  # SET LOCAL per batch: every batch is its own short transaction


async def test_csv_export_is_capped_and_asks_for_a_narrower_range(client, db, monkeypatch):
    monkeypatch.setattr(owner_finance, "EXPORT_MAX_ROWS", 5)
    owner, gym = await owner_gym(db)
    await db.execute(insert(Transaction), [_tx(gym, i) for i in range(6)])
    await db.commit()
    await login_as(client, owner)
    with Statements() as st:
        r = await client.get("/owner/finance/transactions.csv?direction=incoming")
    assert r.status_code == 303 and r.headers["location"] == "/owner/finance/transactions?direction=incoming"
    count_sql = [s for s in st.sql if "FROM transactions" in s]
    assert len(count_sql) == 1 and "LIMIT" in count_sql[0]  # a capped COUNT, then nothing is streamed
    page = await client.get(r.headers["location"])
    assert "more than 5 transactions" in page.text
    ok = await client.get("/owner/finance/transactions.csv?direction=outgoing")
    assert ok.status_code == 200 and len(_csv(ok.text)) == 1


async def test_payroll_staff_filter_is_capped_and_validated_by_one_query(client, db, monkeypatch):
    monkeypatch.setattr(owner_finance, "EMPLOYEE_OPTIONS_LIMIT", 3)
    owner, gym = await owner_gym(db)
    await seed_employees(db, gym, 5)
    await login_as(client, owner)
    r = await client.get("/owner/payroll")
    options = re.findall(r'<option value="(\d+)"[^>]*>Bulk Staff', r.text)
    assert len(options) == 3
    last = await db.scalar(select(Employee.id).where(Employee.full_name == "Bulk Staff 004"))
    r = await client.get(f"/owner/payroll?employee={last}")
    assert r.status_code == 200 and re.search(rf'<option value="{last}" selected>Bulk Staff 004', r.text)
    assert (await client.get("/owner/payroll?employee=99999999")).status_code == 404


# ============================================================================================ members
async def test_members_list_loads_one_page_with_bounded_subscriptions_and_health(client, db):
    owner, gym = await owner_gym(db)
    plans = await make_plans(db, gym)
    ids = await seed_customers(db, 10)
    await login_as(client, owner)

    async def grow(member_ids: list[int]) -> None:
        for k in range(5):  # full history: expired, cancelled, pending + one active
            await seed_subscriptions(db, member_ids, plans[k], status=SubscriptionStatus.EXPIRED,
                                     starts=date.today() - timedelta(days=400 - 40 * k))
        await seed_subscriptions(db, member_ids, plans[6])
        await db.execute(insert(HealthCondition), [
            {"customer_id": cid, "kind": ConditionKind.CONDITION if j % 2 else ConditionKind.ALLERGY,
             "name": f"Entry {j:02d}", "tag": "asthma" if j == 1 else None} for cid in member_ids for j in range(8)
        ])
        await db.commit()

    await grow(ids)
    await client.get("/owner/customers")
    _, few, _ = await get_counted(client, "/owner/customers")
    more = await seed_customers(db, 140, start=10)
    await grow(more)

    r, many, st = await get_counted(client, "/owner/customers")
    assert many == few
    shown = [int(x) for x in re.findall(r'<a href="/owner/customers/(\d+)" class="group', r.text)]
    assert len(shown) == owner_customers.PAGE_SIZE == 24
    assert "Page 1 of 7 · 150 members" in r.text
    # one subscription per member (DISTINCT ON), never the history
    subs_sql = [s for s in st.selects_from("subscriptions") if "DISTINCT ON" in s]
    assert len(subs_sql) == 1
    # at most 3 health labels per member are read (+ a count), never every entry
    health_sql = [(s, p) for s, p in st.sent if "health_conditions" in s and "tag IS NOT NULL" not in s]
    assert len(health_sql) == 1 and "row_number()" in health_sql[0][0]
    assert owner_customers.HEALTH_LABELS_SHOWN in list(health_sql[0][1])
    # same chips as the old "load every entry, dedupe in Python" code
    from app.services.conditions import TAG_LABELS

    entries = (await db.execute(select(HealthCondition).where(HealthCondition.customer_id.in_(shown))
                                .order_by(HealthCondition.kind, HealthCondition.name))).scalars().all()
    for uid in shown:
        labels = list(dict.fromkeys(TAG_LABELS.get(c.tag, c.name) if c.tag else c.name
                                    for c in entries if c.customer_id == uid))
        row = re.search(rf'<a href="/owner/customers/{uid}" class="group.*?</a>', r.text, re.S).group(0)
        chips = re.findall(r'<span class="chip !px-2 !py-0.5 !text-\[11px\]">([^<]+)</span>', row)
        assert chips == labels[:3] + [f"+{len(labels) - 3}"], uid
        assert "Active" in row and plans[6].name in row  # the current (active) plan, picked in SQL
    # the member scope is ONE semi-join sub-select (UNION) on every member query
    member_sql = [s for s in st.sql if "customer_profiles.registered_by_id" in s]
    assert len(member_sql) >= 4 and all("UNION" in s for s in member_sql)


async def test_members_list_membership_state_matches_the_full_history(client, db):
    owner, gym = await owner_gym(db)
    plans = await make_plans(db, gym)
    today = local_today(owner.timezone)  # the page's "today" (the owner's time zone)
    cases: dict[str, list[tuple[Any, SubscriptionStatus, date, int]]] = {
        "two_active": [(plans[0], SubscriptionStatus.ACTIVE, today - timedelta(days=5), 30),
                       (plans[1], SubscriptionStatus.ACTIVE, today - timedelta(days=2), 90)],
        "pending_after_expired": [(plans[2], SubscriptionStatus.EXPIRED, today - timedelta(days=90), 30),
                                  (plans[3], SubscriptionStatus.PENDING_PAYMENT, today, 30)],
        "only_old": [(plans[4], SubscriptionStatus.CANCELLED, today - timedelta(days=200), 30),
                     (plans[5], SubscriptionStatus.EXPIRED, today - timedelta(days=100), 30)],
        "active_and_pending": [(plans[6], SubscriptionStatus.PENDING_OTP, today, 30),
                               (plans[7], SubscriptionStatus.ACTIVE, today - timedelta(days=1), 30)],
    }
    users: dict[str, int] = {}
    for name, subs in cases.items():
        (uid,) = await seed_customers(db, 1, start=len(users), prefix=name.replace("_", ""))
        users[name] = uid
        for plan, status, starts, days in subs:
            await seed_subscriptions(db, [uid], plan, status=status, starts=starts, days=days)
    db.add(CustomerProfile(user_id=(await make_customer(db, with_profile=False)).id, registered_by_id=owner.id))
    await db.commit()
    await login_as(client, owner)
    r = await client.get("/owner/customers")
    for name, uid in users.items():
        full = list((await db.execute(
            select(Subscription).options(joinedload(Subscription.plan), joinedload(Subscription.gym))
            .where(Subscription.customer_id == uid).order_by(Subscription.created_at.desc(), Subscription.id.desc())
        )).unique().scalars().all())
        expected = owner_customers.membership_summary(full, today)  # the old way: the whole history in Python
        row = re.search(rf'<a href="/owner/customers/{uid}" class="group.*?</a>', r.text, re.S).group(0)
        assert f"{expected['plan']} · {gym.name}" in row, name
        badge = {"active": "Active", "pending": "Pending", "expired": "Expired"}[expected["state"]]
        assert badge in re.sub(r"<[^>]+>", " ", row).split(), name
        if expected["days_left"] is not None:
            assert f"Ends in {expected['days_left']} day" in row, name
    assert "Page 1 of" not in r.text and "Registered" in r.text  # the registered-only member (UNION branch)


async def test_member_profile_paginates_memberships_and_caps_health_per_kind(client, db):
    owner, gym = await owner_gym(db)
    plans = await make_plans(db, gym)
    (uid,) = await seed_customers(db, 1, prefix="Profile")
    for k in range(30):  # the newest ended lately: a former member stays visible for intake.FORMER_MEMBER_DAYS
        await seed_subscriptions(db, [uid], plans[k % 12], status=SubscriptionStatus.EXPIRED,
                                 starts=date.today() - timedelta(days=900 - 30 * k))
    cap = owner_customers.CONDITIONS_PER_KIND
    await db.execute(insert(HealthCondition), [{"customer_id": uid, "kind": ConditionKind.ALLERGY, "name": f"Allergen {j:03d}"}
                                               for j in range(cap + 10)])
    await db.commit()
    await login_as(client, owner)
    r, _, st = await get_counted(client, f"/owner/customers/{uid}")
    memberships = r.text.split('id="memberships"', 1)[1].split("</section>", 1)[0]
    assert memberships.count('<li class="flex flex-wrap items-center gap-x-4') == owner_customers.SUBS_PAGE_SIZE == 12
    assert f'href="/owner/customers/{uid}?page=2#memberships"' in memberships and page_numbers(memberships) == [1, 2, 3]
    sub_lists = [(s, p) for s, p in st.sent if "subscriptions.starts_on" in s]  # statements loading subscription rows
    assert len(sub_lists) == 2  # the page of memberships + the current one (DISTINCT ON): never the whole history
    assert sorted(bound(s, p) or 0 for s, p in sub_lists) == [0, owner_customers.SUBS_PAGE_SIZE]
    assert any("DISTINCT ON" in s for s, _p in sub_lists)
    assert len(re.findall(r"Allergen \d{3}", r.text.split('id="health"', 1)[1].split("<details", 1)[0])) == cap
    assert "+10 more" in r.text and "(10 more)" in r.text  # the edit box says what it does not list
    assert f"{cap + 10} items" in r.text
    r3 = await client.get(f"/owner/customers/{uid}?page=3")
    memberships3 = r3.text.split('id="memberships"', 1)[1].split("</section>", 1)[0]
    assert memberships3.count('<li class="flex flex-wrap items-center gap-x-4') == 6


async def test_owner_audit_counts_are_capped_and_areas_are_static(client, db, monkeypatch):
    monkeypatch.setattr(owner_customers, "AUDIT_COUNT_CAP", 100)
    owner, gym = await owner_gym(db)
    actor = await make_customer(db, full_name="Zelda Actor")
    stranger = await make_customer(db, full_name="Zelda Elsewhere")
    other_owner, other_gym = await owner_gym(db)
    await db.execute(insert(AuditLog), [{"action": "customer.register", "entity_type": "user", "entity_id": i,
                                         "actor_id": actor.id if i % 2 else owner.id, "gym_id": gym.id,
                                         "detail": {"n": i}} for i in range(150)])
    db.add(AuditLog(action="customer.register", entity_type="user", entity_id=1, actor_id=stranger.id,
                    gym_id=other_gym.id, detail={"secret": "theirs"}))
    await db.commit()
    await login_as(client, owner)
    r, _, st = await get_counted(client, "/owner/audit")
    assert "100+ entries" in r.text and "Page 1 of 3" in r.text
    assert not any("DISTINCT" in s.upper() for s in st.selects_from("audit_logs"))
    assert all("LIMIT" in s for s in st.selects_from("audit_logs") if "count(" not in s.lower())
    capped = [s for s in st.sql if "count(" in s.lower() and "FROM audit_logs" in s]
    assert capped and "LIMIT" in capped[0]
    assert '<option value="shop"' in r.text and '<option value="customer"' in r.text
    r = await client.get("/owner/audit?q=Zelda")  # the actor's name, matched on the owner's own rows only
    assert "theirs" not in r.text and "75 entries" in r.text
    assert not any("FROM users WHERE" in s and "ilike" in s.lower() for s in (await _audit_sql(client, "/owner/audit?q=Zelda")))
    assert (await client.get("/owner/audit?page=3")).status_code == 200


async def _audit_sql(client, url: str) -> list[str]:
    with Statements() as st:
        await client.get(url)
    return st.sql


# ============================================================================================ activation sweep
async def test_activation_sweep_rotates_through_every_candidate(db, monkeypatch):
    monkeypatch.setattr(S, "SWEEP_BATCH", 2)
    monkeypatch.setattr(S, "_sweep_after_id", 0)
    owner, gym = await owner_gym(db)
    ready = []
    for _ in range(5):
        user = await make_user(db, role=Role.STAFF)
        ready.append(await make_employee(db, gym, user=user, role=EmployeeRole.RECEPTIONIST,
                                         status=EmploymentStatus.INVITED))
    keys = {f"staff_activation_check:{e.id}" for e in ready}
    with Statements() as st:
        for _ in range(3):
            await S.staff_activation_sweep(db)
            await db.commit()
    queued = set((await db.execute(select(Job.dedupe_key).where(Job.kind == "staff_activation_check"))).scalars())
    assert queued == keys  # 2 + 2 + (1 + wrap): every employment got its turn
    windows = [s for s in st.sql if "FROM employees" in s and "employees.status IN" in s]
    assert len(windows) == 4 and all("LIMIT" in s for s in windows)  # the 3rd run wrapped around: 2 windows
    docs = [s for s in st.sql if "FROM employee_documents" in s]
    assert docs and all("ocr_text" not in s for s in docs)  # the sweep never loads OCR text


async def test_activation_sweep_without_rotation_would_starve(db, monkeypatch):
    """The old sweep (oldest 200 by updated_at, no cursor) re-checked the same rows every run. With a batch of 2,
    rows that are never ready no longer hold the window: a ready row beyond them is reached on the next run."""
    monkeypatch.setattr(S, "SWEEP_BATCH", 2)
    monkeypatch.setattr(S, "_sweep_after_id", 0)
    owner, gym = await owner_gym(db)
    for _ in range(2):  # linked but never ready (ID pending): evaluated, never queued
        user = await make_user(db, role=Role.STAFF, id_status=IdStatus.PENDING)
        await make_employee(db, gym, user=user, role=EmployeeRole.CLEANER, status=EmploymentStatus.PENDING_VERIFICATION)
    ready = await make_employee(db, gym, user=await make_user(db, role=Role.STAFF), role=EmployeeRole.CLEANER,
                                status=EmploymentStatus.PENDING_VERIFICATION)
    assert await S.staff_activation_sweep(db) == 0  # the first window holds the two waiting rows
    assert await S.staff_activation_sweep(db) == 1  # ... the next one reaches the ready row
    await db.commit()
    assert await db.scalar(select(Job.dedupe_key).where(Job.kind == "staff_activation_check")) == \
        f"staff_activation_check:{ready.id}"
