"""Load the demo dataset into PostgreSQL:  python scripts/seed_demo.py   (or: python scripts/manage.py seed)

The demo gyms, accounts, shop products, orders and so on are *data* in ``scripts/demo/dataset.json``. This loader
writes them to the database through the app's own models and services. Nothing under ``app/`` knows about them:
once loaded they are ordinary rows that every page queries like any other data, and owners/admins can edit, hide
or delete them in the UI (``tests/test_demo_data.py`` guards both properties).

Idempotent and additive: rows are matched on natural keys - user e-mail, gym slug, branch name, (gym, user) for
staff, product SKU, order reference, coupon code, job title, (gym, member, staff) for chat threads - and only
missing ones are created. Re-running after editing dataset.json adds the new entries and never overwrites
anything that already exists (including edits made in the app). Gym-level history - past income, notifications,
misconduct reports - is only created together with a new gym; member history (profile, conditions, metrics,
tasks, alerts) only together with a new member.
"""

from __future__ import annotations

import json
import logging
import random
import re
import secrets
import zlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.utils import bmi, normalize_phone, utcnow
from app.models import (
    Branch,
    BranchDocument,
    BranchFeature,
    BranchHours,
    BranchPhoto,
    ChatMessage,
    ChatThread,
    Coupon,
    CustomerProfile,
    Employee,
    Gym,
    HealthAlert,
    HealthCondition,
    IdentityDocument,
    InventoryMovement,
    JobEnquiry,
    JobPosting,
    MetricEntry,
    MisconductReport,
    Notification,
    Plan,
    Product,
    ProductImage,
    ProductReview,
    Refund,
    ShopOrder,
    ShopOrderItem,
    Subscription,
    SuspensionEvent,
    Task,
    TrainerClient,
    TrainerShift,
    Transaction,
    User,
    VerificationRun,
)
from app.models.enums import (
    AnomalyLevel,
    BranchDocType,
    ConditionKind,
    EmployeeRole,
    EmploymentStatus,
    EmploymentType,
    FitnessLevel,
    Goal,
    IdDocType,
    IdStatus,
    NotificationCategory,
    PhotoKind,
    PlanMode,
    PurchaseChannel,
    Role,
    Severity,
    ShiftMode,
    SubscriptionStatus,
    TaskKind,
    TxCategory,
    TxDirection,
    TxStatus,
    VerificationStatus,
    WorkMode,
)
from app.models.shop import OrderStatus, ProductCategory, RefundStatus
from app.security.passwords import hash_password
from app.services import storage
from app.services.conditions import normalize_condition
from scripts.demo import art

log = logging.getLogger("ironpulse.demo")
DATASET_PATH = Path(__file__).with_name("dataset.json")
DEMO_DOMAIN = "demo.ironpulse.local"  # default; the dataset's "email_domain" wins
CREDENTIALS_FILE = "demo_credentials.txt"
COUNTRY_NAMES = {"IN": "India", "AE": "United Arab Emirates", "PT": "Portugal", "GB": "United Kingdom"}
ZERO = Decimal("0")
DELIVERED = {OrderStatus.COMPLETED, OrderStatus.REFUND_REQUESTED, OrderStatus.PARTIALLY_REFUNDED, OrderStatus.REFUNDED}


class DatasetError(ValueError):
    """dataset.json references something it doesn't define (a branch, plan, person or product)."""


def load_dataset(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or DATASET_PATH).read_text(encoding="utf-8"))


def saved_password() -> str | None:
    """The password of the demo accounts already in the database (from var/demo_credentials.txt), if any."""
    try:
        text = (get_settings().var_dir / CREDENTIALS_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^password:\s*(\S+)\s*$", text, re.MULTILINE)
    return match.group(1) if match else None


def _idx(seed: str) -> int:
    return zlib.crc32(seed.encode()) % 997


def _t(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "branch"


def _dob(spec: dict[str, Any], today: date) -> date:
    if spec.get("dob"):
        return date.fromisoformat(spec["dob"])
    month, day = (int(x) for x in str(spec.get("birthday", "01-01")).split("-"))
    year = today.year - int(spec["age"]) - (1 if (month, day) > (today.month, today.day) else 0)
    return date(year, month, day)


def _price(raw: Any, factor: Decimal, currency: str) -> Decimal | None:
    if raw in (None, ""):
        return None
    value = Decimal(str(raw)) * factor
    if currency.upper() == "INR" or factor == 1:
        return value.quantize(Decimal("0.01"))
    return max(Decimal("1"), value.to_integral_value()) - Decimal("0.01")  # 131.956 -> 131.99


@dataclass
class Loader:
    db: AsyncSession
    data: dict[str, Any]
    pwd_hash: str
    domain: str
    today: date = field(default_factory=date.today)
    stats: Counter = field(default_factory=Counter)
    users: dict[str, User] = field(default_factory=dict)  # dataset key -> user
    new_user_ids: set[int] = field(default_factory=set)
    branches: dict[tuple[str, str], Branch] = field(default_factory=dict)  # (gym slug, branch name)
    plans: dict[tuple[str, str], Plan] = field(default_factory=dict)  # (gym slug, plan code)
    employees: dict[tuple[str, str], Employee] = field(default_factory=dict)  # (gym slug, staff key)
    products: dict[tuple[str, str], Product] = field(default_factory=dict)  # (gym slug, catalogue key)
    deferred: list[tuple[User, dict[str, Any]]] = field(default_factory=list)  # subscriptions to gyms loaded later
    accounts: list[tuple[str, str, str, str]] = field(default_factory=list)  # (gym, role label, name, e-mail)
    art_cache: dict[tuple[str, int], bytes] = field(default_factory=dict)

    # ------------------------------------------------------------------------------------------- lookups
    def email(self, key: str) -> str:
        return key if "@" in key else f"{key}@{self.domain}"

    def person_for(self, key: str) -> User:
        try:
            return self.users[key]
        except KeyError:
            raise DatasetError(f"unknown person '{key}' - define them in the same gym first") from None

    def _get(self, table: dict, key: tuple[str, str], what: str) -> Any:
        try:
            return table[key]
        except KeyError:
            raise DatasetError(f"{key[0]}: unknown {what} '{key[1]}'") from None

    def note_account(self, gym_name: str, label: str, user: User) -> None:
        if all(email != user.email for *_, email in self.accounts):
            self.accounts.append((gym_name, label, user.full_name, user.email))

    # ------------------------------------------------------------------------------------------- people
    async def person(self, spec: dict[str, Any], role: Role, *, country: str, tz: str, city: str | None,
                     photo: bool = True) -> User:
        email = self.email(spec["key"])
        user = await self.db.scalar(select(User).where(User.email == email))
        if user is None:
            country = spec.get("country", country)
            dob = _dob(spec, self.today)
            phone = spec.get("phone")
            try:
                phone_norm = normalize_phone(phone, country)
            except ValueError:
                phone_norm = None
            if phone_norm and await self.db.scalar(select(User.id).where(User.phone_normalized == phone_norm)):
                log.warning("phone %s already belongs to another account - %s is created without it", phone, email)
                phone_norm = None
            lat, lon = spec.get("home") or (None, None)
            user = User(email=email, full_name=spec["name"], role=role, password_hash=self.pwd_hash, date_of_birth=dob,
                        phone=phone, phone_normalized=phone_norm, email_verified_at=utcnow(), id_status=IdStatus.VERIFIED,
                        timezone=spec.get("timezone", tz), country_code=country, city=spec.get("city", city),
                        latitude=lat, longitude=lon, location_updated_at=utcnow() if lat is not None else None)
            if spec.get("photo", photo):
                stored = await storage.save_public_image(art.portrait_png(spec["name"], _idx(email)), kind="avatars", max_side=512)
                user.photo_key = stored.key
            self.db.add(user)
            await self.db.flush()
            doc, _ = await storage.save_private_file(art.specimen_id_png(spec["name"], dob, f"DEMO{user.id:08d}", country),
                                                     kind="identity", allow_pdf=False)
            self.db.add(IdentityDocument(
                user_id=user.id, doc_type=IdDocType.NATIONAL_ID, country_code=country, front_key=doc.key,
                status=IdStatus.VERIFIED, id_number_masked=f"••••{user.id:04d}", name_on_id=user.full_name, dob_on_id=dob,
                engines=["seed"], checks=[{"check": "Demo document", "passed": True, "detail": "Seeded specimen - verification skipped"}],
                consensus_score=1.0, ai_summary="Demo identity document created by the demo data loader.",
                verified_at=utcnow(), uploaded_by_id=user.id))
            self.new_user_ids.add(user.id)
            self.stats["accounts"] += 1
        self.users[spec["key"]] = user
        return user

    # ------------------------------------------------------------------------------------------- gym
    async def load_gym(self, g: dict[str, Any]) -> None:
        owner = await self.person(g["owner"], Role.OWNER, country=g["country"], tz=g["timezone"], city=g["owner"].get("city"))
        gym = await self.db.scalar(select(Gym).where(Gym.slug == g["slug"]))
        new = gym is None
        if new:
            gym = Gym(owner_id=owner.id, name=g["name"], slug=g["slug"], country_code=g["country"], currency=g["currency"],
                      tagline=g.get("tagline"), description=g.get("description"),
                      contact_email=self.email(g["contact_email"]) if g.get("contact_email") else None,
                      contact_phone=g.get("contact_phone"), website=g.get("website"),
                      online_enabled=g.get("online", True), online_open=True, offline_enabled=g.get("offline", True))
            self.db.add(gym)
            await self.db.flush()
            self.stats["gyms"] += 1
        if g.get("logo") and not gym.logo_key:
            gym.logo_key = (await storage.save_public_image(art.logo_png(gym.name, _idx(gym.slug)), kind="logos",
                                                            max_side=512)).key
        self.note_account(gym.name, "Owner", owner)

        for i, b in enumerate(g.get("branches", [])):
            await self.branch(gym, g, b, i)
        await self.load_plans(gym, g, new)
        for s in g.get("staff", []):
            await self.staff(gym, g, s)
        for m in g.get("members", []):
            await self.member(gym, g, m)
        await self.shop(gym, g)
        await self.chats(gym, g)
        await self.jobs(gym, g)
        if new:
            await self.gym_history(gym, g)
        await self.db.flush()

    async def branch(self, gym: Gym, g: dict[str, Any], b: dict[str, Any], i: int) -> Branch:
        branch = await self.db.scalar(select(Branch).where(Branch.gym_id == gym.id, Branch.name == b["name"]))
        if branch is None:
            verified = b.get("verified", True)
            summary = ("All documents matched the registration details; address confirmed by geocoding (42 m from the pin). "
                       "Fire NOC and liability insurance valid.") if verified else None
            branch = Branch(
                gym_id=gym.id, name=b["name"], address_line1=b["address"], city=b["city"], state=b.get("state"),
                postal_code=b.get("postal"), country_code=g["country"], timezone=g["timezone"], latitude=b["lat"],
                longitude=b["lon"], geocoded_latitude=b["lat"] + 0.0004, geocoded_longitude=b["lon"] - 0.0003,
                formatted_address=f"{b['address']}, {b['city']}, {COUNTRY_NAMES.get(g['country'], g['country'])}",
                registration_number=b.get("registration"), phone=b.get("phone"),
                email=f"{_slug(b['name'])}.{gym.slug}@{self.domain}",
                verification_status=VerificationStatus.VERIFIED if verified else VerificationStatus.DRAFT,
                verified_at=utcnow() - timedelta(days=40) if verified else None, verification_summary=summary)
            self.db.add(branch)
            await self.db.flush()
            hours = b.get("hours") or self.data["default_hours"]
            for wd in range(7):
                opens, closes = hours["sun"] if wd == 6 else hours["mon_sat"]
                self.db.add(BranchHours(branch_id=branch.id, weekday=wd, opens_at=_t(opens), closes_at=_t(closes)))
            for name in b.get("features") or [*self.data["base_features"], *b.get("extra_features", [])]:
                self.db.add(BranchFeature(branch_id=branch.id, name=name))
            for p in range(b.get("photos", 3)):
                kind = PhotoKind.MACHINES if p % 2 == 0 else PhotoKind.OVERALL
                png = art.gym_photo_png(kind.value, _idx(gym.slug) + i * 3 + p, f"{b['name']} - {kind.value}")
                stored = await storage.save_public_image(png, kind="branch_photos")
                self.db.add(BranchPhoto(branch_id=branch.id, image_key=stored.key, caption=f"{b['name']} {kind.value}", kind=kind,
                                        width=stored.width, height=stored.height, sort_order=p))
            for doc_type in b.get("documents", []):
                png = art.gym_photo_png("overall", _idx(doc_type), f"{doc_type.replace('_', ' ').title()} (demo)")
                stored, _ = await storage.save_private_file(png, kind="branch_docs")
                self.db.add(BranchDocument(branch_id=branch.id, doc_type=BranchDocType(doc_type), file_key=stored.key,
                                           original_filename=f"{doc_type}-demo.png", content_type=stored.content_type,
                                           sha256=stored.sha256, size_bytes=stored.size))
            if verified:
                self.db.add(VerificationRun(
                    branch_id=branch.id, outcome=VerificationStatus.VERIFIED, score=0.93, ai_provider="seed", summary=summary,
                    checks=[{"check": "Registration number format", "passed": True}, {"check": "Name on certificate", "passed": True},
                            {"check": "Address vs map pin (42 m)", "passed": True},
                            {"check": "Fire safety certificate valid", "passed": True}]))
            self.stats["branches"] += 1
        self.branches[(gym.slug, b["name"])] = branch
        return branch

    async def load_plans(self, gym: Gym, g: dict[str, Any], new_gym: bool) -> None:
        from app.services.checkout import seed_default_plans

        rows = await seed_default_plans(self.db, gym)  # adds only missing plan codes
        await self.db.flush()
        disabled = set(g.get("disabled_plans", []))
        for plan in rows:
            if new_gym and plan.code in disabled:
                plan.is_enabled = False
            self.plans[(gym.slug, plan.code)] = plan

    async def staff(self, gym: Gym, g: dict[str, Any], s: dict[str, Any]) -> None:
        user = await self.person(s, Role.STAFF, country=g["country"], tz=g["timezone"], city=s.get("city") or g["owner"].get("city"))
        emp = await self.db.scalar(select(Employee).where(Employee.gym_id == gym.id, Employee.user_id == user.id))
        if emp is None:
            role = EmployeeRole(s["role"])
            hire_days = int(s.get("hire_days", 365))
            emp = Employee(
                gym_id=gym.id, user_id=user.id, invite_email=user.email, full_name=user.full_name, phone=user.phone, role=role,
                role_title=s["title"], is_trainer=role == EmployeeRole.TRAINER, work_mode=WorkMode(s["mode"]),
                employment_type=EmploymentType(s.get("type", "full_time")), country_code=s.get("country", g["country"]),
                currency=s.get("currency", g["currency"]), monthly_salary=Decimal(s["salary"]),
                hire_date=self.today - timedelta(days=hire_days), status=EmploymentStatus.ACTIVE, photo_key=user.photo_key or "",
                bio=s.get("bio", ""), specialties=list(s.get("specialties", [])), years_experience=max(1, hire_days // 365 + 2),
                verification_status=VerificationStatus.VERIFIED, verified_at=utcnow(), id_status=IdStatus.VERIFIED)
            self.db.add(emp)
            await self.db.flush()
            for shift in s.get("shifts", []):
                branch = None if shift.get("online") else self._get(self.branches, (gym.slug, shift["branch"]), "branch")
                for wd in shift["days"]:
                    self.db.add(TrainerShift(employee_id=emp.id, branch_id=branch.id if branch else None,
                                             mode=ShiftMode.OFFLINE if branch else ShiftMode.ONLINE, weekday=wd,
                                             start_time=_t(shift["from"]), end_time=_t(shift["to"])))
            self.stats["staff"] += 1
        self.employees[(gym.slug, s["key"])] = emp
        self.note_account(gym.name, f"Staff - {s['title']}", user)

    # ------------------------------------------------------------------------------------------- members
    async def member(self, gym: Gym, g: dict[str, Any], m: dict[str, Any]) -> None:
        home_branch = next((s["branch"] for s in m.get("subscriptions", []) if s.get("branch") and not s.get("gym")), None)
        city = m.get("city") or (self._get(self.branches, (gym.slug, home_branch), "branch").city if home_branch
                                 else g["owner"].get("city"))
        user = await self.person(m, Role.CUSTOMER, country=g["country"], tz=g["timezone"], city=city)
        if user.id in self.new_user_ids:
            await self.member_history(user, m)
            self.stats["members"] += 1
        for sub in m.get("subscriptions", []):
            if sub.get("gym") and sub["gym"] != g["slug"]:
                self.deferred.append((user, sub))
            else:
                await self.subscribe(user, gym, sub)
        guardian = m.get("guardian")
        self.note_account(gym.name, f"Member - minor (guardian {guardian['name']})" if guardian else "Member", user)

    async def member_history(self, user: User, m: dict[str, Any]) -> None:
        db = self.db
        profile = CustomerProfile(user_id=user.id, goal=Goal(m["goal"]), fitness_level=FitnessLevel(m["level"]),
                                  height_cm=m["height"], weight_kg=m["weight"], wake_time=time(6, 30), sleep_reminders_enabled=True)
        if guardian := m.get("guardian"):
            profile.guardian_name, profile.guardian_phone = guardian["name"], guardian["phone"]
            profile.guardian_email, profile.guardian_relation = self.email(guardian["email"]), guardian.get("relation")
        db.add(profile)
        tags = []
        for kind, name in m.get("conditions", []):
            tag = normalize_condition(name)
            tags.append(tag)
            db.add(HealthCondition(customer_id=user.id, kind=ConditionKind(kind), name=name, tag=tag, source="self"))
        await db.flush()

        # 21 days of daily height/weight (ending yesterday, so today's entry is still open and prefilled)
        height, end = float(m["height"]), float(m["weight"])
        start = float(m.get("start_weight", end))
        drop = m.get("drop") or {}
        rng = random.Random(user.email)
        days = 21
        for i in range(days):
            day = self.today - timedelta(days=days - i)
            if i == days - 1:
                weight = end
            elif drop and i >= days - 1 - int(drop["days"]):  # sudden drop over the last N days
                left = days - 1 - i
                weight = end + float(drop["kg"]) * left / int(drop["days"])
            else:
                plateau_end = end + float(drop.get("kg", 0))
                span = days - 1 - int(drop.get("days", 0))
                weight = start + (plateau_end - start) * i / max(1, span) + rng.uniform(-0.2, 0.2)
            weight = round(weight, 1)
            db.add(MetricEntry(customer_id=user.id, recorded_on=day, height_cm=height, weight_kg=weight,
                               bmi=bmi(height, weight), source="manual", confirmed=True))

        for task in self.data.get("member_tasks", []):
            db.add(Task(user_id=user.id, title=task["title"], kind=TaskKind(task["kind"]),
                        due_at=utcnow() + timedelta(hours=task["due_in_hours"]) if "due_in_hours" in task else None,
                        remind_at=utcnow() + timedelta(hours=task["remind_in_hours"]) if "remind_in_hours" in task else None))
        for a in m.get("alerts", []):
            db.add(HealthAlert(customer_id=user.id, level=AnomalyLevel(a["level"]), title=a["title"], message=a["message"],
                               recommendation=a.get("recommendation"), source="rule"))
        if suspension := m.get("suspension"):
            a = suspension["alert"]
            alert = HealthAlert(customer_id=user.id, level=AnomalyLevel(a["level"]), title=a["title"], message=a["message"],
                                recommendation=a.get("recommendation"), source="rule")
            db.add(alert)
            await db.flush()
            profile.activity_suspended, profile.suspension_reason = True, suspension["reason"]
            profile.suspended_at, profile.suspended_by = utcnow() - timedelta(hours=20), "rule"
            db.add(SuspensionEvent(customer_id=user.id, action="suspend", reason=suspension["reason"], actor="rule",
                                   health_alert_id=alert.id))
        try:
            from app.services.health import ac_recommendation

            profile.ac_recommendation = ac_recommendation([t for t in tags if t], user.age())
        except Exception as exc:  # noqa: BLE001 - optional nicety
            log.info("AC recommendation skipped for %s: %s", user.email, exc)

    async def subscribe(self, user: User, gym: Gym, sub: dict[str, Any]) -> None:
        plan = self._get(self.plans, (gym.slug, sub["plan"]), "plan")
        if await self.db.scalar(select(Subscription.id).where(Subscription.customer_id == user.id, Subscription.plan_id == plan.id)):
            return
        branch = self._get(self.branches, (gym.slug, sub["branch"]), "branch") if sub.get("branch") else None
        days = int(sub.get("days_ago", 7))
        starts = self.today - timedelta(days=days)
        when = utcnow() - timedelta(days=days)
        channel = PurchaseChannel(sub.get("channel") or ("branch_desk_otp" if branch else "online_checkout"))
        s = Subscription(customer_id=user.id, gym_id=gym.id, plan_id=plan.id, branch_id=branch.id if branch else None,
                         status=SubscriptionStatus.ACTIVE, channel=channel, price=plan.price, currency=plan.currency,
                         starts_on=starts, ends_on=starts + timedelta(days=plan.duration_days or 30), activated_at=when,
                         physically_present=True if branch else None, otp_verified_at=when if branch else None)
        self.db.add(s)
        await self.db.flush()
        self.db.add(Transaction(gym_id=gym.id, direction=TxDirection.INCOMING, category=TxCategory.SUBSCRIPTION,
                                status=TxStatus.SUCCESS, amount=plan.price, currency=plan.currency,
                                description=f"{plan.name} - {user.full_name}", party_label=user.full_name, provider="test",
                                provider_order_id=f"SEED-{s.id}-{secrets.token_hex(3)}", customer_id=user.id,
                                subscription_id=s.id, completed_at=when, created_at=when))
        if sub.get("trainer"):
            emp = self._get(self.employees, (gym.slug, sub["trainer"]), "trainer")
            s.trainer_id = emp.id
            self.db.add(TrainerClient(employee_id=emp.id, customer_id=user.id, subscription_id=s.id,
                                      mode=ShiftMode.ONLINE if plan.mode == PlanMode.ONLINE else ShiftMode.OFFLINE))
        self.stats["subscriptions"] += 1

    async def load_deferred(self, gyms: dict[str, Gym]) -> None:
        for user, sub in self.deferred:
            gym = gyms.get(sub["gym"])
            if gym is None:  # that gym wasn't part of this run (``gyms=`` filter)
                continue
            await self.subscribe(user, gym, sub)

    # ------------------------------------------------------------------------------------------- shop
    async def shop(self, gym: Gym, g: dict[str, Any]) -> None:
        from app.services.shop import pickup_branches, refresh_embedding, unique_product_slug

        spec = g.get("shop") or {}
        factor = Decimal(str(g.get("price_factor", 1)))
        catalogue = self.data["catalogue"]
        pickup = await pickup_branches(self.db, gym.id)
        net_out = Counter()  # units the dataset's orders take out of stock (so the final stock matches the dataset)
        for order in spec.get("orders", []):
            if OrderStatus(order["status"]) in DELIVERED | {OrderStatus.PAID, OrderStatus.PROCESSING,
                                                           OrderStatus.READY_FOR_PICKUP, OrderStatus.SHIPPED}:
                for key, qty in order["items"]:
                    net_out[key] += qty
                refund = order.get("refund") or {}
                if refund.get("status") == "completed" and refund.get("restock"):
                    for key, qty in refund["items"]:
                        net_out[key] -= qty
        for key in spec.get("products", []):
            c = catalogue.get(key)
            if c is None:
                raise DatasetError(f"{gym.slug}: unknown catalogue product '{key}'")
            product = await self.db.scalar(select(Product).where(Product.gym_id == gym.id, Product.sku == c["sku"]))
            if product is None:
                stock = int(spec.get("stock", {}).get(key, c["stock"])) + net_out[key]
                product = Product(
                    gym_id=gym.id, name=c["name"], brand=c["brand"], category=ProductCategory(c["category"]),
                    description=c["description"], price=_price(c["price"], factor, gym.currency),
                    compare_at_price=_price(c.get("mrp"), factor, gym.currency), currency=gym.currency,
                    tax_rate=Decimal(c["tax"]), stock=stock, low_stock_threshold=c["threshold"], sku=c["sku"], is_active=True,
                    is_supplement=c["supplement"], age_restricted=c["age_restricted"], allergens=list(c["allergens"]),
                    caution_tags=list(c["cautions"]), ingredients=c.get("ingredients"), nutrition_facts=c.get("nutrition"),
                    fulfillment=c["fulfillment"] if pickup or c["fulfillment"] == "delivery" else "delivery",
                    slug=await unique_product_slug(self.db, gym.id, c["name"]))
                refresh_embedding(product)
                self.db.add(product)
                await self.db.flush()
                if stock:
                    self.db.add(InventoryMovement(product_id=product.id, change=stock, reason="restock", actor_id=gym.owner_id,
                                                  note="Opening stock"))
                for variant in range(int(c["art"].get("images", 1))):
                    png = self.art_cache.get((key, variant))
                    if png is None:
                        png = self.art_cache[(key, variant)] = art.product_png(c["art"], c["brand"], c.get("nutrition"), variant)
                    stored = await storage.save_public_image(png, kind="products", max_side=1200)
                    self.db.add(ProductImage(product_id=product.id, image_key=stored.key, alt=product.name, sort_order=variant))
                self.stats["products"] += 1
            self.products[(gym.slug, key)] = product

        for cp in spec.get("coupons", []):
            if await self.db.scalar(select(Coupon.id).where(Coupon.gym_id == gym.id, Coupon.code == cp["code"])):
                continue
            self.db.add(Coupon(gym_id=gym.id, code=cp["code"], percent_off=cp.get("percent_off"),
                               amount_off=Decimal(cp["amount_off"]) if cp.get("amount_off") else None,
                               min_order_total=Decimal(cp["min_order"]) if cp.get("min_order") else None,
                               valid_until=utcnow() + timedelta(days=cp["valid_days"]) if cp.get("valid_days") else None,
                               max_uses=cp.get("max_uses"), is_active=True))
            self.stats["coupons"] += 1
        await self.db.flush()
        for order in spec.get("orders", []):
            await self.order(gym, order)

    async def order(self, gym: Gym, o: dict[str, Any]) -> None:
        from app.services.shop import PAID_STATUSES, build_quote, customer_label, delivery_fee, new_order_number
        from app.services.shop_payments import _sale_tx

        db = self.db
        ref = f"DEMO-{o['ref']}"
        if await db.scalar(select(ShopOrder.id).where(ShopOrder.provider_order_id == ref)):
            return
        member = self.person_for(o["member"])
        lines = [(self._get(self.products, (gym.slug, key), "product"), int(qty)) for key, qty in o["items"]]
        pickup = o["fulfillment"] == "pickup"
        subtotal = sum((p.price * q for p, q in lines), ZERO)
        quote = build_quote([(p.id, p.name, p.price, p.tax_rate, q) for p, q in lines], gym.currency,
                            shipping=ZERO if pickup else delivery_fee(gym.currency, subtotal))
        status = OrderStatus(o["status"])
        paid = status in PAID_STATUSES
        now = utcnow()
        placed = now - timedelta(days=int(o["days_ago"]), hours=_idx(ref) % 9 + 1)
        address = None
        if not pickup:
            a = o["address"]
            address = {"name": member.full_name, "phone": member.phone or "", "line1": a["line1"], "city": a["city"],
                       "postal_code": a["postal_code"], "country_code": gym.country_code}
        order = ShopOrder(
            order_number=await new_order_number(db), gym_id=gym.id, customer_id=member.id, customer_label=customer_label(member),
            status=status, fulfillment=o["fulfillment"],
            pickup_branch_id=self._get(self.branches, (gym.slug, o["branch"]), "branch").id if pickup else None,
            shipping_address=address, subtotal=quote.subtotal, tax_total=quote.tax_total, shipping_fee=quote.shipping_fee,
            discount_total=quote.discount_total, total=quote.total, currency=gym.currency, payment_provider="test",
            provider_order_id=ref, paid_at=placed + timedelta(minutes=2) if paid else None,
            completed_at=min(placed + timedelta(days=2 if pickup else 3), now - timedelta(hours=1)) if status in DELIVERED else None,
            created_at=placed)
        db.add(order)
        await db.flush()
        items: dict[int, ShopOrderItem] = {}
        for (product, qty), ql in zip(lines, quote.lines):
            brand = product.brand or ""
            name = f"{brand} {product.name}" if brand and not product.name.lower().startswith(brand.lower()) else product.name
            items[product.id] = ShopOrderItem(order_id=order.id, product_id=product.id, product_name=name[:160],
                                              unit_price=ql.unit_price, tax_rate=ql.tax_rate, quantity=qty, line_total=ql.total)
            db.add(items[product.id])
        await db.flush()
        if paid:
            sale = _sale_tx(order, gym.name, f"DEMO-PAY-{o['ref']}")
            sale.completed_at = sale.created_at = order.paid_at
            db.add(sale)
            for product, qty in lines:
                product.stock -= qty
                db.add(InventoryMovement(product_id=product.id, change=-qty, reason="sale", order_id=order.id,
                                         created_at=order.paid_at))
        if r := o.get("refund"):
            await self.refund(gym, order, member, items, r)
        for rv in o.get("reviews", []):
            product = self._get(self.products, (gym.slug, rv["product"]), "product")
            if await db.scalar(select(ProductReview.id).where(ProductReview.product_id == product.id,
                                                              ProductReview.user_id == member.id)):
                continue
            written = min((order.completed_at or placed) + timedelta(days=1), now - timedelta(minutes=30))
            db.add(ProductReview(product_id=product.id, user_id=member.id, rating=int(rv["rating"]), title=rv.get("title"),
                                 body=rv.get("body"), created_at=written))
            self.stats["reviews"] += 1
        self.stats["orders"] += 1

    async def refund(self, gym: Gym, order: ShopOrder, member: User, items: dict[int, ShopOrderItem], r: dict[str, Any]) -> None:
        from app.services.shop import unit_refund_amount

        db = self.db
        picked: list[tuple[ShopOrderItem, Product, int]] = []
        amount = ZERO
        for key, qty in r["items"]:
            product = self._get(self.products, (gym.slug, key), "product")
            item = items.get(product.id)
            if item is None:
                raise DatasetError(f"{gym.slug}: refund for '{key}', which is not in order {order.provider_order_id}")
            amount += unit_refund_amount(item, int(qty))
            picked.append((item, product, int(qty)))
        done = r.get("status") == "completed"
        when = utcnow() - timedelta(days=int(r.get("days_ago", 1)))
        refund = Refund(order_id=order.id, amount=amount, reason=r["reason"], restock=bool(r.get("restock")),
                        items=[{"order_item_id": item.id, "quantity": qty} for item, _, qty in picked],
                        status=RefundStatus.COMPLETED if done else RefundStatus.REQUESTED, requested_by_id=member.id,
                        decided_by_id=gym.owner_id if done else None,
                        decision_note="Approved - refunded to the original payment method." if done else None,
                        provider_refund_id=f"DEMO-RF-{order.provider_order_id}" if done else None,
                        processed_at=when if done else None, created_at=when - timedelta(hours=6) if done else when)
        db.add(refund)
        await db.flush()
        if not done:
            return
        for item, product, qty in picked:
            item.refunded_quantity += qty
            if refund.restock:
                product.stock += qty
                db.add(InventoryMovement(product_id=product.id, change=qty, reason="refund_restock", order_id=order.id,
                                         actor_id=gym.owner_id, note=f"Refund {refund.id}", created_at=when))
        order.refunded_total = (order.refunded_total or ZERO) + amount
        tx = Transaction(gym_id=gym.id, direction=TxDirection.OUTGOING, category=TxCategory.REFUND, status=TxStatus.SUCCESS,
                         amount=amount, currency=order.currency, description=f"Refund for shop order {order.order_number}",
                         party_label=order.customer_label, provider="test", provider_reference=refund.provider_refund_id,
                         customer_id=member.id, created_by_id=gym.owner_id, completed_at=when, created_at=when,
                         meta={"shop_order_id": order.id, "order_number": order.order_number, "refund_id": refund.id,
                               "kind": "shop_refund", "simulated": True})
        db.add(tx)
        await db.flush()
        refund.transaction_id = tx.id
        self.stats["refunds"] += 1

    # ------------------------------------------------------------------------------------------- engagement
    async def chats(self, gym: Gym, g: dict[str, Any]) -> None:
        owner = await self.db.get(User, gym.owner_id)
        for c in g.get("chats", []):
            member = self.person_for(c["member"])
            emp = self._get(self.employees, (gym.slug, c["staff"]), "staff member") if c.get("staff") else None
            exists = await self.db.scalar(select(ChatThread.id).where(
                ChatThread.gym_id == gym.id, ChatThread.customer_id == member.id,
                ChatThread.employee_id == emp.id if emp else ChatThread.employee_id.is_(None)))
            if exists:
                continue
            last = c["messages"][-1]
            thread = ChatThread(gym_id=gym.id, customer_id=member.id, employee_id=emp.id if emp else None, subject=c["subject"],
                                last_message_at=utcnow() - timedelta(minutes=last["minutes_ago"]),
                                last_message_preview=last["body"][:140], customer_unread=c.get("customer_unread", 0),
                                staff_unread=c.get("staff_unread", 0))
            self.db.add(thread)
            await self.db.flush()
            staff_user = self.person_for(c["staff"]) if c.get("staff") else None
            for msg in c["messages"]:
                sender, role = {"member": (member, "customer"), "staff": (staff_user, "staff"), "owner": (owner, "owner")}[msg["from"]]
                if sender is None:
                    raise DatasetError(f"{gym.slug}: chat '{c['subject']}' has a staff message but no staff member")
                self.db.add(ChatMessage(thread_id=thread.id, sender_id=sender.id, sender_role=role, body=msg["body"],
                                        created_at=utcnow() - timedelta(minutes=msg["minutes_ago"])))
            self.stats["chats"] += 1

    async def jobs(self, gym: Gym, g: dict[str, Any]) -> None:
        for j in g.get("jobs", []):
            if await self.db.scalar(select(JobPosting.id).where(JobPosting.gym_id == gym.id, JobPosting.title == j["title"])):
                continue
            branch = self._get(self.branches, (gym.slug, j["branch"]), "branch") if j.get("branch") else None
            job = JobPosting(gym_id=gym.id, branch_id=branch.id if branch else None, title=j["title"], role=EmployeeRole(j["role"]),
                             employment_type=EmploymentType(j["type"]), location=j.get("location"),
                             salary_min=Decimal(j["salary"][0]), salary_max=Decimal(j["salary"][1]),
                             currency=j.get("currency", gym.currency), requirements=j.get("requirements"),
                             description=j.get("description"))
            self.db.add(job)
            await self.db.flush()
            for e in j.get("enquiries", []):
                self.db.add(JobEnquiry(job_id=job.id, name=e["name"], email=self.email(e["email"]), phone=e.get("phone"),
                                       message=e["message"]))
            self.stats["jobs"] += 1

    async def gym_history(self, gym: Gym, g: dict[str, Any]) -> None:
        """Past income, misconduct reports and notifications - only created together with a new gym."""
        db = self.db
        for r in g.get("misconduct", []):
            emp = self._get(self.employees, (gym.slug, r["employee"]), "staff member")
            db.add(MisconductReport(gym_id=gym.id, employee_id=emp.id, reporter_id=self.person_for(r["reporter"]).id,
                                    category=r["category"], description=r["description"],
                                    incident_date=self.today - timedelta(days=int(r.get("days_ago", 1)))))
        for n in g.get("notifications", []):
            db.add(Notification(user_id=self.person_for(n["to"]).id, title=n["title"], body=n["body"],
                                category=NotificationCategory.SYSTEM, severity=Severity.INFO))
        plans = [p for (slug, _), p in self.plans.items() if slug == gym.slug and p.is_enabled]
        rng = random.Random(f"income:{gym.slug}")
        for month in range(1, int(g.get("income_history_months", 0)) + 1 if plans else 1):
            for k in range(rng.randint(3, 8)):
                plan = rng.choice(plans)
                when = utcnow() - timedelta(days=30 * month + rng.randint(0, 25))
                db.add(Transaction(gym_id=gym.id, direction=TxDirection.INCOMING, category=TxCategory.SUBSCRIPTION,
                                   status=TxStatus.SUCCESS, amount=plan.price, currency=plan.currency,
                                   description=f"{plan.name} - walk-in member", party_label="Member", provider="test",
                                   provider_order_id=f"SEED-H{month}-{k}-{secrets.token_hex(3)}", completed_at=when, created_at=when))

    async def payroll(self, gym: Gym) -> None:
        try:
            from app.services.payroll import generate_statements

            last = self.today.replace(day=1) - timedelta(days=1)
            async with self.db.begin_nested():
                await generate_statements(self.db, gym, last.year, last.month, queue_explanations=False)
        except Exception as exc:  # noqa: BLE001 - statements are a nicety for the demo
            log.info("payroll statements skipped for %s: %s", gym.slug, exc)


# ----------------------------------------------------------------------------------------------- entry point
async def seed_demo(db: AsyncSession, *, password: str | None = None, gyms: list[str] | None = None,
                    dataset: dict[str, Any] | None = None, write_credentials: bool = True) -> dict[str, Any]:
    """Load the demo dataset (optionally only the gyms whose slugs are listed). The caller commits.

    Returns {"created": bool, "password": str, "accounts": {label: e-mail}, "stats": {...}}. All demo accounts share one
    password: the one passed in, else the one already in var/demo_credentials.txt, else a new random one."""
    data = dataset or load_dataset()
    domain = data.get("email_domain", DEMO_DOMAIN)
    password = password or saved_password() or (secrets.token_urlsafe(9) + "A1!")
    loader = Loader(db, data, await hash_password(password), domain)
    try:
        from app.services.content import ensure_content

        await ensure_content(db)
    except Exception as exc:  # noqa: BLE001 - exercise library / videos are optional for the demo
        log.warning("content sync skipped: %s", exc)

    wanted = set(gyms or [])
    loaded: dict[str, Gym] = {}
    for g in data["gyms"]:
        if wanted and g["slug"] not in wanted:
            continue
        await loader.load_gym(g)
        loaded[g["slug"]] = await db.scalar(select(Gym).where(Gym.slug == g["slug"]))
    await loader.load_deferred(loaded)
    await db.flush()
    for gym in loaded.values():
        await loader.payroll(gym)

    from app.services.identity_lookup import backfill_usernames

    await backfill_usernames(db)
    await db.flush()
    settings = get_settings()
    accounts = {f"{gym_name} · {label} · {name}": email for gym_name, label, name, email in loader.accounts}
    accounts["Platform super-admin"] = (f"{settings.superadmin_email}  (password: SUPERADMIN_PASSWORD in .env; sign in "
                                        "at /superadmin/login)")
    result = {"created": any(loader.stats.values()), "password": password, "accounts": accounts, "stats": dict(loader.stats)}
    if write_credentials:
        _write_credentials(password, loader.accounts, settings.superadmin_email)
    return result


def _write_credentials(password: str, accounts: list[tuple[str, str, str, str]], superadmin: str) -> None:
    path = get_settings().var_dir / CREDENTIALS_FILE
    lines = ["IronPulse demo accounts - every account below uses the same password.\n",
             "These are database rows only (loaded from scripts/demo/dataset.json); delete them before going live.\n\n",
             f"password: {password}\n"]
    current = None
    for gym_name, label, name, email in accounts:
        if gym_name != current:
            lines.append(f"\n{gym_name}\n")
            current = gym_name
        lines.append(f"  {label:52} {name:24} {email}\n")
    lines.append(f"\nPlatform super-admin: {superadmin}  (password: SUPERADMIN_PASSWORD in .env; sign in at "
                 "/superadmin/login)\n")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(lines), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except OSError as exc:
        log.warning("could not write demo credentials: %s", exc)
