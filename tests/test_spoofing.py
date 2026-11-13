"""Cross-account isolation ("spoofing") audit - can one account see or change another account's data?

World: two gyms from the demo dataset - the VICTIM gym (Forge & Flow) and the ATTACKERS' gym (Harbor Strength).
The victim side's private rows are tagged with unique marker strings: a member's health conditions and notes,
tasks, notifications, chat, delivery address, refund, identity document, health alert, live-coach session, AI
assistant history and e-mails; the owner's misconduct reports, job applications, payroll, finance ledger, coupons,
staff documents, an unpublished branch and a hidden product.

1. Positive control: the victim's own principals do see those markers (otherwise the crawl would prove nothing).
2. GET crawl: every GET route as anonymous, a member, the owner and a trainer of the *other* gym, and another member
   of the *same* gym - with the victim's IDs in the path and in ID-like query parameters (?gym=, ?customer= ...).
   No response may contain a victim marker or a victim's e-mail/phone, and private files must not be served.
3. POST crawl: every POST route as the same attackers, with the victim's IDs in the path and in every ID-like
   form/JSON field. None of the victim's rows may change and nothing may be attached to them.
4. E-mails produced during the attacks never carry victim data to an attacker.
Plus focused tests: forged/foreign session cookies, CSRF tokens from another session, spoofed X-Forwarded-For /
X-Real-IP / X-Forwarded-Proto, Host-header poisoning of e-mailed links, identifier impersonation at sign-in,
realtime fan-out, AI answer caches, and many accounts requesting pages concurrently.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from starlette.routing import Route

from app.core.utils import utcnow
from app.db import Base, get_sessionmaker
from app.models import (
    AssistantMessage,
    Branch,
    BranchDocument,
    BranchPhoto,
    ChatMessage,
    ChatThread,
    Coupon,
    CustomerProfile,
    EmailOutbox,
    Employee,
    EmployeeDocument,
    Gym,
    HealthAlert,
    HealthCondition,
    IdentityDocument,
    JobEnquiry,
    JobPosting,
    MisconductReport,
    Notification,
    Plan,
    Product,
    ProductImage,
    ProductReview,
    Refund,
    SalaryStatement,
    ShopOrder,
    Subscription,
    Task,
    TrainerClient,
    TrainerShift,
    Transaction,
    User,
    WellnessCheck,
    WorkoutSession,
    WorkoutSessionEvent,
)
from app.models.enums import (
    ConditionKind,
    NotificationCategory,
    Role,
    Severity,
    TaskKind,
    TxCategory,
    TxDirection,
    TxStatus,
    VerificationStatus,
)
from scripts.demo.loader import seed_demo
from tests.conftest import csrf_token, login_as, make_client

DOMAIN = "demo.ironpulse.local"
M = "zqx" + secrets.token_hex(4)  # marker prefix, unique per run
MARKER_RE = re.compile(re.escape(M) + r"[-a-z0-9]*", re.IGNORECASE)
SKIP_PATHS = {"/ws", "/maps/tiles/{z}/{x}/{y}.png", "/favicon.ico", "/robots.txt"}
TABS = ("documents", "schedule", "clients", "staff", "profile", "identity", "branches")
SKIP_PREFIXES = ("/static", "/api/geo", "/api/tts")  # external services / static files
# POSTs that would end the attacker's own session or account (they can't touch anyone else's data anyway)
SELF_DESTRUCT = {"/auth/logout", "/account/delete", "/account/password"}
# tables that are logs / queues / per-request state - not account data
IGNORED_TABLES = {"audit_logs", "security_events", "ip_bans", "user_sessions", "auth_tokens", "jobs", "ai_call_logs",
                  "email_outbox"}
# inserts an outsider may legitimately cause on the victim gym (reported for review, never silently accepted)
REVIEW_INSERT_TABLES = {"notifications", "job_enquiries"}
# columns that legitimately change as a side effect of other people's actions
BENIGN_COLUMNS = {"updated_at", "last_seen_at", "last_login_at", "last_login_ip", "used_count"}


def _iter_routes(routes: Any):
    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from _iter_routes(inner.routes)
        elif isinstance(r, Route):
            yield r


def _routes(app: Any, method: str) -> list[str]:
    out = []
    for r in _iter_routes(app.routes):
        if method in (r.methods or set()) and r.path not in SKIP_PATHS and not r.path.startswith(SKIP_PREFIXES):
            out.append(r.path)
    return sorted(set(out))


# ====================================================================================================== the world
@dataclass
class World:
    forge: Gym
    harbor: Gym
    people: dict[str, User]  # dataset key -> user
    ids: dict[str, list[Any]]  # path parameter -> victim values (first = default)
    victim_users: set[int]
    pii: list[str]
    markers: dict[str, str] = field(default_factory=dict)
    slugs: dict[str, str] = field(default_factory=dict)

    def user(self, key: str) -> User:
        return self.people[key]


async def _one(db: Any, stmt: Any) -> Any:
    return (await db.execute(stmt)).scalars().first()


async def build_world(db: Any) -> World:
    await seed_demo(db, password="Demo-Passw0rd!", gyms=["forge-and-flow", "harbor-strength"], write_credentials=False)
    await db.commit()
    keys = ["owner", "kabir", "meera", "arjun", "sana", "lucas", "ishaan", "rohan", "anaya", "kavya.menon", "aditya.rao",
            "owner2", "zoya.merchant", "farhan.qureshi", "tanvi.desai", "dev.malhotra", "sameer.khan"]
    people = {k: await _one(db, select(User).where(User.email == f"{k}@{DOMAIN}")) for k in keys}
    forge = await _one(db, select(Gym).where(Gym.slug == "forge-and-flow"))
    harbor = await _one(db, select(Gym).where(Gym.slug == "harbor-strength"))
    rohan, kavya, owner = people["rohan"], people["kavya.menon"], people["owner"]
    mk = {name: f"{M}-{name}" for name in ("health", "notes", "task", "notif", "chat", "addr", "refund", "idname", "alert",
                                           "live", "assistant", "report", "enquiry", "salary", "ledger", "hidden",
                                           "draftbranch", "mail", "empdoc", "wellness")}
    mk["coupon"] = f"{M.upper()}CODE"

    # ---- the member's private data
    condition = HealthCondition(customer_id=rohan.id, kind=ConditionKind.CONDITION, name=mk["health"], source="self")
    task = Task(user_id=rohan.id, title=mk["task"], kind=TaskKind.TASK, due_at=utcnow() + timedelta(days=1))
    notif = Notification(user_id=rohan.id, title=mk["notif"], body=mk["notif"], category=NotificationCategory.SYSTEM,
                         severity=Severity.INFO)
    live = WorkoutSession(customer_id=rohan.id, title=mk["live"])
    db.add_all([condition, task, notif, live,
                AssistantMessage(user_id=rohan.id, role="user", content=mk["assistant"]),
                EmailOutbox(user_id=rohan.id, to_email=rohan.email, subject=mk["mail"], text_body=mk["mail"])])
    await db.flush()
    db.add(WorkoutSessionEvent(session_id=live.id, kind="user_said", message=mk["live"]))
    wellness = WellnessCheck(customer_id=rohan.id)
    db.add(wellness)
    profile = await db.get(CustomerProfile, rohan.id)
    profile.health_notes = mk["notes"]
    doc = await _one(db, select(IdentityDocument).where(IdentityDocument.user_id == rohan.id))
    doc.name_on_id = mk["idname"]
    alert = await _one(db, select(HealthAlert).where(HealthAlert.customer_id == rohan.id))
    alert.message = f"{alert.message} {mk['alert']}"
    thread = await _one(db, select(ChatThread).where(ChatThread.customer_id == rohan.id, ChatThread.gym_id == forge.id))
    db.add(ChatMessage(thread_id=thread.id, sender_id=rohan.id, sender_role="customer", body=mk["chat"]))
    thread.last_message_preview = mk["chat"]
    kavya_order = await _one(db, select(ShopOrder).where(ShopOrder.provider_order_id == "DEMO-FF-1004"))
    kavya_order.shipping_address = {**(kavya_order.shipping_address or {}), "line1": mk["addr"]}
    refund = await _one(db, select(Refund).where(Refund.order_id == kavya_order.id))
    refund.reason = mk["refund"]
    rohan_order = await _one(db, select(ShopOrder).where(ShopOrder.provider_order_id == "DEMO-FF-1002"))

    # ---- the owner's private data
    report = await _one(db, select(MisconductReport).where(MisconductReport.gym_id == forge.id))
    report.description = f"{report.description} {mk['report']}"
    job = await _one(db, select(JobPosting).where(JobPosting.gym_id == forge.id).order_by(JobPosting.id))
    enquiry = await _one(db, select(JobEnquiry).where(JobEnquiry.job_id == job.id))
    enquiry.message = mk["enquiry"]
    statement = await _one(db, select(SalaryStatement).where(SalaryStatement.gym_id == forge.id))
    if statement is not None:
        statement.ai_explanation = mk["salary"]
    db.add(Transaction(gym_id=forge.id, direction=TxDirection.INCOMING, category=TxCategory.SUBSCRIPTION,
                       status=TxStatus.SUCCESS, amount=Decimal("1"), currency="INR", description=mk["ledger"],
                       party_label="Member", provider="test", provider_order_id=f"{M}-tx", completed_at=utcnow()))
    coupon = Coupon(gym_id=forge.id, code=mk["coupon"], percent_off=5, is_active=True)
    db.add(coupon)
    hidden = await _one(db, select(Product).where(Product.gym_id == forge.id, Product.sku == "IS-DUFFEL-40"))
    hidden.name, hidden.is_active = mk["hidden"], False
    whey = await _one(db, select(Product).where(Product.gym_id == forge.id, Product.sku == "PF-WHEY-ISO-1KG"))
    draft = await _one(db, select(Branch).where(Branch.gym_id == forge.id,
                                               Branch.verification_status != VerificationStatus.VERIFIED))
    draft.address_line2 = mk["draftbranch"]
    branch = await _one(db, select(Branch).where(Branch.gym_id == forge.id, Branch.name == "Indiranagar"))
    kabir_emp = await _one(db, select(Employee).where(Employee.user_id == people["kabir"].id))
    sana_emp = await _one(db, select(Employee).where(Employee.user_id == people["sana"].id))
    emp_doc = EmployeeDocument(employee_id=kabir_emp.id, doc_type="certification", file_key=f"private/staff_docs/{'0' * 32}.pdf",
                               original_filename=f"{mk['empdoc']}.pdf", content_type="application/pdf", sha256="0" * 64,
                               size_bytes=1, ocr_text=mk["empdoc"])
    db.add(emp_doc)
    await db.flush()
    await db.commit()

    sub = await _one(db, select(Subscription).where(Subscription.customer_id == rohan.id))
    ids: dict[str, list[Any]] = {
        "gym_id": [forge.id], "gym_slug": [forge.slug], "branch_id": [branch.id, draft.id],
        "employee_id": [kabir_emp.id, sana_emp.id], "customer_id": [rohan.id], "user_id": [rohan.id, owner.id],
        "product_id": [hidden.id, whey.id], "order_id": [rohan_order.id, kavya_order.id], "refund_id": [refund.id],
        "thread_id": [thread.id], "sid": [sub.id], "subscription_id": [sub.id], "statement_id": [statement.id] if statement else [],
        "job_id": [job.id], "enquiry_id": [enquiry.id], "report_id": [report.id], "nid": [notif.id], "task_id": [task.id],
        "session_id": [live.id], "alert_id": [alert.id], "condition_id": [condition.id], "check_id": [wellness.id],
        "email_id": [await _one(db, select(EmailOutbox.id).where(EmailOutbox.subject == mk["mail"]))],
        "image_id": [await _one(db, select(ProductImage.id).where(ProductImage.product_id == whey.id))],
        "photo_id": [await _one(db, select(BranchPhoto.id).where(BranchPhoto.branch_id == branch.id))],
        "shift_id": [await _one(db, select(TrainerShift.id).where(TrainerShift.employee_id == kabir_emp.id))],
        "link_id": [await _one(db, select(TrainerClient.id).where(TrainerClient.customer_id == rohan.id))],
        "plan_id": [await _one(db, select(Plan.id).where(Plan.gym_id == forge.id))],
        "coupon_id": [coupon.id],
        "review_id": [await _one(db, select(ProductReview.id).where(ProductReview.user_id == rohan.id))],
        "identity_doc": [doc.id], "branch_doc": [await _one(db, select(BranchDocument.id).where(BranchDocument.branch_id == draft.id))],
        "employee_doc": [emp_doc.id], "key": [doc.front_key], "side": ["front"],
    }
    victims = {u.id for k, u in people.items() if k in ("owner", "kabir", "meera", "arjun", "sana", "lucas", "rohan", "anaya",
                                                        "kavya.menon", "aditya.rao")}
    pii = [rohan.email, kavya.email, people["anaya"].email, owner.email, "98450 88888", "9845088888", "98450 10101",
           "9845010101"]
    return World(forge=forge, harbor=harbor, people=people, ids=ids, victim_users=victims, pii=pii, markers=mk,
                 slugs={"product": whey.slug, "gym": forge.slug})


def fill(path: str, w: World, variant: int = 0) -> str | None:
    """Substitute the victim's IDs into a route path (None if a parameter has no victim value)."""
    def pick(values: list[Any]) -> Any:
        return values[min(variant, len(values) - 1)] if values else None

    out = path
    for name in re.findall(r"{([a-z_]+)(?::[a-z]+)?}", path):
        if name == "doc_id":
            key = "branch_doc" if "/branches/" in path or "branch-doc" in path else (
                "employee_doc" if "/staff/" in path or "employee-doc" in path or "/employments/" in path else "identity_doc")
            value = pick(w.ids[key])
        elif name == "slug":
            value = w.slugs["product"] if path.startswith("/shop/p/") else (
                None if "videos" in path else w.slugs["gym"])
        elif name == "employee_id" and path.startswith("/staff/employments/"):
            value = pick(w.ids["employee_id"])
        else:
            value = pick(w.ids.get(name, []))
        if value is None:
            return None
        out = re.sub(r"{" + name + r"(?::[a-z]+)?}", str(value), out, count=1)
    return out


def idor_query(w: World) -> str:
    i = w.ids
    pairs = {"gym": i["gym_id"][0], "gym_id": i["gym_id"][0], "branch": i["branch_id"][0], "branch_id": i["branch_id"][0],
             "customer": i["customer_id"][0], "customer_id": i["customer_id"][0], "user": i["user_id"][0],
             "user_id": i["user_id"][0], "employee": i["employee_id"][0], "employee_id": i["employee_id"][0],
             "thread": i["thread_id"][0], "thread_id": i["thread_id"][0], "order": i["order_id"][0], "product": i["product_id"][0],
             "sid": i["sid"][0], "status": "all"}
    return "&".join(f"{k}={v}" for k, v in pairs.items())


def idor_payload(w: World) -> dict[str, Any]:
    i = w.ids
    text = f"attacker-{M}-wrote"  # not a victim marker: contains no "-<marker name>" the victim owns
    return {
        "body": text, "message": text, "text": text, "reason": text, "note": text, "title": text, "description": text,
        "name": text, "status": "approved", "decision": "approve", "action": "approve", "rating": "5", "quantity": "1",
        "qty": "1", "amount": "1", "confirm": "1", "otp": "123456", "code": "123456", "question": "Is this safe for me?",
        "email": w.user("rohan").email, "identifier": w.user("rohan").email,
        "gym_id": i["gym_id"][0], "gym": i["gym_id"][0], "branch_id": i["branch_id"][0], "branch": i["branch_id"][0],
        "employee_id": i["employee_id"][0], "employee": i["employee_id"][0], "trainer_id": i["employee_id"][0],
        "customer_id": i["customer_id"][0], "customer": i["customer_id"][0], "user_id": i["user_id"][0],
        "plan_id": i["plan_id"][0], "product_id": i["product_id"][1], "order_id": i["order_id"][0],
        "thread_id": i["thread_id"][0], "subscription_id": i["sid"][0], "sid": i["sid"][0], "job_id": i["job_id"][0],
        "session_id": i["session_id"][0],
    }


def leaks_in(text: str, w: World, echoed: set[str] = frozenset()) -> list[str]:
    """Victim markers / contact details in ``text`` - except values the requester sent themselves (``echoed``):
    a form re-rendered with the attacker's own input is not a leak."""
    found = sorted({m.group(0).lower() for m in MARKER_RE.finditer(text)} - {f"{M}-wrote".lower()})
    found += [p for p in w.pii if p.lower() in text.lower() and p.lower() not in echoed]
    return found


async def fetch(client: Any, method: str, url: str, **kw: Any) -> Any:
    try:
        return await asyncio.wait_for(client.request(method, url, **kw), timeout=25)
    except (asyncio.TimeoutError, TimeoutError):
        return None


def _scan(r: Any, w: World, echoed: set[str] = frozenset()) -> tuple[list[str], bool]:
    """(markers/PII found, private binary served)"""
    ctype = r.headers.get("content-type", "")
    if ctype.startswith(("image/", "application/pdf", "application/octet-stream")):
        return [], r.status_code == 200
    return leaks_in(r.text, w, echoed), False


# ====================================================================================================== snapshots
async def snapshot() -> dict[str, dict[tuple, dict[str, Any]]]:
    out: dict[str, dict[tuple, dict[str, Any]]] = {}
    async with get_sessionmaker()() as s:
        for t in Base.metadata.sorted_tables:
            if t.name in IGNORED_TABLES:
                continue
            pk = [c.name for c in t.primary_key.columns]
            rows = (await s.execute(select(t))).mappings().all()
            out[t.name] = {tuple(r[k] for k in pk): dict(r) for r in rows}
    return out


def _owners(table: str, row: dict[str, Any], idx: dict[str, dict[Any, tuple[set, set]]]) -> tuple[set[int], set[int]]:
    """(gym ids, user ids) a row belongs to - directly or through its parent rows."""
    gyms: set[int] = set()
    users: set[int] = set()
    if table == "gyms":
        gyms.add(row["id"])
    if table == "users":
        users.add(row["id"])
    if row.get("gym_id"):
        gyms.add(row["gym_id"])
    for col in ("customer_id", "user_id", "reporter_id", "requested_by_id", "sender_id", "uploaded_by_id"):
        if row.get(col):
            users.add(row[col])
    for col, parent in (("branch_id", "branches"), ("employee_id", "employees"), ("product_id", "products"),
                        ("order_id", "shop_orders"), ("thread_id", "chat_threads"), ("session_id", "workout_sessions"),
                        ("subscription_id", "subscriptions"), ("job_id", "job_postings"), ("plan_id", "plans"),
                        ("health_alert_id", "health_alerts")):
        value = row.get(col)
        if value is not None and value in idx.get(parent, {}):
            g, u = idx[parent][value]
            gyms |= g
            users |= u
    return gyms, users


def _index(snap: dict[str, dict[tuple, dict[str, Any]]]) -> dict[str, dict[Any, tuple[set, set]]]:
    idx: dict[str, dict[Any, tuple[set, set]]] = {}
    for parent in ("branches", "employees", "products", "job_postings", "plans", "subscriptions", "shop_orders",
                   "chat_threads", "workout_sessions", "health_alerts"):
        idx[parent] = {}
        for (pk,), row in snap.get(parent, {}).items():
            idx[parent][pk] = _owners(parent, row, idx)
    return idx


def diff(before: dict, after: dict, w: World, attacker: User | None) -> tuple[list[str], list[str]]:
    """(violations, review notes) for rows of the victim side changed between two snapshots."""
    violations: list[str] = []
    review: list[str] = []
    idx_b, idx_a = _index(before), _index(after)
    me = attacker.id if attacker else None
    victim_gym = w.forge.id
    for table, rows in before.items():
        new_rows = after.get(table, {})
        for pk, row in rows.items():
            gyms, users = _owners(table, row, idx_b)
            victim = victim_gym in gyms or bool(users & (w.victim_users - {me}))
            if not victim or (me in users):
                continue
            if pk not in new_rows:
                violations.append(f"DELETED {table}{list(pk)}")
                continue
            changed = sorted(k for k, v in new_rows[pk].items() if row.get(k) != v and k not in BENIGN_COLUMNS
                             and not (table == "products" and k == "stock"))
            if changed:
                violations.append(f"CHANGED {table}{list(pk)} {changed}")
    for table, rows in after.items():
        for pk, row in rows.items():
            if pk in before.get(table, {}):
                continue
            gyms, users = _owners(table, row, idx_a)
            others = users & (w.victim_users - {me})
            if me in users and not others and not ({"thread_id", "order_id", "session_id"} & {k for k, v in row.items() if v}):
                if victim_gym in gyms:
                    review.append(f"attacker-owned insert on the victim gym: {table}{list(pk)}")
                continue
            if others or victim_gym in gyms:
                (review if table in REVIEW_INSERT_TABLES else violations).append(f"INSERTED {table}{list(pk)}")
    return violations, review


# ====================================================================================================== the audit
@pytest.mark.usefixtures("ai_down")
async def test_no_account_can_read_or_change_another_accounts_data(app, db):
    w = await build_world(db)
    gets, posts = _routes(app, "GET"), _routes(app, "POST")
    assert len(gets) > 100 and len(posts) > 100, "route discovery is broken - the audit would be vacuous"

    get_urls: list[str] = []
    for path in gets:
        for variant in (0, 1):
            url = fill(path, w, variant)
            if not url or url in get_urls:
                continue
            get_urls.append(url)
            if re.search(r"/\d+(/|$)", url):  # detail pages render some data only on a tab
                get_urls += [f"{url}?tab={tab}" for tab in TABS]

    # ---- 1. positive control: the rightful owners see their markers
    seen_names: set[str] = set()
    for key in ("rohan", "kavya.menon", "owner", "arjun", "kabir"):
        async with make_client() as c:
            await login_as(c, w.user(key))
            for url in get_urls:
                r = await fetch(c, "GET", url, follow_redirects=True)
                if r is not None and r.status_code == 200:
                    body = r.text.lower()
                    seen_names |= {name for name, value in w.markers.items() if value.lower() in body}
    expected = {"health", "task", "notif", "chat", "addr", "refund", "report", "enquiry", "ledger", "coupon", "hidden",
                "draftbranch", "live", "empdoc"}
    missing = expected - seen_names
    assert not missing, f"positive control failed - these markers are never shown even to their owner: {sorted(missing)}"

    # ---- 2. GET crawl as attackers
    attackers: dict[str, User | None] = {"anonymous": None, "member-of-other-gym": w.user("zoya.merchant"),
                                         "owner-of-other-gym": w.user("owner2"), "trainer-of-other-gym": w.user("farhan.qureshi"),
                                         "other-member-same-gym": w.user("aditya.rao")}
    query = idor_query(w)
    leaks: list[str] = []
    for label, user in attackers.items():
        async with make_client() as c:
            if user is not None:
                await login_as(c, user)
            for url in get_urls:
                for target in (url, f"{url}{'&' if '?' in url else '?'}{query}"):
                    r = await fetch(c, "GET", target, follow_redirects=True)
                    if r is None:
                        continue
                    found, binary = _scan(r, w)
                    if found:
                        leaks.append(f"[{label}] GET {target} -> {r.status_code}: {found[:4]}")
                    if binary and (str(w.ids["identity_doc"][0]) in target or "private" in target):
                        leaks.append(f"[{label}] GET {target} -> served a private file")
            if user is not None:  # the crawl must not have locked the attacker out (that would hide leaks)
                assert (await c.get("/account")).status_code == 200, f"{label} was locked out during the crawl"
    assert not leaks, "cross-account READ leaks:\n" + "\n".join(leaks)

    # ---- 3. POST crawl as attackers (victim IDs in the path and in every ID-like field)
    payload = idor_payload(w)
    violations: list[str] = []
    review: list[str] = []
    for label, user in attackers.items():
        async with make_client() as c:
            if user is not None:
                await login_as(c, user)
            token = await csrf_token(c)
            for path in posts:
                if path in SELF_DESTRUCT:
                    continue
                url = fill(path, w, 0)
                if url is None:
                    continue
                before = await snapshot()
                if url.startswith("/api/"):
                    r = await fetch(c, "POST", url, json=payload, headers={"X-CSRF-Token": token, "Accept": "application/json"})
                else:
                    r = await fetch(c, "POST", url, data={**payload, "csrf_token": token})
                after = await snapshot()
                bad, notes = diff(before, after, w, user)
                status = r.status_code if r is not None else "timeout"
                violations += [f"[{label}] POST {url} -> {status}: {v}" for v in bad]
                review += [f"[{label}] POST {url} -> {status}: {n}" for n in notes]
                if r is not None:
                    found, _ = _scan(r, w, {str(v).lower() for v in payload.values()})
                    if found:
                        violations.append(f"[{label}] POST {url} -> {r.status_code} response leaks {found[:4]}")
    if review:
        print("\nREVIEW (legitimate cross-tenant side effects?):\n" + "\n".join(review))
    assert not violations, "cross-account WRITE violations:\n" + "\n".join(violations)

    # ---- 4. nothing generated during the attacks e-mails victim data to an attacker
    emails = [u.email for u in attackers.values() if u is not None]
    async with get_sessionmaker()() as s:
        rows = (await s.execute(select(EmailOutbox).where(EmailOutbox.to_email.in_(emails)))).scalars().all()
    mail_leaks = [f"{m.to_email}: {m.subject}" for m in rows if leaks_in(f"{m.subject}\n{m.text_body}\n{m.html_body or ''}", w)]
    assert not mail_leaks, "victim data e-mailed to an attacker:\n" + "\n".join(mail_leaks)


# ====================================================================================================== sessions & CSRF
async def test_forged_and_foreign_session_cookies_are_anonymous(db):
    from app.security.sessions import session_cookie_name

    await seed_demo(db, password="Demo-Passw0rd!", gyms=["harbor-strength"], write_credentials=False)
    await db.commit()
    zoya = await _one(db, select(User).where(User.email == f"zoya.merchant@{DOMAIN}"))
    async with make_client() as c:
        for forged in (str(zoya.id), f"user:{zoya.id}", secrets.token_urlsafe(32), "../../" + str(zoya.id), ""):
            c.cookies.set(session_cookie_name(), forged)
            r = await c.get("/me")
            assert r.status_code in (302, 303, 307) and "/auth/login" in r.headers["location"], forged
        await login_as(c, zoya)
        assert (await c.get("/me")).status_code == 200
        token = c.cookies.get(session_cookie_name())
        from app.security.sessions import revoke_session  # DB row + the Redis fast-path entry

        await revoke_session(db, token)
        await db.commit()
        r = await c.get("/me")
        assert r.status_code in (302, 303, 307), "a revoked session still works"


async def test_csrf_token_from_another_session_is_rejected(db):
    await seed_demo(db, password="Demo-Passw0rd!", gyms=["harbor-strength"], write_credentials=False)
    await db.commit()
    zoya = await _one(db, select(User).where(User.email == f"zoya.merchant@{DOMAIN}"))
    dev = await _one(db, select(User).where(User.email == f"dev.malhotra@{DOMAIN}"))
    async with make_client() as attacker, make_client() as victim:
        await login_as(attacker, zoya)
        await login_as(victim, dev)
        stolen = await csrf_token(attacker)
        await csrf_token(victim)  # the victim has a CSRF cookie of their own
        r = await victim.post("/tasks", data={"title": "planted by someone else", "csrf_token": stolen})
        assert r.status_code == 403
        r = await victim.post("/tasks", data={"title": "cross-site"}, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
    assert not await _one(db, select(Task).where(Task.title.in_(["planted by someone else", "cross-site"])))


# ====================================================================================================== address spoofing
def _scope(peer: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    return {"type": "http", "client": (peer, 5555),
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}


def test_forwarded_headers_cannot_spoof_the_client_address(monkeypatch):
    from app.config import get_settings
    from app.security.net import client_ip_from_scope

    s = get_settings()
    monkeypatch.setattr(s, "trust_proxy_headers", False)
    assert client_ip_from_scope(_scope("198.51.100.7", {"X-Forwarded-For": "127.0.0.1"})) == "198.51.100.7"
    assert client_ip_from_scope(_scope("198.51.100.7", {"X-Real-IP": "10.0.0.1"})) == "198.51.100.7"

    monkeypatch.setattr(s, "trust_proxy_headers", True)
    monkeypatch.setattr(s, "trusted_proxies", ["127.0.0.1", "10.0.0.0/8"])
    # a direct connection from the internet: its headers are ignored even with proxy support switched on
    assert client_ip_from_scope(_scope("198.51.100.7", {"X-Forwarded-For": "203.0.113.5"})) == "198.51.100.7"
    # through our proxy: the client typed "6.6.6.6, 127.0.0.1", the proxy appended the real address
    xff = {"X-Forwarded-For": "6.6.6.6, 127.0.0.1, 203.0.113.5"}
    assert client_ip_from_scope(_scope("127.0.0.1", xff)) == "203.0.113.5"
    # two proxy hops (10.x is ours too)
    assert client_ip_from_scope(_scope("10.1.2.3", {"X-Forwarded-For": "6.6.6.6, 203.0.113.5, 10.9.9.9"})) == "203.0.113.5"
    # only proxies in the chain: fall back to X-Real-IP set by the proxy, then the peer
    assert client_ip_from_scope(_scope("127.0.0.1", {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "203.0.113.8"})) == "203.0.113.8"
    assert client_ip_from_scope(_scope("127.0.0.1", {"X-Forwarded-For": "garbage"})) == "127.0.0.1"


async def test_spoofed_headers_neither_dodge_a_ban_nor_frame_someone_else(db, monkeypatch):
    from app.config import get_settings
    from app.security.bans import BanInfo, ban_registry

    s = get_settings()
    monkeypatch.setattr(s, "trust_proxy_headers", True)
    monkeypatch.setattr(s, "trusted_proxies", ["127.0.0.1"])
    monkeypatch.setattr(s, "rate_limit_whitelist", ["127.0.0.1"])
    banned = "203.0.113.9"
    ban_registry.put(banned, BanInfo(until=utcnow() + timedelta(hours=1), reason="test", severity=3))
    try:
        async with make_client(banned) as c:  # the banned client pretends to be someone else / localhost
            for spoof in ({"X-Forwarded-For": "198.51.100.1"}, {"X-Forwarded-For": "127.0.0.1"}, {"X-Real-IP": "127.0.0.1"}):
                assert (await c.get("/", headers=spoof)).status_code == 403, spoof
        async with make_client("198.51.100.7") as c:  # an innocent client can't be made to look like the banned one
            assert (await c.get("/", headers={"X-Forwarded-For": "8.8.8.8"})).status_code == 200
        async with make_client("127.0.0.1") as proxy:  # via the trusted proxy the real (banned) client is still blocked
            assert (await proxy.get("/", headers={"X-Forwarded-For": f"1.1.1.1, {banned}"})).status_code == 403
    finally:
        ban_registry.remove(banned)


async def test_forwarded_proto_from_a_direct_client_does_not_skip_https(monkeypatch):
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "force_https", True)
    monkeypatch.setattr(s, "trust_proxy_headers", True)
    monkeypatch.setattr(s, "trusted_proxies", ["127.0.0.1"])
    async with make_client("198.51.100.7") as c:
        r = await c.get("/about", headers={"X-Forwarded-Proto": "https"})
        assert r.status_code == 308 and r.headers["location"].startswith("https://")
    async with make_client("127.0.0.1") as proxy:
        assert (await proxy.get("/about", headers={"X-Forwarded-Proto": "https"})).status_code == 200


async def test_emailed_links_ignore_a_spoofed_host_header(db):
    from app.config import get_settings

    await seed_demo(db, password="Demo-Passw0rd!", gyms=["harbor-strength"], write_credentials=False)
    await db.commit()
    zoya = await _one(db, select(User).where(User.email == f"zoya.merchant@{DOMAIN}"))
    async with make_client() as c:
        r = await c.get("/auth/forgot", headers={"Host": "evil.example"})
        assert r.status_code == 400  # unknown hosts are refused outright
        token = await csrf_token(c)
        r = await c.post("/auth/forgot", data={"identifier": zoya.email, "csrf_token": token},
                         headers={"X-Forwarded-Host": "evil.example", "Forwarded": "host=evil.example"})
        assert r.status_code in (302, 303)
    mail = await _one(db, select(EmailOutbox).where(EmailOutbox.to_email == zoya.email).order_by(EmailOutbox.id.desc()))
    assert mail is not None and "evil.example" not in (mail.text_body + (mail.html_body or ""))
    assert get_settings().public_base_url in mail.text_body


async def test_sign_in_identifiers_cannot_be_impersonated(db):
    from app.services.identity_lookup import find_user_by_identifier, member_id, username_problem

    await seed_demo(db, password="Demo-Passw0rd!", gyms=["harbor-strength"], write_credentials=False)
    await db.commit()
    dev = await _one(db, select(User).where(User.email == f"dev.malhotra@{DOMAIN}"))
    zoya = await _one(db, select(User).where(User.email == f"zoya.merchant@{DOMAIN}"))
    # usernames can't look like another account's member ID or e-mail
    assert username_problem(member_id(dev)) and username_problem(member_id(dev).lower())
    assert username_problem(dev.email)
    # even a username equal to someone's phone digits never wins over the real phone number
    digits = re.sub(r"\D", "", dev.phone)[-10:]
    zoya.username = digits
    await db.commit()
    assert (await find_user_by_identifier(db, digits)).id == dev.id
    assert (await find_user_by_identifier(db, dev.phone)).id == dev.id
    assert (await find_user_by_identifier(db, member_id(dev))).id == dev.id
    assert (await find_user_by_identifier(db, dev.email.upper())).id == dev.id  # case-insensitive, same account


# ====================================================================================================== realtime
async def test_realtime_events_reach_only_the_thread_participants(db, monkeypatch):
    from app.services import chat as chat_service
    from app.services import realtime

    await seed_demo(db, password="Demo-Passw0rd!", gyms=["forge-and-flow", "harbor-strength"], write_credentials=False)
    await db.commit()
    sent: list[tuple[list[int], dict]] = []

    async def capture(user_ids: Any, evt: dict) -> None:
        sent.append((sorted(set(user_ids)), evt))

    monkeypatch.setattr(realtime.hub, "publish", capture)
    rohan = await _one(db, select(User).where(User.email == f"rohan@{DOMAIN}"))
    arjun = await _one(db, select(User).where(User.email == f"arjun@{DOMAIN}"))
    owner = await _one(db, select(User).where(User.email == f"owner@{DOMAIN}"))
    outsiders = {u.id for u in (await db.execute(select(User).where(User.email.in_(
        [f"zoya.merchant@{DOMAIN}", f"owner2@{DOMAIN}", f"farhan.qureshi@{DOMAIN}", f"aditya.rao@{DOMAIN}",
         f"kabir@{DOMAIN}"])))).scalars()}
    arjun_emp = await _one(db, select(Employee).where(Employee.user_id == arjun.id))
    thread = await chat_service.get_or_create_thread(db, gym_id=arjun_emp.gym_id, customer_id=rohan.id, employee_id=arjun_emp.id)
    await chat_service.post_message(db, thread=thread, sender=rohan, body=f"{M}-private", sender_role="customer")
    await db.commit()
    for _ in range(50):
        if sent:
            break
        await asyncio.sleep(0.02)
    chat_events = [(ids, e) for ids, e in sent if e.get("type") == "chat.message"]
    assert chat_events, "no realtime event was published"
    for ids, _ in chat_events:
        assert set(ids) <= {rohan.id, arjun.id, owner.id} and not (set(ids) & outsiders), ids

    # an outsider asking the typing relay about someone else's thread gets nothing published
    from app.routers.chat import ws_typing

    sent.clear()
    zoya = await _one(db, select(User).where(User.email == f"zoya.merchant@{DOMAIN}"))
    await ws_typing(zoya, {"thread_id": thread.id})
    assert not sent


# ====================================================================================================== AI caches
async def test_plan_answers_are_never_shared_between_members_with_different_details(db, fake_ai):
    from tests.factories import make_customer

    await seed_demo(db, password="Demo-Passw0rd!", gyms=["forge-and-flow"], write_credentials=False)
    await db.commit()

    def echo(**kw: Any) -> str:  # the "model" repeats the member facts it was given
        line = next((ln for ln in kw["prompt"].splitlines() if ln.startswith("Member goal")), "no member facts")
        return f"Answer for: {line}"

    fake_ai(handler=echo)
    teen = await _one(db, select(User).where(User.email == f"anaya@{DOMAIN}"))  # 15, peanut allergy
    adult = await make_customer(db, full_name="Adult Twin")
    adult_profile = await db.get(CustomerProfile, adult.id)
    teen_profile = await db.get(CustomerProfile, teen.id)
    if adult_profile is None:
        adult_profile = CustomerProfile(user_id=adult.id)
        db.add(adult_profile)
    adult_profile.goal = teen_profile.goal
    db.add(HealthCondition(customer_id=adult.id, kind=ConditionKind.ALLERGY, name="Peanut allergy", tag="food_allergy",
                           source="self"))
    adult.latitude, adult.longitude = teen.latitude, teen.longitude
    await db.commit()

    async def ask(user: User) -> str:
        async with make_client() as c:
            await login_as(c, user)
            token = await csrf_token(c)
            r = await c.post("/api/plans/ask", json={"question": "Which plan is best for me?", "gym_slug": "forge-and-flow"},
                             headers={"X-CSRF-Token": token})
            assert r.status_code == 200
            return r.text

    first = await ask(teen)
    assert "age: 15" in first
    second = await ask(adult)
    assert "age: 15" not in second, "one member's personalised answer (with their age) was served to another member"


async def test_product_answers_are_never_shared_between_members_with_different_details(db):
    from app.ai.shop import build_context, cache_key
    from tests.factories import make_customer

    await seed_demo(db, password="Demo-Passw0rd!", gyms=["forge-and-flow"], write_credentials=False)
    await db.commit()
    whey = await _one(db, select(Product).where(Product.sku == "PF-WHEY-ISO-1KG"))
    a, b = await make_customer(db, full_name="Member A"), await make_customer(db, full_name="Member B")
    db.add_all([HealthCondition(customer_id=a.id, kind=ConditionKind.ALLERGY, name="Severe dairy allergy - anaphylaxis 2019",
                                source="self"),
                HealthCondition(customer_id=b.id, kind=ConditionKind.ALLERGY, name="Lactose intolerance", source="self")])
    await db.commit()
    whey = await db.get(Product, whey.id)
    ctx_a, ctx_b = await build_context(db, whey, a), await build_context(db, whey, b)
    assert ctx_a.safety_context == ctx_b.safety_context  # same allergen key ("milk") - the old cache key collided
    question = "Can I take this after training?"
    assert cache_key(ctx_a, question) != cache_key(ctx_b, question)
    assert cache_key(ctx_a, question) == cache_key(await build_context(db, whey, a), question)  # still cacheable


# ====================================================================================================== concurrency
async def test_concurrent_requests_from_many_accounts_never_mix_identities(db):
    await seed_demo(db, password="Demo-Passw0rd!", gyms=["forge-and-flow", "harbor-strength"], write_credentials=False)
    await db.commit()
    users = (await db.execute(select(User).where(User.email.like(f"%@{DOMAIN}"), User.role == Role.CUSTOMER))).scalars().all()
    assert len(users) >= 6

    async def browse(user: User) -> list[str]:
        problems = []
        async with make_client() as c:
            await login_as(c, user)
            for path in ("/account", "/me", "/account", "/me/health", "/account"):
                r = await c.get(path)
                if r.status_code != 200:
                    continue
                if path == "/account" and user.email not in r.text:
                    problems.append(f"{user.email} did not see their own account on {path}")
                others = [u.email for u in users if u.id != user.id and u.email in r.text]
                if others:
                    problems.append(f"{user.email} saw {others} on {path}")
        return problems

    results = await asyncio.gather(*(browse(u) for u in users))
    problems = [p for r in results for p in r]
    assert not problems, "\n".join(problems)
