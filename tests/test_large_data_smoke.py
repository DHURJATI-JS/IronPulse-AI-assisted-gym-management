"""Large-data smoke (pagination audit, integration check): on a platform of 300 gyms, 2,000 branches, 5,000 members,
20,000 notifications, 3,000 shop orders and 10,000 audit logs (plus 3,000 products, 220 staff, 1,600 conversations,
900 job postings, 8,000 security events, bans, mail and jobs), the busiest pages

* answer with 200 and render at most one page of rows,
* run EXACTLY the statements they run on a nearly empty platform (a few rows of each kind), and
* never fetch a whole table: no single statement returns more than MAX_ROWS rows, no request loads more than
  MAX_ENTITIES ORM objects.

The data is bulk inserted (multi-row INSERTs, no factories in loops). Each page is requested with the in-process
TTL caches cleared, i.e. the cold, worst case. ``SMOKE_MAX_SECONDS`` is the per-page time limit (default 10 s: a
guard against a page that reads a whole table, not a benchmark - the pagination target is 1.5 s), and
``SMOKE_REPORT=<path>`` writes every measurement as JSON."""

from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from datetime import time as dtime
from decimal import Decimal
from typing import Any

from sqlalchemy import event, insert, select, text

from app import db as dbmod
from app.ai.embeddings import embed
from app.core import ttl_cache
from app.core.utils import utcnow
from app.db import QueryCounter
from app.models import (
    AuditLog,
    Branch,
    BranchHours,
    BranchPhoto,
    ChatMessage,
    ChatThread,
    CustomerProfile,
    EmailOutbox,
    Employee,
    Gym,
    IpBan,
    Job,
    JobPosting,
    Notification,
    Plan,
    Product,
    SecurityEvent,
    ShopOrder,
    ShopOrderItem,
    Subscription,
    User,
)
from app.models.enums import (
    EmailStatus,
    EmployeeRole,
    EmploymentStatus,
    EmploymentType,
    IdStatus,
    JobStatus,
    NotificationCategory,
    PlanMode,
    PurchaseChannel,
    Role,
    Severity,
    SubscriptionStatus,
    ThreatCategory,
    ThreadStatus,
    VerificationStatus,
    WorkMode,
)
from app.models.shop import OrderStatus, ProductCategory
from tests.conftest import login_as, make_client
from tests.factories import make_customer, make_gym, make_owner, make_plans, make_user

MAX_SECONDS = float(os.environ.get("SMOKE_MAX_SECONDS") or 10.0)
MAX_ROWS = 200  # rows returned by one statement (the largest page is 50; the widest bounded facet is 60 cities)
MAX_ENTITIES = 400  # ORM objects loaded by one request

# (city, lat, lon): most branches around Bengaluru (the /jobs?lat=12.97&lon=77.59 visitor), the rest far away
CITIES = [("Bengaluru", 12.97, 77.59)] * 7 + [("Mumbai", 19.07, 72.87), ("Delhi", 28.61, 77.21),
                                              ("Chennai", 13.08, 80.27), ("Hyderabad", 17.38, 78.48), ("Pune", 18.52, 73.85)]
EMPLOYEE_ROLES = [EmployeeRole.TRAINER, EmployeeRole.RECEPTIONIST, EmployeeRole.MANAGER, EmployeeRole.NUTRITIONIST]
STAFF_STATUSES = [EmploymentStatus.ACTIVE] * 6 + [EmploymentStatus.PENDING_VERIFICATION, EmploymentStatus.SUSPENDED]
ORDER_STATUSES = [OrderStatus.PAID, OrderStatus.PROCESSING, OrderStatus.COMPLETED, OrderStatus.COMPLETED,
                  OrderStatus.SHIPPED, OrderStatus.CANCELLED, OrderStatus.PENDING_PAYMENT]
PRODUCT_NAMES = [("Whey Protein Isolate", ProductCategory.SUPPLEMENTS, True), ("Creatine Monohydrate", ProductCategory.SUPPLEMENTS, True),
                 ("Lifting Gloves", ProductCategory.ACCESSORIES, False), ("Yoga Mat", ProductCategory.EQUIPMENT, False),
                 ("Protein Bar Box", ProductCategory.NUTRITION, False), ("Training Tee", ProductCategory.APPAREL, False),
                 ("Shaker Bottle", ProductCategory.ACCESSORIES, False)]


@dataclass
class Size:
    """How many rows one ``grow`` call adds."""

    owners: int  # other owners (each gets a share of ``gyms``)
    gyms: int  # new gyms (``big_gyms`` of them for the big owner)
    big_gyms: int
    branches: int  # spread over the new gyms
    flagship_branches: int  # on the big owner's flagship gym (the /gyms/<slug> page)
    members: int
    staff: int  # employees of the big owner's gyms (the /owner/staff list)
    trainers: int  # public trainers of the new gyms (the home page teaser)
    notes_member: int  # notifications of the heavy member (/account/notifications)
    notes_owner: int
    notes_other: int
    flagship_products: int
    products_per_gym: int
    orders_flagship: int  # the /owner/shop/orders list
    orders_member: int  # ... of which the heavy member placed these (/shop/orders)
    orders_other: int
    audit_owner: int
    audit_other: int
    events: int  # security events of the last 7 days (/superadmin/security)
    bans: int
    threads_flagship: int  # conversations of the flagship gym (the owner's /chat)
    threads_member: int  # conversations of the heavy member with other gyms (the member's /chat)
    long_thread_messages: int
    postings: int
    mails: int
    jobs: int


# a few rows of every kind, then the rest: the totals are 300 gyms, 2,000 branches, 5,000 members, 20,000
# notifications, 3,000 orders, 10,000 audit logs
SMALL = Size(owners=2, gyms=3, big_gyms=1, branches=6, flagship_branches=2, members=9, staff=3, trainers=3,
             notes_member=5, notes_owner=3, notes_other=4, flagship_products=2, products_per_gym=4, orders_flagship=4,
             orders_member=1, orders_other=3, audit_owner=5, audit_other=5, events=5, bans=2, threads_flagship=3,
             threads_member=2, long_thread_messages=4, postings=3, mails=3, jobs=3)
LARGE = Size(owners=57, gyms=296, big_gyms=10, branches=1844, flagship_branches=148, members=4990, staff=117,
             trainers=97, notes_member=11995, notes_owner=2997, notes_other=4996, flagship_products=26,
             products_per_gym=10, orders_flagship=2396, orders_member=299, orders_other=597, audit_owner=5995,
             audit_other=3995, events=7995, bans=298, threads_flagship=1497, threads_member=98,
             long_thread_messages=1996, postings=897, mails=1997, jobs=597)


@dataclass
class World:
    admin: User
    owner: User
    member: User
    flagship: Gym
    password_hash: str
    big_gyms: list[int] = field(default_factory=list)  # the big owner's gym ids (flagship first)
    flagship_product_ids: list[int] = field(default_factory=list)
    long_thread_id: int | None = None
    seq: int = 0

    def next(self) -> int:
        self.seq += 1
        return self.seq


async def _rows(db, model, rows: list[dict[str, Any]], *, ids: bool = False) -> list[int]:
    """One bulk INSERT (multi-row VALUES / executemany) - never a factory per row."""
    if not rows:
        return []
    if ids:
        return list((await db.execute(insert(model).returning(model.id, sort_by_parameter_order=True), rows)).scalars())
    await db.execute(insert(model), rows)
    return []


async def build_world(db) -> World:
    admin = await make_user(db, role=Role.SUPERADMIN, full_name="Platform Admin")
    owner = await make_owner(db, full_name="Big Owner")
    member = await make_customer(db, full_name="Heavy Member")
    flagship = await make_gym(db, owner, name="Flagship Fitness")
    await make_plans(db, flagship)
    pw = (await db.get(User, member.id)).password_hash
    world = World(admin=admin, owner=owner, member=member, flagship=flagship, password_hash=pw or "")
    world.big_gyms.append(flagship.id)
    return world


async def grow(db, w: World, s: Size, tag: str) -> None:  # noqa: PLR0915 - one linear seed script
    rnd = random.Random(f"smoke-{tag}")
    now = utcnow()
    today = date.today()

    def ago(max_days: float) -> Any:
        return now - timedelta(seconds=rnd.uniform(60, max_days * 86400))

    # ---------------------------------------------------------------- people
    owner_ids = await _rows(db, User, [
        {"email": f"smk-{tag}-owner-{i}@example.com", "full_name": f"Owner {tag} {i}", "role": Role.OWNER,
         "password_hash": w.password_hash, "id_status": IdStatus.VERIFIED, "email_verified_at": now,
         "photo_key": f"public/avatars/{w.next():032x}.jpg", "timezone": "Asia/Kolkata"} for i in range(s.owners)
    ], ids=True)
    member_ids = await _rows(db, User, [
        {"email": f"smk-{tag}-member-{i}@example.com", "full_name": f"Member {tag} {i:05d}", "role": Role.CUSTOMER,
         "password_hash": w.password_hash, "id_status": IdStatus.VERIFIED, "email_verified_at": now,
         "date_of_birth": date(1990, 1, 1) + timedelta(days=i % 5000), "timezone": "Asia/Kolkata"}
        for i in range(s.members)
    ], ids=True)
    await _rows(db, CustomerProfile, [{"user_id": uid, "height_cm": 170.0, "weight_kg": 70.0} for uid in member_ids])
    staff_user_ids = await _rows(db, User, [
        {"email": f"smk-{tag}-staff-{i}@example.com", "full_name": f"Staff {tag} {i:04d}", "role": Role.STAFF,
         "password_hash": w.password_hash, "id_status": IdStatus.VERIFIED, "email_verified_at": now,
         "photo_key": f"public/avatars/{w.next():032x}.jpg", "timezone": "Asia/Kolkata"}
        for i in range(s.staff + s.trainers)
    ], ids=True)

    # ---------------------------------------------------------------- gyms, branches, plans
    gym_owner = [w.owner.id] * s.big_gyms + [owner_ids[i % len(owner_ids)] for i in range(s.gyms - s.big_gyms)]
    gym_ids = await _rows(db, Gym, [
        {"owner_id": oid, "name": f"{rnd.choice(['Iron', 'Pulse', 'Core', 'Titan', 'Zen'])} Gym {tag} {i:03d}",
         "slug": f"smk-{tag}-gym-{i}", "tagline": "Strength for everyone", "description": "Seeded gym",
         "country_code": "IN", "currency": "INR", "online_enabled": True, "offline_enabled": True,
         "online_open": i % 3 == 0, "is_active": True} for i, oid in enumerate(gym_owner)
    ], ids=True)
    w.big_gyms += gym_ids[: s.big_gyms]
    await _rows(db, Plan, [
        {"gym_id": gid, "code": code, "name": code.title(), "mode": mode, "price": Decimal(price), "currency": "INR",
         "duration_days": 30, "sort_order": k}
        for gid in gym_ids for k, (code, mode, price) in enumerate(
            [("offline", PlanMode.OFFLINE, "1499"), ("online", PlanMode.ONLINE, "799"), ("combo", PlanMode.COMBO, "1999")])
    ])
    branch_gym = [w.flagship.id] * s.flagship_branches + [gym_ids[i % len(gym_ids)] for i in range(s.branches)]
    branch_rows = []
    for i, gid in enumerate(branch_gym):
        city, lat, lon = rnd.choice(CITIES)
        branch_rows.append({
            "gym_id": gid, "name": f"Branch {tag} {i:04d}", "address_line1": f"{i} Main Road", "city": city,
            "country_code": "IN", "timezone": "Asia/Kolkata", "latitude": lat + rnd.uniform(-0.25, 0.25),
            "longitude": lon + rnd.uniform(-0.25, 0.25), "verification_status": VerificationStatus.VERIFIED,
            "verified_at": now, "is_open": True})
    branch_ids = await _rows(db, Branch, branch_rows, ids=True)
    await _rows(db, BranchHours, [{"branch_id": bid, "weekday": wd, "opens_at": dtime(6), "closes_at": dtime(20)}
                                  for bid in branch_ids for wd in range(7)])
    await _rows(db, BranchPhoto, [{"branch_id": bid, "image_key": f"public/branch/{w.next():032x}.jpg", "sort_order": k}
                                  for bid in branch_ids for k in range(3)])
    first_branch = {}
    for bid, gid in zip(branch_ids, branch_gym, strict=True):
        first_branch.setdefault(gid, bid)

    # ---------------------------------------------------------------- staff: the big owner's team + public trainers
    emp_rows = []
    for i in range(s.staff + s.trainers):
        big = i < s.staff
        status = STAFF_STATUSES[i % len(STAFF_STATUSES)] if big else EmploymentStatus.ACTIVE
        role = EMPLOYEE_ROLES[i % len(EMPLOYEE_ROLES)] if big else EmployeeRole.TRAINER
        emp_rows.append({
            "gym_id": w.big_gyms[i % len(w.big_gyms)] if big else gym_ids[(i - s.staff) % len(gym_ids)],
            "user_id": staff_user_ids[i], "invite_email": f"smk-{tag}-staff-{i}@example.com",
            "full_name": f"Staff {tag} {i:04d}", "role": role, "is_trainer": role == EmployeeRole.TRAINER,
            "work_mode": WorkMode.BOTH, "employment_type": EmploymentType.FULL_TIME, "country_code": "IN",
            "currency": "INR", "monthly_salary": Decimal(25000 + 500 * (i % 20)),
            "hire_date": today - timedelta(days=30 + i % 900), "status": status,
            "photo_key": f"public/staff/{w.next():032x}.jpg", "is_public": not big or i % 2 == 0,
            "years_experience": i % 15, "verification_status": VerificationStatus.VERIFIED, "id_status": IdStatus.VERIFIED})
    await _rows(db, Employee, emp_rows)

    # ---------------------------------------------------------------- members' subscriptions
    plan_of = dict((await db.execute(
        select(Plan.gym_id, Plan.id).where(Plan.gym_id.in_([w.flagship.id, *gym_ids]), Plan.code == "offline"))).all())
    sub_gyms = [w.flagship.id if i % 3 == 0 else gym_ids[i % len(gym_ids)] for i in range(len(member_ids))]
    await _rows(db, Subscription, [
        {"customer_id": uid, "gym_id": gid, "plan_id": plan_of[gid], "status": SubscriptionStatus.ACTIVE,
         "channel": PurchaseChannel.ONLINE_CHECKOUT, "price": Decimal("1499"), "currency": "INR", "starts_on": today,
         "ends_on": today + timedelta(days=30), "activated_at": now}
        for uid, gid in zip(member_ids, sub_gyms, strict=True)
    ])

    # ---------------------------------------------------------------- notifications
    def note(uid: int, i: int) -> dict[str, Any]:
        return {"user_id": uid, "title": f"Notice {tag} {i}", "body": "Your plan renews soon.",
                "category": rnd.choice(list(NotificationCategory)), "severity": Severity.INFO,
                "link": "/account" if i % 3 else None, "read_at": now if i % 2 else None, "emailed": True,
                "created_at": ago(120)}

    notes = [note(w.member.id, i) for i in range(s.notes_member)]
    notes += [note(w.owner.id, i) for i in range(s.notes_owner)]
    notes += [note(member_ids[i % len(member_ids)], i) for i in range(s.notes_other)]
    await _rows(db, Notification, notes)

    # ---------------------------------------------------------------- products and orders
    def product(gid: int, i: int) -> dict[str, Any]:
        name, category, supplement = PRODUCT_NAMES[i % len(PRODUCT_NAMES)]
        title = f"{name} {tag}-{gid}-{i}"
        return {"gym_id": gid, "name": title, "slug": f"smk-{tag}-{gid}-{i}", "category": category, "brand": "PulseLabs",
                "description": f"{name} for daily training.", "price": Decimal(499 + 10 * (i % 50)), "currency": "INR",
                "stock": 0 if i % 9 == 0 else 25, "is_active": True, "is_supplement": supplement,
                "embedding": embed(f"{title} {category.value} {name}")}

    w.flagship_product_ids += await _rows(db, Product, [product(w.flagship.id, i) for i in range(s.flagship_products)],
                                          ids=True)
    other_products = await _rows(db, Product, [product(gid, i) for gid in gym_ids for i in range(s.products_per_gym)],
                                 ids=True)
    product_gym = {pid: gid for pid, gid in zip(other_products, [g for g in gym_ids for _ in range(s.products_per_gym)],
                                                 strict=True)}
    order_rows, item_products = [], []
    for i in range(s.orders_flagship + s.orders_other):
        flagship = i < s.orders_flagship
        if flagship:
            pid, gid = w.flagship_product_ids[i % len(w.flagship_product_ids)], w.flagship.id
            customer = w.member.id if i < s.orders_member else member_ids[i % len(member_ids)]
        else:
            pid = other_products[i % len(other_products)]
            gid, customer = product_gym[pid], member_ids[i % len(member_ids)]
        status = ORDER_STATUSES[i % len(ORDER_STATUSES)]
        created = ago(90)
        order_rows.append({
            "order_number": f"SMK{tag}{i:07d}".upper(), "gym_id": gid, "customer_id": customer,
            "customer_label": f"Member {i:05d}", "status": status, "fulfillment": "pickup",
            "pickup_branch_id": first_branch.get(gid), "subtotal": Decimal(999), "total": Decimal(999),
            "currency": "INR", "payment_provider": "test", "created_at": created,
            "paid_at": created if status not in (OrderStatus.PENDING_PAYMENT, OrderStatus.CANCELLED) else None})
        item_products.append(pid)
    order_ids = await _rows(db, ShopOrder, order_rows, ids=True)
    await _rows(db, ShopOrderItem, [{"order_id": oid, "product_id": pid, "product_name": "Seeded product",
                                     "unit_price": Decimal(999), "quantity": 1, "line_total": Decimal(999)}
                                    for oid, pid in zip(order_ids, item_products, strict=True)])

    # ---------------------------------------------------------------- audit logs, security, bans, mail, jobs
    audit = [{"actor_id": w.owner.id, "gym_id": w.big_gyms[i % len(w.big_gyms)],
              "action": rnd.choice(["shop.order_status", "staff.update", "gym.update", "plan.update"]),
              "entity_type": "shop_order", "entity_id": order_ids[i % len(order_ids)], "detail": {"n": i},
              "ip": "10.0.0.1", "created_at": ago(300)} for i in range(s.audit_owner)]
    audit += [{"actor_id": member_ids[i % len(member_ids)], "gym_id": gym_ids[i % len(gym_ids)],
               "action": rnd.choice(["auth.login", "subscription.create", "profile.update"]), "entity_type": "user",
               "entity_id": member_ids[i % len(member_ids)], "detail": {}, "ip": "10.0.0.2", "created_at": ago(300)}
              for i in range(s.audit_other)]
    await _rows(db, AuditLog, audit)
    categories = [c for c in ThreatCategory if c != ThreatCategory.OTHER]
    await _rows(db, SecurityEvent, [
        {"ip": f"203.0.113.{1 + i % 150}", "method": "GET", "path": f"/wp-login.php?x={i}", "user_agent": "curl/8",
         "category": rnd.choice(categories), "severity": 1 + i % 5, "detail": f"probe {tag} {i}",
         "action": "blocked" if i % 4 else "logged", "ai_reviewed": i % 3 == 0, "created_at": ago(7)}
        for i in range(s.events)
    ])
    await _rows(db, IpBan, [
        {"ip": f"198.18.{(w.seq + i) // 250 % 250}.{1 + (w.seq + i) % 250}", "reason": "Automated probe", "severity": 3,
         "banned_until": now + timedelta(days=7), "created_by": "system", "offense_count": 1, "created_at": ago(20)}
        for i in range(s.bans)
    ])
    w.seq += s.bans
    await _rows(db, EmailOutbox, [
        {"to_email": f"smk-{tag}-mail-{i}@example.com", "subject": "Your receipt", "text_body": "Thanks!",
         "status": rnd.choice([EmailStatus.SENT, EmailStatus.LOGGED, EmailStatus.FAILED]), "created_at": ago(25)}
        for i in range(s.mails)
    ])
    await _rows(db, Job, [
        {"kind": "smoke_noop", "payload": {}, "status": rnd.choice([JobStatus.DONE, JobStatus.FAILED]),
         "attempts": 1, "finished_at": now, "created_at": ago(10)} for i in range(s.jobs)
    ])

    # ---------------------------------------------------------------- conversations
    thread_rows = [{"gym_id": w.flagship.id, "customer_id": member_ids[i], "subject": "Question", "status": ThreadStatus.OPEN,
                    "last_message_at": ago(60), "last_message_preview": "See you tomorrow", "staff_unread": i % 2}
                   for i in range(s.threads_flagship)]
    thread_rows += [{"gym_id": gym_ids[i], "customer_id": w.member.id, "subject": "Membership", "status": ThreadStatus.OPEN,
                     "last_message_at": ago(60), "last_message_preview": "Thanks!", "customer_unread": i % 2}
                    for i in range(s.threads_member)]
    if w.long_thread_id is None:
        thread_rows.append({"gym_id": w.flagship.id, "customer_id": w.member.id, "subject": "Training plan",
                            "status": ThreadStatus.OPEN, "last_message_at": now, "last_message_preview": "Long chat"})
    thread_ids = await _rows(db, ChatThread, thread_rows, ids=True)
    if w.long_thread_id is None:
        w.long_thread_id = thread_ids[-1]
    customers = [r["customer_id"] for r in thread_rows]
    messages = [{"thread_id": tid, "sender_id": cid, "sender_role": "customer", "body": "Hello!", "created_at": ago(60)}
                for tid, cid in zip(thread_ids, customers, strict=True)]
    messages += [{"thread_id": w.long_thread_id, "sender_id": w.member.id if i % 2 else w.owner.id,
                  "sender_role": "customer" if i % 2 else "owner", "body": f"Message {tag} {i}",
                  "created_at": now - timedelta(minutes=s.long_thread_messages - i)}
                 for i in range(s.long_thread_messages)]
    await _rows(db, ChatMessage, messages)

    # ---------------------------------------------------------------- job postings
    posting_gyms = [*w.big_gyms, *gym_ids]
    postings = []
    for i in range(s.postings):
        gid = posting_gyms[i % len(posting_gyms)]
        postings.append({
            "gym_id": gid, "branch_id": first_branch.get(gid), "title": f"{rnd.choice(['Trainer', 'Front desk', 'Coach'])} {i}",
            "role": EMPLOYEE_ROLES[i % len(EMPLOYEE_ROLES)], "employment_type": EmploymentType.FULL_TIME,
            "location": "On site", "salary_min": Decimal(20000), "salary_max": Decimal(40000), "currency": "INR",
            "requirements": "Certified, friendly.", "is_open": True, "created_at": ago(30)})
    await _rows(db, JobPosting, postings)
    await db.commit()


# ==================================================================================================== measuring
@dataclass
class Probe:
    label: str
    who: str
    url: str
    pattern: str | None  # a regex whose DISTINCT matches are the rendered rows (None: no list on the page)
    page_size: int | None
    required: bool = True


PROBES = [
    Probe("Home", "anon", "/", r'href="/gyms/([a-z0-9-]+)[?"]', 6 + 8),
    Probe("Gym directory", "anon", "/gyms", r'href="/gyms/([a-z0-9-]+)[?"]', 12),
    Probe("Shop", "anon", "/shop", r'class="shop-card group"', 24),
    Probe("Shop search", "anon", "/shop?q=whey", r'class="shop-card group"', 24),
    Probe("Jobs near me", "anon", "/jobs?lat=12.97&lon=77.59", r'href="/jobs/(\d+)', 10),
    Probe("Owner staff", "owner", "/owner/staff", r'href="/owner/staff/(\d+)"', 30),
    Probe("Owner shop orders", "owner", "/owner/shop/orders", r'href="/owner/shop/orders/(\d+)"', 30),
    Probe("Super-admin overview", "admin", "/superadmin", None, None),
    Probe("Super-admin security", "admin", "/superadmin/security", r'class="sa-row sa-cols-events"', 40),
    Probe("Chat (owner)", "owner", "/chat", r'href="/chat/(\d+)', 50),
    Probe("Chat (member)", "member", "/chat", r'href="/chat/(\d+)', 50),
    Probe("Notifications", "member", "/account/notifications", r'<li class="notif-item', 20),
    # not in the brief, same rules
    Probe("Gym page (150 branches)", "anon", "/gyms/{flagship}", r'href="/gyms/[a-z0-9-]+/branches/(\d+)', 12, False),
    Probe("Member orders", "member", "/shop/orders", r'href="/shop/orders/(\d+)"', 20, False),
    Probe("Bell feed API", "member", "/api/notifications", None, None, False),
    Probe("Long conversation", "member", "/chat/{long_thread}", r'data-msg-id="(\d+)"', 50, False),
    Probe("Owner dashboard", "owner", "/owner", None, None, False),
    Probe("Owner members", "owner", "/owner/customers", r'href="/owner/customers/(\d+)"', 24, False),
    Probe("Owner audit", "owner", "/owner/audit", None, None, False),
    Probe("Owner products", "owner", "/owner/shop/products", None, None, False),
    Probe("Super-admin users", "admin", "/superadmin/users", None, None, False),
    Probe("Super-admin audit", "admin", "/superadmin/audit", None, None, False),
    Probe("Super-admin bans", "admin", "/superadmin/bans", None, None, False),
    Probe("Super-admin outbox", "admin", "/superadmin/outbox", None, None, False),
]


class Meter:
    """Statements, rows per statement (the asyncpg cursor buffers a statement's whole result) and ORM loads."""

    def __init__(self) -> None:
        self.rows: list[tuple[int, str]] = []
        self.entities = 0

    def _after(self, conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        buffered = getattr(cursor, "_rows", None)
        self.rows.append((len(buffered) if buffered is not None else 0, statement))

    def _load(self, target, context):  # noqa: ANN001
        self.entities += 1

    def __enter__(self) -> "Meter":
        event.listen(dbmod.get_engine().sync_engine, "after_cursor_execute", self._after)
        event.listen(dbmod.Base, "load", self._load, propagate=True)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(dbmod.get_engine().sync_engine, "after_cursor_execute", self._after)
        event.remove(dbmod.Base, "load", self._load)

    @property
    def max_rows(self) -> tuple[int, str]:
        return max(self.rows, default=(0, ""))


def clear_caches() -> None:
    for cache in list(ttl_cache._caches.values()):  # noqa: SLF001 - every in-process TTL cache: the cold case
        cache.clear()


async def measure(clients: dict[str, Any], probe: Probe, url: str) -> dict[str, Any]:
    """Two cold requests (TTL caches cleared before each): the fewer statements of the two (a session refresh every
    5 minutes is not part of the page) and the slower time; then one warm request (caches filled)."""
    client = clients[probe.who]
    clear_caches()
    warm_up = await client.get(url)  # first-use costs: not part of the page
    assert warm_up.status_code == 200, (probe.label, url, warm_up.status_code, warm_up.text[:300])
    runs = []
    for _ in range(2):
        clear_caches()
        with Meter() as meter, QueryCounter() as qc:
            start = time.perf_counter()
            r = await client.get(url)
            elapsed = time.perf_counter() - start
        assert r.status_code == 200, (probe.label, url, r.status_code, r.text[:300])
        runs.append((qc.count, elapsed, meter, r))
    start = time.perf_counter()
    again = await client.get(url)
    warm = time.perf_counter() - start
    assert again.status_code == 200, (probe.label, url, again.status_code)
    queries, _, meter, r = min(runs, key=lambda run: run[0])
    rendered = None
    if probe.pattern:
        found = re.findall(probe.pattern, r.text)
        rendered = len(found) if probe.pattern.startswith(("class=", "<")) else len(set(found))
    max_rows, max_sql = meter.max_rows
    return {"label": probe.label, "who": probe.who, "url": url, "status": r.status_code, "queries": queries,
            "cold_s": round(max(run[1] for run in runs), 3), "warm_s": round(warm, 3), "rendered": rendered,
            "page_size": probe.page_size, "max_rows": max_rows, "max_rows_sql": max_sql[:300],
            "rows_total": sum(n for n, _ in meter.rows), "entities": meter.entities, "required": probe.required,
            "bytes": len(r.content)}


async def measure_all(clients: dict[str, Any], w: World) -> dict[str, dict[str, Any]]:
    out = {}
    flagship_slug = w.flagship.slug
    for probe in PROBES:
        url = probe.url.format(flagship=flagship_slug, long_thread=w.long_thread_id)
        out[probe.label] = await measure(clients, probe, url)
    return out


# ==================================================================================================== the test
async def test_busy_pages_stay_fast_and_bounded_on_a_large_platform(db):
    world = await build_world(db)
    clients = {"anon": make_client(), "owner": make_client(), "member": make_client(), "admin": make_client()}
    try:
        await login_as(clients["owner"], world.owner)
        await login_as(clients["member"], world.member)
        await login_as(clients["admin"], world.admin)

        start = time.perf_counter()
        await grow(db, world, SMALL, "s")
        small = await measure_all(clients, world)
        await grow(db, world, LARGE, "l")
        seed_s = time.perf_counter() - start
        large = await measure_all(clients, world)
    finally:
        for c in clients.values():
            await c.aclose()

    totals = {t: int(await db.scalar(text(f"SELECT count(*) FROM {t}"))) for t in (  # noqa: S608 - fixed names
        "gyms", "branches", "users", "notifications", "shop_orders", "audit_logs", "products", "employees",
        "chat_threads", "chat_messages", "job_postings", "security_events", "subscriptions")}
    totals["members"] = int(await db.scalar(text("SELECT count(*) FROM users WHERE role = 'customer'")))

    report = {"seed_seconds": round(seed_s, 1), "totals": totals, "max_seconds": MAX_SECONDS,
              "pages": [{**large[k], "queries_small": small[k]["queries"], "cold_s_small": small[k]["cold_s"],
                         "rendered_small": small[k]["rendered"]} for k in large]}
    if os.environ.get("SMOKE_REPORT"):
        with open(os.environ["SMOKE_REPORT"], "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)

    assert (totals["gyms"], totals["branches"], totals["members"]) == (300, 2000, 5000), totals
    assert (totals["notifications"], totals["shop_orders"], totals["audit_logs"]) == (20000, 3000, 10000), totals
    assert totals["products"] == 3000, totals

    problems = []
    for label, m in large.items():
        s = small[label]
        if m["queries"] != s["queries"]:
            problems.append(f"{label}: {s['queries']} statements on a small platform, {m['queries']} on a large one")
        if m["cold_s"] > MAX_SECONDS:
            problems.append(f"{label}: {m['cold_s']} s (limit {MAX_SECONDS} s)")
        if m["max_rows"] > MAX_ROWS:
            problems.append(f"{label}: one statement returned {m['max_rows']} rows: {m['max_rows_sql']}")
        if m["entities"] > MAX_ENTITIES:
            problems.append(f"{label}: {m['entities']} ORM objects loaded")
        if m["page_size"] is not None and not (0 < (m["rendered"] or 0) <= m["page_size"]):
            problems.append(f"{label}: {m['rendered']} rows rendered (one page = {m['page_size']})")
    assert not problems, "\n".join(problems)
