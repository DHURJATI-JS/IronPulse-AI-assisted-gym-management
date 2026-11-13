"""Build the load-test database ``ironpulse_loadtest`` from scratch, at production-like scale.

    "/path/to/project3 /.venv/bin/python" loadtest/seed_loadtest.py               # full build (drop + create + bulk)
    "/path/to/project3 /.venv/bin/python" loadtest/seed_loadtest.py --scale 0.1   # a 10 % copy (quick experiments)
    "/path/to/project3 /.venv/bin/python" loadtest/seed_loadtest.py --reset-runtime  # keep the data, clear what a run
                                                                                     # leaves behind (sessions, bans ...)

Steps (each one timed in the summary):
 1. DROP + CREATE DATABASE ironpulse_loadtest (PostgreSQL superuser from ~/.ironpulse/pg_superuser_password), owned by
    the app role of DATABASE_URL, CONNECTION LIMIT (so a run can never starve the other databases); extensions
    vector / citext / pg_trgm created by the superuser, like scripts/init_db.py does for test databases.
 2. Schema through the app itself: ``app.db.create_all()`` (create_all + additive sync_schema) with DATABASE_URL
    pointing at the load-test database (environment only - .env is never written).
 3. ``bootstrap()`` (super-admin lt_superadmin@example.com, exercise library) and the demo dataset through
    ``scripts.demo.loader.seed_demo(..., write_credentials=False)`` - var/demo_credentials.txt is NOT touched; its
    accounts are re-homed onto the reserved .test TLD and share the load-test password.
 4. Bulk data with CSV COPY (no ORM objects): gyms, branches (+hours/features/photos), plans, owners, staff with
    shifts, members with profiles / conditions / daily metrics / alerts / tasks / wellness checks, subscriptions and
    their payments, identity documents, products (+images, local pgvector embeddings), orders with items, status
    events (audit log), sales / refunds / inventory, reviews, wishlists, carts, coupons, chat threads + messages,
    notifications, job postings + enquiries, audit trail, transactions (walk-in income, salaries, expenses).
    One account per simulated user (the app signs an account out everywhere else on every sign-in): super-admins
    lt_superadmin_01..10, junk testers lt_junk_001..500 (online members; the signed-in JunkUsers), remembered
    accounts of the sign-in page's chooser for ~30 % of the members.
    RANDOM JUNK (--junk-share, default 0.10, deterministic): that share of the seeded display texts (names, gym /
    branch / product names and descriptions, notes, chat, task titles, health notes, reviews, job postings and
    enquiries) is random junk within each column's limit (ltlib/junk.py; JUNK_COLUMNS below).
    products.rating_avg / review_count: backfilled from the reviews in ONE set-based UPDATE after the COPY (else the
    app's hourly reconcile rewrites thousands of products during a run).
 5. Sequences, the HNSW index (rebuilt after the load), VACUUM ANALYZE; Redis db 9 FLUSHDB; loadtest/accounts.json
    + loadtest/catalogue.json for the Locust users (incl. other people's object ids for the JunkUser IDOR probes).

Idempotent: every run rebuilds the same database from scratch (fixed random seeds => the same data, junk included).
Runs with the PROJECT interpreter (.venv) because it imports the app. Never touches the main database, the dev
servers or Redis db 0; refuses to run while something listens on the load-test port.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import math
import os
import random
import shutil
import socket
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import ltenv  # noqa: E402
from ltlib import junk as junkgen  # noqa: E402

SEED = 20261110
T0 = time.perf_counter()
TIMINGS: list[tuple[str, float]] = []
COUNTS: Counter = Counter()

# ------------------------------------------------------------------------------------------------ random junk data
# --junk-share (default 0.10; --no-junk = 0): that share of the seeded values of the columns below is replaced by random
# junk (ltlib/junk.py: emoji / 4-byte characters, right-to-left and mixed-direction text, zero-width and combining
# characters, maximum-length strings, whitespace-heavy strings, HTML / JS / SQL / template-looking strings, control
# characters) - within each column's limit: VARCHAR(n) from the app's own metadata, the number here for TEXT columns
# (chat_messages.body: its CHECK of 1..4000). Its own random generator (JUNK_SEED): the rest of the data is the same
# with or without junk, and every rebuild is identical. Only display text is touched - never e-mails, slugs, cities,
# categories or anything else the Locust users put into a URL (a normal user's request never carries junk).
JUNK_SEED = SEED + 7
JUNK: dict[str, Any] = {"share": 0.10, "rng": random.Random(JUNK_SEED)}
JUNK_COUNTS: Counter = Counter()
JUNK_SAMPLES: list[str] = []
JUNK_COLUMNS: dict[str, dict[str, int | None]] = {  # table -> column -> max length for TEXT (None: the VARCHAR length)
    "users": {"full_name": None},
    "employees": {"full_name": None, "bio": 1000},
    "gyms": {"name": None, "tagline": None, "description": 2000},
    "branches": {"name": None, "address_line1": None},
    "branch_photos": {"caption": None},
    "products": {"name": None, "brand": None, "description": 4000},
    "customer_profiles": {"health_notes": 2000},
    "health_conditions": {"notes": None},
    "chat_threads": {"subject": None, "last_message_preview": None},
    "chat_messages": {"body": 4000},
    "tasks": {"title": None, "notes": 2000},
    "product_reviews": {"title": None, "body": 2000},
    "job_postings": {"title": None, "description": 4000, "requirements": 2000, "location": None},
    "job_enquiries": {"name": None, "message": 2000},
}


def log(msg: str) -> None:
    print(f"[seed {time.perf_counter() - T0:7.1f}s] {msg}", flush=True)


class Step:
    def __init__(self, name: str) -> None:
        self.name = name

    def __enter__(self) -> "Step":
        self.t = time.perf_counter()
        log(f"{self.name} ...")
        return self

    def __exit__(self, *exc: object) -> None:
        dt = time.perf_counter() - self.t
        TIMINGS.append((self.name, dt))
        log(f"{self.name} done in {dt:.1f}s")


# ================================================================================================ environment
def configure_environment() -> None:
    """Point the app at the load-test database/Redis/var BEFORE anything imports app.config (env beats .env)."""
    env = ltenv.server_env("off")
    os.environ.update(env)
    os.environ["RUN_BACKGROUND_JOBS"] = "false"
    ltenv.assert_loadtest_target(os.environ["DATABASE_URL"])


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((ltenv.HOST, port)) == 0


# ================================================================================================ COPY helper
NULL = "\\N"


def enc(v: Any) -> str:
    if v is None:
        return NULL
    if v is True:
        return "t"
    if v is False:
        return "f"
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"), ensure_ascii=False)
    if isinstance(v, (datetime, date, dtime)):
        return v.isoformat()
    return str(v)


class Copier:
    """COPY ... FROM STDIN (CSV, NULL '\\N') with column checks against the app's own table metadata."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        from app.db import Base

        self.tables = Base.metadata.tables

    def check(self, table: str, columns: list[str]) -> None:
        t = self.tables[table]
        unknown = [c for c in columns if c not in t.c]
        if unknown:
            raise RuntimeError(f"{table}: unknown columns {unknown}")
        # COPY applies server defaults only; Python-side defaults (and serial ids, which we always pass) must be given
        required = {c.name for c in t.columns if not c.nullable and c.server_default is None}
        missing = sorted(required - set(columns))
        if missing:
            raise RuntimeError(f"{table}: NOT NULL columns without a server default are missing: {missing}")

    def junk_targets(self, table: str, columns: list[str]) -> list[tuple[int, str, int]]:
        """(column index, name, max length) of this COPY's columns that get random junk (JUNK_COLUMNS)."""
        spec = JUNK_COLUMNS.get(table) if JUNK["share"] > 0 else None
        out = []
        for name, cap in (spec or {}).items():
            if name in columns:
                length = getattr(self.tables[table].c[name].type, "length", None)
                limit = cap or length
                if not limit:
                    raise RuntimeError(f"{table}.{name}: TEXT column without a junk length in JUNK_COLUMNS")
                out.append((columns.index(name), name, int(min(limit, length) if length and cap else limit)))
        return out

    @staticmethod
    def with_junk(table: str, rows: Iterable[tuple], targets: list[tuple[int, str, int]]) -> Iterable[tuple]:
        rng, share = JUNK["rng"], JUNK["share"]
        for row in rows:
            hit = None
            for i, name, limit in targets:
                if rng.random() < share:
                    if hit is None:
                        hit = list(row)
                    hit[i] = junkgen.text(rng, limit)
                    JUNK_COUNTS[f"{table}.{name}"] += 1
                    if len(JUNK_SAMPLES) < 40 and rng.random() < 0.02:
                        JUNK_SAMPLES.append(f"{table}.{name}: {hit[i]!r}"[:160])
            yield tuple(hit) if hit is not None else row

    async def copy(self, table: str, columns: list[str], rows: Iterable[tuple], chunk: int = 50_000) -> int:
        self.check(table, columns)
        targets = self.junk_targets(table, columns)
        if targets:
            rows = self.with_junk(table, rows, targets)
        total = 0
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        n = 0
        for row in rows:
            w.writerow([enc(v) for v in row])
            n += 1
            if n >= chunk:
                await self._flush(table, columns, buf)
                total += n
                buf = io.StringIO()
                w = csv.writer(buf, lineterminator="\n")
                n = 0
        if n:
            await self._flush(table, columns, buf)
            total += n
        COUNTS[table] += total
        return total

    async def _flush(self, table: str, columns: list[str], buf: io.StringIO) -> None:
        data = buf.getvalue().encode("utf-8")
        await self.conn.copy_to_table(table, source=io.BytesIO(data), columns=columns, format="csv", null=NULL)


# ================================================================================================ reference data
CITIES = [  # name, state, lat, lon, weight, spread (deg), pin prefix, localities
    ("Bengaluru", "Karnataka", 12.9716, 77.5946, 18, 0.075, "560", ["Indiranagar", "Koramangala", "HSR Layout",
     "Whitefield", "Jayanagar", "Malleshwaram", "Electronic City", "Hebbal", "JP Nagar", "Marathahalli", "Yelahanka",
     "Bannerghatta Road", "Sarjapur Road", "Rajajinagar", "BTM Layout"]),
    ("Mumbai", "Maharashtra", 19.0760, 72.8777, 14, 0.07, "400", ["Bandra West", "Andheri East", "Powai", "Lower Parel",
     "Juhu", "Malad", "Goregaon", "Chembur", "Colaba", "Worli", "Borivali", "Thane West"]),
    ("New Delhi", "Delhi", 28.6139, 77.2090, 12, 0.08, "110", ["Hauz Khas", "Connaught Place", "Saket", "Lajpat Nagar",
     "Dwarka", "Rajouri Garden", "Vasant Kunj", "Karol Bagh", "Greater Kailash", "Janakpuri", "Pitampura"]),
    ("Hyderabad", "Telangana", 17.3850, 78.4867, 9, 0.07, "500", ["Banjara Hills", "Gachibowli", "Jubilee Hills",
     "Madhapur", "Kondapur", "Begumpet", "Kukatpally", "Secunderabad", "Hitech City"]),
    ("Chennai", "Tamil Nadu", 13.0827, 80.2707, 8, 0.065, "600", ["T. Nagar", "Besant Nagar", "Adyar", "Anna Nagar",
     "Velachery", "Nungambakkam", "OMR", "Mylapore", "Porur"]),
    ("Pune", "Maharashtra", 18.5204, 73.8567, 7, 0.06, "411", ["Koregaon Park", "Baner", "Kothrud", "Viman Nagar",
     "Hinjewadi", "Aundh", "Kalyani Nagar", "Wakad"]),
    ("Kolkata", "West Bengal", 22.5726, 88.3639, 6, 0.06, "700", ["Salt Lake", "Park Street", "Ballygunge", "New Town",
     "Behala", "Gariahat"]),
    ("Ahmedabad", "Gujarat", 23.0225, 72.5714, 5, 0.06, "380", ["Navrangpura", "Satellite", "Bodakdev", "Vastrapur",
     "Maninagar", "SG Highway"]),
    ("Gurugram", "Haryana", 28.4595, 77.0266, 4, 0.05, "122", ["DLF Phase 4", "Sohna Road", "Golf Course Road",
     "Sector 56", "Cyber City"]),
    ("Jaipur", "Rajasthan", 26.9124, 75.7873, 3, 0.05, "302", ["Malviya Nagar", "Vaishali Nagar", "C-Scheme", "Mansarovar"]),
    ("Lucknow", "Uttar Pradesh", 26.8467, 80.9462, 3, 0.05, "226", ["Gomti Nagar", "Hazratganj", "Aliganj", "Indira Nagar"]),
    ("Noida", "Uttar Pradesh", 28.5355, 77.3910, 3, 0.04, "201", ["Sector 18", "Sector 62", "Sector 50", "Sector 137"]),
    ("Kochi", "Kerala", 9.9312, 76.2673, 2, 0.04, "682", ["Kakkanad", "Edappally", "Panampilly Nagar", "Vyttila"]),
    ("Chandigarh", "Chandigarh", 30.7333, 76.7794, 2, 0.035, "160", ["Sector 17", "Sector 35", "Sector 8", "Sector 22"]),
    ("Indore", "Madhya Pradesh", 22.7196, 75.8577, 2, 0.04, "452", ["Vijay Nagar", "Palasia", "Bhawarkua", "Rau"]),
    ("Coimbatore", "Tamil Nadu", 11.0168, 76.9558, 2, 0.04, "641", ["RS Puram", "Peelamedu", "Saibaba Colony", "Gandhipuram"]),
    ("Mysuru", "Karnataka", 12.2958, 76.6394, 1.5, 0.03, "570", ["Kuvempunagar", "Vijayanagar", "Gokulam", "Saraswathipuram"]),
    ("Nagpur", "Maharashtra", 21.1458, 79.0882, 1.5, 0.04, "440", ["Dharampeth", "Sadar", "Manish Nagar", "Civil Lines"]),
    ("Surat", "Gujarat", 21.1702, 72.8311, 1.5, 0.04, "395", ["Adajan", "Vesu", "Athwa", "Piplod"]),
    ("Bhubaneswar", "Odisha", 20.2961, 85.8245, 1, 0.035, "751", ["Saheed Nagar", "Patia", "Jaydev Vihar"]),
    ("Visakhapatnam", "Andhra Pradesh", 17.6868, 83.2185, 1, 0.035, "530", ["MVP Colony", "Dwaraka Nagar", "Madhurawada"]),
    ("Thiruvananthapuram", "Kerala", 8.5241, 76.9366, 1, 0.03, "695", ["Kowdiar", "Pattom", "Kazhakoottam"]),
    ("Guwahati", "Assam", 26.1445, 91.7362, 1, 0.03, "781", ["Zoo Road", "Dispur", "Beltola"]),
    ("Mangaluru", "Karnataka", 12.9141, 74.8560, 1, 0.03, "575", ["Kadri", "Bejai", "Kankanady"]),
    ("Vadodara", "Gujarat", 22.3072, 73.1812, 1, 0.035, "390", ["Alkapuri", "Akota", "Gotri"]),
]
FIRST = ["Aarav", "Vivaan", "Aditya", "Vihaan", "Arjun", "Sai", "Reyansh", "Ayaan", "Krishna", "Ishaan", "Shaurya",
         "Atharv", "Kabir", "Rohan", "Aryan", "Dhruv", "Karan", "Nikhil", "Rahul", "Siddharth", "Ananya", "Diya",
         "Aadhya", "Saanvi", "Myra", "Anika", "Pari", "Ira", "Kiara", "Meera", "Riya", "Sneha", "Priya", "Kavya",
         "Ishita", "Tanvi", "Neha", "Pooja", "Aisha", "Zara", "Farhan", "Imran", "Sana", "Arnav", "Yash", "Tara",
         "Nisha", "Varun", "Gaurav", "Lakshmi", "Harini", "Deepak", "Manoj", "Suresh", "Anjali", "Divya", "Rekha",
         "Vikram", "Arun", "Bhavna", "Chirag", "Esha", "Gitanjali", "Hemant", "Jasleen", "Kunal", "Lavanya", "Mohit"]
LAST = ["Sharma", "Verma", "Iyer", "Nair", "Reddy", "Rao", "Patel", "Shah", "Mehta", "Gupta", "Singh", "Kaur",
        "Das", "Banerjee", "Chatterjee", "Mukherjee", "Pillai", "Menon", "Kulkarni", "Joshi", "Deshpande", "Khan",
        "Ahmed", "Fernandes", "D'Souza", "Bhat", "Hegde", "Shetty", "Naidu", "Chowdhury", "Malhotra", "Kapoor",
        "Agarwal", "Jain", "Bose", "Sen", "Ghosh", "Mishra", "Pandey", "Tiwari", "Yadav", "Thakur", "Saxena"]
GYM_A = ["Iron", "Pulse", "Titan", "Apex", "Core", "Prime", "Peak", "Zen", "Flex", "Forge", "Summit", "Vital", "Rise",
         "Bolt", "Urban", "Metro", "Elite", "Power", "Pure", "Alpha", "Nova", "Atlas", "Spartan", "Kinetic", "Velocity",
         "Momentum", "Stride", "Thrive", "Ember", "Oak", "Granite", "Monsoon", "Lotus", "Cobalt", "Saffron", "Tiger"]
GYM_B = ["Fitness", "Gym", "Strength Club", "Athletics", "Fitness Studio", "Training Co.", "Performance",
         "Wellness Club", "Barbell Club", "Fit Hub", "Movement Lab", "Health Club"]
TAGLINES = ["Stronger every week.", "Train smart, live well.", "Your neighbourhood strength club.",
            "Coaching that fits your day.", "Lift, move, recover.", "Real coaches, real results.",
            "Open early, close late, always friendly.", "Science-backed training for everyone."]
FEATURES = ["Air conditioning", "Cardio zone", "Free weights", "Machines", "Showers", "Lockers", "Personal training",
            "Steam room", "Parking", "Group classes", "Functional zone", "Women-only hours", "Juice bar", "CrossFit rig",
            "Yoga studio", "Swimming pool", "Wheelchair access", "Physiotherapy room"]
STAFF_ROLES = [("trainer", 55, "Personal Trainer"), ("receptionist", 12, "Front Desk Executive"),
               ("manager", 8, "Branch Manager"), ("cleaner", 8, "Housekeeping Associate"),
               ("nutritionist", 5, "Nutritionist"), ("physiotherapist", 4, "Physiotherapist"),
               ("maintenance", 3, "Maintenance Technician"), ("accountant", 3, "Accountant"),
               ("security", 2, "Security Officer")]
SPECIALTIES = ["Strength", "Hypertrophy", "Fat loss", "Mobility", "Calisthenics", "Powerlifting", "Yoga", "HIIT",
               "Rehab", "Senior fitness", "Sports conditioning", "Pre/post-natal", "Boxing", "Kettlebells"]
CONDITIONS = [("condition", "Asthma"), ("condition", "Hypertension"), ("condition", "Type 2 diabetes"),
              ("injury", "Lower back pain"), ("injury", "Knee injury (old ACL)"), ("allergy", "Lactose intolerance"),
              ("allergy", "Peanut allergy"), ("medication", "Metformin"), ("condition", "Migraine"),
              ("injury", "Shoulder impingement"), ("condition", "Hypothyroidism"), ("allergy", "Dust allergy")]
GOALS = ["general_fitness", "weight_loss", "bulking", "calisthenics"]
LEVELS = ["beginner", "intermediate", "advanced"]
TASKS = [("Mobility flow - 10 minutes", "workout"), ("Drink 2 L of water", "reminder"), ("Log weight & height", "task"),
         ("Leg day at the gym", "workout"), ("Meal prep for the week", "task"), ("Stretch before bed", "reminder"),
         ("Book a session with my trainer", "task"), ("5 km easy run", "workout"), ("Buy whey protein", "task")]
JOB_TEMPLATES = [("trainer", "Personal Trainer", "full_time", (25000, 45000)),
                 ("trainer", "Strength & Conditioning Coach", "full_time", (35000, 60000)),
                 ("trainer", "Yoga Instructor (part-time)", "part_time", (15000, 25000)),
                 ("receptionist", "Front Desk Executive", "full_time", (16000, 24000)),
                 ("manager", "Gym Manager", "full_time", (45000, 80000)),
                 ("cleaner", "Housekeeping Associate", "full_time", (14000, 19000)),
                 ("nutritionist", "Sports Nutritionist", "contract", (30000, 50000)),
                 ("physiotherapist", "Physiotherapist", "full_time", (35000, 55000)),
                 ("maintenance", "Equipment Maintenance Technician", "full_time", (18000, 28000)),
                 ("accountant", "Accountant (part-time)", "part_time", (18000, 30000)),
                 ("software_developer", "Software Developer (remote)", "full_time", (60000, 120000))]
THREAD_SUBJECTS = ["Diet plan question", "Rescheduling my session", "Locker issue", "Knee pain during squats",
                   "Membership renewal", "Can I bring a guest?", "Supplement advice", "Online session link",
                   "Progress check-in", "Shoulder mobility drills", "Holiday timings", "Payment receipt"]
MEMBER_LINES = ["Hi! Can we move tomorrow's session to 7 am?", "Thanks for the plan, it's going well.",
                "My knee felt a bit sore after lunges - should I skip them this week?",
                "Is the gym open on the public holiday?", "Could you check my form video?",
                "I've been consistent for three weeks now!", "What should I eat before a morning workout?",
                "Can I pause my membership for two weeks?", "The shower in the men's locker room is leaking.",
                "How many rest days do you recommend?", "Got it, see you on Thursday.", "Will do, thanks coach!"]
STAFF_LINES = ["Sure, 7 am works - see you then.", "Great work! Let's add one more set next week.",
               "Skip lunges for now and do glute bridges instead. If the pain continues, see a physio.",
               "Yes, we're open 7 am to 2 pm on the holiday.", "Send it over and I'll review it tonight.",
               "Aim for some carbs and a little protein 60-90 minutes before training.",
               "You can pause from the membership page or at the front desk.",
               "Thanks for letting us know - maintenance will fix it today.",
               "Two rest days a week is a good start for your plan.", "Proud of your consistency!"]
NOTIFY = [  # category, severity, title, body, link kind
    ("payment", "success", "Payment received", "We received your membership payment. Thank you!", "subs"),
    ("reminder", "info", "Time to wind down", "Time to wind down - recovery matters. Bedtime is 22:30.", "me"),
    ("message", "info", "New message", "You have a new message from your gym.", "chat"),
    ("health", "info", "Weekly progress", "You logged 5 readings this week - nice consistency.", "health"),
    ("account", "info", "Profile updated", "Your profile details were updated.", "account"),
    ("payment", "success", "Order confirmed", "Your shop order is confirmed and being prepared.", "orders"),
    ("reminder", "info", "Session tomorrow", "Reminder: your training session is tomorrow at 7:00.", "calendar"),
    ("system", "info", "New videos available", "Three new mobility videos were added to your library.", "videos"),
    ("security", "warning", "New sign-in to your account", "Your account was signed in from a new device.", "account"),
]
OWNER_NOTIFY = [("payment", "success", "New shop order", "A member placed a new shop order.", "/owner/shop/orders"),
                ("staff", "info", "Shift updated", "A trainer's shift was updated.", "/owner/staff"),
                ("payment", "warning", "Low stock", "Some products are running low.", "/owner/shop/products"),
                ("message", "info", "New member message", "A member sent your team a message.", "/chat"),
                ("payment", "info", "Salaries due", "Salary statements are due this week.", "/owner/payroll")]
SEARCH_TERMS = ["whey", "protein", "creatine", "gloves", "belt", "shaker", "yoga mat", "resistance band", "t-shirt",
                "pre workout", "bcaa", "oats", "peanut butter", "bottle", "straps", "chocolate", "vanilla", "dumbbell",
                "skipping rope", "hoodie", "electrolyte", "multivitamin", "muscle gain", "weight loss snack"]
TYPO_TERMS = ["wehy", "protien", "creatin", "glvoes", "shakr", "yoga matt", "resistence band", "tshirt", "prewokout",
              "multivitamn", "dumbell", "peanut buter", "electrolite"]
VARIANTS = {
    "supplements": ["Chocolate", "Vanilla", "Cookies & Cream", "Mango", "Unflavoured", "Coffee", "Strawberry",
                    "Kesar Pista", "500 g", "2 kg"],
    "nutrition": ["Chocolate", "Peanut", "Berry", "Box of 6", "Box of 12", "Salted Caramel", "Crunchy"],
    "apparel": ["Black / M", "Navy / L", "Grey / S", "Olive / XL", "Maroon / M", "White / L", "Black / XXL"],
    "accessories": ["Black", "Red", "Pro", "Classic", "Large", "Medium", "2.0"],
    "wearables": ["Black", "Silver", "Rose Gold", "Gen 2"],
    "equipment": ["Light", "Medium", "Heavy", "Set of 3", "Pro", "6 mm", "10 mm"],
    "other": ["Standard", "Deluxe"],
}
BRANDS = {"supplements": ["PulseFuel", "IronLeaf Nutrition", "CoreNutrix", "Vitalis Labs", "PeakForm"],
          "nutrition": ["PulseFuel", "OatBox", "NutriCrunch", "GoodGrain"],
          "apparel": ["PulseWear", "Stride Athletics", "Urban Sweat"],
          "accessories": ["GripMaster", "PulseGear", "LiftRight"],
          "wearables": ["PulseBand", "TrackFit"],
          "equipment": ["FlexBand", "HomeIron", "PulseGear"],
          "other": ["PulseGear"]}


def weighted(rng: random.Random, items: list, weights: list):  # noqa: ANN201
    return rng.choices(items, weights=weights, k=1)[0]


def money(x: float) -> Decimal:
    return Decimal(f"{x:.2f}")


def nice_price(x: float) -> Decimal:
    if x < 200:
        return Decimal(max(49, int(round(x / 10.0)) * 10 - 1))
    return Decimal(int(round(x / 100.0)) * 100 - 1)


def jitter(rng: random.Random, lat: float, lon: float, spread: float) -> tuple[float, float]:
    return round(lat + rng.gauss(0, spread), 6), round(lon + rng.gauss(0, spread), 6)


# ================================================================================================ data model (in memory)
@dataclass
class GymRec:
    id: int
    slug: str
    name: str
    owner_id: int
    owner_idx: int
    size: int
    online_only: bool
    home_city: int
    currency: str = "INR"
    branches: list[int] = field(default_factory=list)  # all branch ids
    published: list[int] = field(default_factory=list)
    branch_city: dict[int, int] = field(default_factory=dict)  # branch id -> city index
    branch_pos: dict[int, tuple[float, float]] = field(default_factory=dict)
    plans: dict[str, tuple[int, str, Decimal, str]] = field(default_factory=dict)  # code -> (id, mode, price, name)
    trainers: list[int] = field(default_factory=list)  # employee ids
    trainer_user: dict[int, int] = field(default_factory=dict)  # employee id -> user id
    staff: list[int] = field(default_factory=list)
    members: list[int] = field(default_factory=list)
    products: list[int] = field(default_factory=list)  # active product ids
    online: bool = True


@dataclass
class MemberRec:
    id: int
    idx: int
    email: str
    name: str
    gym: int  # gym id
    branch: int | None
    lat: float | None
    lon: float | None
    height: float
    weight: float
    minor: bool
    threads: list[int] = field(default_factory=list)
    orders: list[int] = field(default_factory=list)
    has_active: bool = True


@dataclass
class ProductRec:
    id: int
    gym: int
    name: str
    slug: str
    price: Decimal
    tax: Decimal
    fulfillment: str
    stock: int
    active: bool


# ================================================================================================ the builder
class Builder:
    def __init__(self, conn: Any, scale: float) -> None:
        self.conn = conn
        self.cp = Copier(conn)
        self.scale = scale
        self.rng = random.Random(SEED)
        self.now = datetime.now(UTC).replace(microsecond=0)
        from zoneinfo import ZoneInfo

        self.today = self.now.astimezone(ZoneInfo("Asia/Kolkata")).date()
        self.ids: dict[str, int] = {}
        self.gyms: list[GymRec] = []
        self.gym_by_id: dict[int, GymRec] = {}
        self.members: list[MemberRec] = []
        self.owners: list[dict[str, Any]] = []
        self.staff_accounts: list[dict[str, Any]] = []
        self.abusers: list[dict[str, Any]] = []
        self.products: dict[int, ProductRec] = {}
        self.threads_by_gym_team: dict[int, list[int]] = defaultdict(list)
        self.threads_by_emp: dict[int, list[int]] = defaultdict(list)
        self.user_names: dict[int, str] = {}
        self.user_emails: dict[int, str] = {}
        self.user_ip: dict[int, str] = {}
        self.wishlist_by_user: dict[int, set[int]] = defaultdict(set)
        self.jobs_by_gym: dict[int, list[tuple[int, int]]] = defaultdict(list)  # gym -> [(job id, enquiries)]
        self.heavy_owner_idx: list[int] = []
        self.heavy_member_ids: list[int] = []
        self.heavy_trainer_emps: list[int] = []

    def n(self, base: int, minimum: int = 1) -> int:
        return max(minimum, int(round(base * self.scale)))

    async def next_id(self, table: str, count: int) -> int:
        """First id of a block of ``count`` explicit ids (sequences are reset at the end)."""
        if table not in self.ids:
            self.ids[table] = int(await self.conn.fetchval(f"SELECT coalesce(max(id), 0) FROM {table}"))
        start = self.ids[table] + 1
        self.ids[table] += count
        return start

    # -------------------------------------------------------------------------------------- pools from the demo seed
    async def load_pools(self) -> None:
        c = self.conn
        self.photo_pool = [r[0] for r in await c.fetch("SELECT DISTINCT image_key FROM branch_photos ORDER BY 1")]
        self.avatar_pool = [r[0] for r in await c.fetch(
            "SELECT DISTINCT photo_key FROM users WHERE photo_key IS NOT NULL ORDER BY 1")]
        self.logo_pool = [r[0] for r in await c.fetch("SELECT DISTINCT logo_key FROM gyms WHERE logo_key IS NOT NULL ORDER BY 1")]
        self.id_front_key = await c.fetchval("SELECT front_key FROM identity_documents ORDER BY id LIMIT 1")
        rows = await c.fetch("SELECT p.sku, p.id, i.image_key FROM product_images i JOIN products p ON p.id = i.product_id "
                             "ORDER BY p.id, i.sort_order, i.id")
        self.product_images: dict[str, list[str]] = defaultdict(list)
        first_product: dict[str, int] = {}
        for sku, pid, key in rows:  # the gallery of ONE demo product per SKU (each demo gym rendered its own copy)
            if first_product.setdefault(sku, pid) == pid:
                self.product_images[sku].append(key)
        from scripts.demo.loader import load_dataset

        self.catalogue = load_dataset()["catalogue"]
        if not (self.photo_pool and self.avatar_pool and self.id_front_key):
            raise RuntimeError("the demo seed left no images to reuse - did seed_demo run?")
        self.hash = await c.fetchval("SELECT password_hash FROM users WHERE email = $1", ltenv.SUPERADMIN_EMAIL)
        if not self.hash:
            raise RuntimeError("lt_superadmin was not created by bootstrap()")

    # -------------------------------------------------------------------------------------- users
    def person(self) -> tuple[str, str]:
        return self.rng.choice(FIRST), self.rng.choice(LAST)

    def user_row(self, uid: int, email: str, name: str, role: str, *, dob: date, city: str | None, lat: float | None,
                 lon: float | None, photo: str | None, ip: str, created: datetime) -> tuple:
        username = email.split("@", 1)[0]
        self.user_names[uid], self.user_emails[uid], self.user_ip[uid] = name, email, ip
        return (uid, email, username, None, None, self.hash, name, role, True, created, dob, None, photo,
                "Asia/Kolkata", "IN", city, lat, lon, created if lat is not None else None, "verified", 0, None,
                self.now - timedelta(hours=self.rng.randint(1, 240)), ip, created, created)

    USER_COLS = ["id", "email", "username", "phone", "phone_normalized", "password_hash", "full_name", "role",
                 "is_active", "email_verified_at", "date_of_birth", "gender", "photo_key", "timezone", "country_code",
                 "city", "latitude", "longitude", "location_updated_at", "id_status", "failed_login_count",
                 "locked_until", "last_login_at", "last_login_ip", "created_at", "updated_at"]

    def dob(self, lo: int, hi: int) -> date:
        age = self.rng.randint(lo, hi)
        return self.today - timedelta(days=age * 365 + self.rng.randint(15, 340))

    # -------------------------------------------------------------------------------------- gyms + owners
    async def build_gyms(self) -> None:
        rng = self.rng
        from app.core.utils import slugify

        n_gyms, n_branches = self.n(300, 4), self.n(2000, 8)
        n_owners = self.n(200, 3)
        # heavy-tailed chain sizes (Pareto): a few national chains (~100+ branches), a middle tier and many
        # single-city gyms with 1-5 branches; ~3 % of the gyms are online-only (no branch at all)
        raw = [0 if rng.random() < 0.033 else min(150, int(rng.paretovariate(1.15))) for _ in range(n_gyms)]
        if not any(raw):
            raw[0] = 1
        factor = n_branches / sum(raw)
        sizes = [0 if r == 0 else max(1, int(r * factor)) for r in raw]
        live = [i for i, s in enumerate(sizes) if s > 0]
        for _ in range(100_000):  # bounded: hand out / take back the rounding remainder
            diff = n_branches - sum(sizes)
            if diff == 0:
                break
            i = rng.choice(live)
            if diff > 0:
                sizes[i] += 1
            elif sizes[i] > 1:
                sizes[i] -= 1
        # owners: every owner has at least one gym, some two or three (round-robin over a shuffled gym order)
        owner_of = [i % n_owners for i in range(n_gyms)]
        rng.shuffle(owner_of)
        owner_start = await self.next_id("users", n_owners)
        weights = [c[4] for c in CITIES]
        owner_rows = []
        for j in range(n_owners):
            uid = owner_start + j
            fn, ln = self.person()
            email = f"lt_owner_{j + 1:03d}@{ltenv.EMAIL_DOMAIN}"
            ip = ltenv.account_ip("owner", j + 1)
            city = weighted(rng, CITIES, weights)
            created = self.now - timedelta(days=rng.randint(200, 900))
            owner_rows.append(self.user_row(uid, email, f"{fn} {ln}", "owner", dob=self.dob(28, 58), city=city[0],
                                            lat=None, lon=None, photo=rng.choice(self.avatar_pool), ip=ip, created=created))
            self.owners.append({"e": email, "id": uid, "ip": ip, "gyms": [], "slugs": []})
        await self.cp.copy("users", self.USER_COLS, owner_rows)

        gym_start = await self.next_id("gyms", n_gyms)
        used = {r[0] for r in await self.conn.fetch("SELECT slug FROM gyms")}
        names = [f"{a} {b}" for a in GYM_A for b in GYM_B]
        rng.shuffle(names)
        gym_rows = []
        for i in range(n_gyms):
            gid = gym_start + i
            oi = owner_of[i]
            home = weighted(rng, list(range(len(CITIES))), weights)
            name = names[i % len(names)] + ("" if i < len(names) else f" {CITIES[home][0]}")
            slug = slugify(name, max_len=120)
            while slug in used:
                slug = f"{slug}-{CITIES[home][0].lower().replace(' ', '-')}" if slug.count("-") < 6 else f"{slug}-{gid}"
            used.add(slug)
            online_only = sizes[i] == 0
            online = online_only or rng.random() < 0.55
            created = self.now - timedelta(days=rng.randint(120, 1500))
            g = GymRec(gid, slug, name, self.owners[oi]["id"], oi, sizes[i], online_only, home, online=online)
            self.gyms.append(g)
            self.gym_by_id[gid] = g
            self.owners[oi]["gyms"].append(gid)
            self.owners[oi]["slugs"].append(slug)
            logo = rng.choice(self.logo_pool) if self.logo_pool and rng.random() < 0.6 else None
            gym_rows.append((gid, self.owners[oi]["id"], name, slug, rng.choice(TAGLINES),
                             f"{name} is a {'chain of ' + str(sizes[i]) + ' gyms' if sizes[i] > 1 else 'neighbourhood gym'} "
                             f"focused on coached strength training, conditioning and recovery.", logo, "IN", "INR",
                             f"hello@{slug[:40]}.example.com", None, f"https://{slug[:40]}.example.com", online, True,
                             not online_only, True, created, created))
        await self.cp.copy("gyms", ["id", "owner_id", "name", "slug", "tagline", "description", "logo_key", "country_code",
                                    "currency", "contact_email", "contact_phone", "website", "online_enabled",
                                    "online_open", "offline_enabled", "is_active", "created_at", "updated_at"], gym_rows)
        await self.build_branches(n_branches)

    async def build_branches(self, n_branches: int) -> None:
        rng = self.rng
        weights = [c[4] for c in CITIES]
        start = await self.next_id("branches", n_branches)
        bid = start
        b_rows, h_rows, f_rows, p_rows, v_rows = [], [], [], [], []
        f_id = await self.next_id("branch_features", n_branches * 9)
        p_id = await self.next_id("branch_photos", n_branches * 5)
        v_id = await self.next_id("verification_runs", n_branches)
        loc_count: Counter = Counter()
        for g in self.gyms:
            if g.size == 0:
                continue
            # a chain spreads over several cities (home city first), a small gym stays in one or two
            n_cities = 1 if g.size <= 3 else min(len(CITIES), 2 + g.size // 8)
            city_idx = [g.home_city] + [c for c in rng.sample(range(len(CITIES)), k=min(len(CITIES), n_cities + 2))
                                        if c != g.home_city][: n_cities - 1]
            for k in range(g.size):
                ci = city_idx[0] if k < max(1, g.size // 3) else weighted(rng, city_idx, [weights[c] for c in city_idx])
                cname, state, clat, clon, _, spread, pin, locs = CITIES[ci]
                loc = rng.choice(locs)
                loc_count[(g.id, loc)] += 1
                suffix = f" {loc_count[(g.id, loc)]}" if loc_count[(g.id, loc)] > 1 else ""
                lat, lon = jitter(rng, clat, clon, spread)
                r = rng.random()
                status = "verified" if r < 0.965 else ("draft" if r < 0.985 else "needs_review")
                is_open = rng.random() > 0.02
                n_photos = 3 + (rng.random() < 0.45) + (rng.random() < 0.2) if rng.random() > 0.01 else 2
                created = self.now - timedelta(days=rng.randint(30, 1200))
                name = f"{loc}{suffix}"
                summary = "All documents matched the registration details; address confirmed by geocoding." if status == "verified" else None
                b_rows.append((bid, g.id, name[:120], f"{rng.randint(1, 250)}, {rng.choice(['Main Road', '1st Cross', '100 Feet Road', 'Ring Road', 'MG Road', 'Station Road', '4th Block', 'Sector Road'])}, {loc}",
                               None, cname, state, f"{pin}{rng.randint(1, 99):03d}", "IN", "Asia/Kolkata",
                               f"+91 80{rng.randint(10000000, 99999999)}", f"{g.slug[:40]}.{bid}@{ltenv.EMAIL_DOMAIN}",
                               lat, lon, round(lat + 0.0003, 6), round(lon - 0.0002, 6),
                               f"{loc}, {cname}, {state}, India", None, 0.92, f"REG-{g.id:04d}-{bid:06d}", status, summary,
                               None, created + timedelta(days=3) if status == "verified" else None, is_open,
                               None if is_open else self.now - timedelta(days=rng.randint(2, 60)),
                               None if is_open else "Renovation", created, created))
                opens = dtime(rng.choice([5, 6, 6, 6, 7]), rng.choice([0, 30]))
                closes = dtime(min(opens.hour + rng.choice([13, 14, 14]), 22) if opens.minute == 0 else min(opens.hour + 13, 22), 0)
                for wd in range(7):
                    if wd == 6:
                        h_rows.append((bid, wd, dtime(7, 0), dtime(13, 0)))
                    else:
                        h_rows.append((bid, wd, opens, closes))
                for fname in rng.sample(FEATURES, k=rng.randint(5, 9)):
                    f_rows.append((f_id, bid, fname))
                    f_id += 1
                for p in range(n_photos):
                    p_rows.append((p_id, bid, rng.choice(self.photo_pool), f"{name} - {'machines' if p % 2 == 0 else 'overall'}",
                                   "machines" if p % 2 == 0 else "overall", 1280, 853, p, created))
                    p_id += 1
                if status == "verified":
                    v_rows.append((v_id, bid, None, "verified", 0.93, [{"check": "Registration number format", "passed": True},
                                   {"check": "Address vs map pin", "passed": True}], summary, "seed", False, None,
                                   created + timedelta(days=3)))
                    v_id += 1
                g.branches.append(bid)
                g.branch_city[bid] = ci
                g.branch_pos[bid] = (lat, lon)
                if status == "verified" and is_open and n_photos >= 3:
                    g.published.append(bid)
                bid += 1
        await self.cp.copy("branches", ["id", "gym_id", "name", "address_line1", "address_line2", "city", "state",
                                        "postal_code", "country_code", "timezone", "phone", "email", "latitude",
                                        "longitude", "geocoded_latitude", "geocoded_longitude", "formatted_address",
                                        "geo_place_id", "geo_confidence", "registration_number", "verification_status",
                                        "verification_summary", "verification_checks", "verified_at", "is_open",
                                        "closed_at", "closed_reason", "created_at", "updated_at"], b_rows)
        await self.cp.copy("branch_hours", ["branch_id", "weekday", "opens_at", "closes_at"], h_rows)
        await self.cp.copy("branch_features", ["id", "branch_id", "name"], f_rows)
        await self.cp.copy("branch_photos", ["id", "branch_id", "image_key", "caption", "kind", "width", "height",
                                             "sort_order", "created_at"], p_rows)
        await self.cp.copy("verification_runs", ["id", "branch_id", "employee_id", "outcome", "score", "checks", "summary",
                                                 "ai_provider", "manual", "reviewer_id", "created_at"], v_rows)

    # -------------------------------------------------------------------------------------- plans
    async def build_plans(self) -> None:
        from app.services.checkout import PLAN_CATALOG, default_price

        rows = []
        pid = await self.next_id("plans", len(self.gyms) * len(PLAN_CATALOG))
        when = self.now - timedelta(days=100)
        for g in self.gyms:
            prng = random.Random(f"ironpulse-plans:{g.id}:{g.currency}")  # the same prices seed_default_plans gives
            disabled = set(self.rng.sample([c[0] for c in PLAN_CATALOG], k=self.rng.randint(1, 3)))
            sellable = {c[0] for c in PLAN_CATALOG
                        if (c[1].value == "online" and g.online) or (c[1].value == "offline" and not g.online_only)
                        or (c[1].value == "combo" and g.online and not g.online_only)}
            enabled = (sellable - disabled) or {"online" if g.online else "offline"}  # every gym sells something
            for i, (code, mode, trainer, ai_, name, desc) in enumerate(PLAN_CATALOG):
                price = default_price(code, g.currency, prng)
                on = code in enabled
                rows.append((pid, g.id, code, name, mode.value, trainer, ai_, price, g.currency, 30, on, desc, i, when, when))
                if on:
                    g.plans[code] = (pid, mode.value, price, name)
                pid += 1
        await self.cp.copy("plans", ["id", "gym_id", "code", "name", "mode", "includes_trainer", "includes_ai", "price",
                                     "currency", "duration_days", "is_enabled", "description", "sort_order", "created_at",
                                     "updated_at"], rows)

    # -------------------------------------------------------------------------------------- staff
    async def build_staff(self) -> None:
        rng = self.rng
        n_staff = self.n(2000, len(self.gyms))
        weight = [max(1.0, g.size * 0.9 + 1.5) for g in self.gyms]
        alloc = [1] * len(self.gyms)  # at least one (a trainer) per gym
        for i in rng.choices(range(len(self.gyms)), weights=weight, k=max(0, n_staff - len(self.gyms))):
            alloc[i] += 1
        uid = await self.next_id("users", n_staff)
        eid = await self.next_id("employees", n_staff)
        sid = await self.next_id("trainer_shifts", n_staff * 5)
        u_rows, e_rows, s_rows = [], [], []
        roles, role_w = [r[0] for r in STAFF_ROLES], [r[1] for r in STAFF_ROLES]
        titles = {r[0]: r[2] for r in STAFF_ROLES}
        k = 0
        for gi, g in enumerate(self.gyms):
            for j in range(alloc[gi]):
                k += 1
                fn, ln = self.person()
                name = f"{fn} {ln}"
                email = f"lt_staff_{k:04d}@{ltenv.EMAIL_DOMAIN}"
                ip = ltenv.account_ip("staff", k)
                role = "trainer" if j == 0 else weighted(rng, roles, role_w)
                created = self.now - timedelta(days=rng.randint(60, 1100))
                photo = rng.choice(self.avatar_pool)
                ci = g.branch_city[g.branches[0]] if g.branches else g.home_city
                u_rows.append(self.user_row(uid, email, name, "staff", dob=self.dob(21, 50), city=CITIES[ci][0], lat=None,
                                            lon=None, photo=photo, ip=ip, created=created))
                mode = "online" if g.online_only else rng.choice(["offline", "offline", "both"])
                salary = Decimal(rng.randrange(18000, 70000, 500)) if role != "manager" else Decimal(rng.randrange(45000, 90000, 1000))
                hire = (created + timedelta(days=rng.randint(0, 20))).date()
                e_rows.append((eid, g.id, uid, email, name, None, role, titles[role], role == "trainer", mode,
                               "full_time" if rng.random() < 0.85 else "part_time", "IN", "INR", salary, hire, None, None,
                               "bank_transfer", None, "active", photo,
                               f"{titles[role]} with {rng.randint(2, 12)} years of experience." if role == "trainer" else None,
                               rng.sample(SPECIALTIES, k=rng.randint(2, 4)) if role == "trainer" else [], None,
                               rng.randint(1, 15), True, "verified", None, created + timedelta(days=2), "verified", None, None,
                               created, created))
                self.staff_accounts.append({"e": email, "id": uid, "ip": ip, "gym": g.id, "emp": eid,
                                            "trainer": role == "trainer"})
                g.staff.append(eid)
                if role == "trainer":
                    g.trainers.append(eid)
                    g.trainer_user[eid] = uid
                    days = sorted(rng.sample(range(6), k=rng.randint(3, 5)))  # Mon-Sat, inside every opening window
                    start_h = rng.choice([8, 9, 10, 14])
                    offline = mode != "online" and bool(g.published)
                    branch = rng.choice(g.published) if offline else None
                    for wd in days:
                        s_rows.append((sid, eid, branch, "offline" if branch else "online", wd, dtime(start_h, 0),
                                       dtime(start_h + rng.choice([4, 5]), 0), created))
                        sid += 1
                uid += 1
                eid += 1
        await self.cp.copy("users", self.USER_COLS, u_rows)
        await self.cp.copy("employees", ["id", "gym_id", "user_id", "invite_email", "full_name", "phone", "role",
                                         "role_title", "is_trainer", "work_mode", "employment_type", "country_code",
                                         "currency", "monthly_salary", "hire_date", "termination_date",
                                         "termination_reason", "payout_method", "payout_details_enc", "status",
                                         "photo_key", "bio", "specialties", "certifications", "years_experience",
                                         "is_public", "verification_status", "verification_summary", "verified_at",
                                         "id_status", "deleted_at", "purge_after", "created_at", "updated_at"], e_rows)
        await self.cp.copy("trainer_shifts", ["id", "employee_id", "branch_id", "mode", "weekday", "start_time",
                                              "end_time", "created_at"], s_rows)

    # -------------------------------------------------------------------------------------- members
    async def build_members(self) -> None:
        rng = self.rng
        from app.core.utils import bmi
        from app.services.conditions import normalize_condition

        n_members = self.n(20000, 20)
        gym_w = [max(0.4, g.size) + (1.5 if g.online else 0) for g in self.gyms]
        uid0 = await self.next_id("users", n_members)
        u_rows, prof_rows, cond_rows = [], [], []
        cond_id = await self.next_id("health_conditions", n_members * 2)
        home_gyms = rng.choices(self.gyms, weights=gym_w, k=n_members)
        for i in range(n_members):
            uid = uid0 + i
            g = home_gyms[i]
            fn, ln = self.person()
            name = f"{fn} {ln}"
            email = f"lt_member_{i + 1:05d}@{ltenv.EMAIL_DOMAIN}"
            ip = ltenv.account_ip("member", i + 1)
            minor = rng.random() < 0.02
            dob = self.dob(15, 17) if minor else self.dob(18, 58)
            branch = rng.choice(g.published) if g.published else None
            ci = g.branch_city.get(branch, g.home_city) if branch else g.home_city
            if branch and rng.random() < 0.7:
                blat, blon = g.branch_pos[branch]
                lat, lon = jitter(rng, blat, blon, 0.025)
            else:
                lat = lon = None
            height = round(rng.gauss(167, 8), 1)
            weight = round(max(42.0, min(140.0, rng.uniform(19.5, 31) * (height / 100) ** 2)), 1)
            created = self.now - timedelta(days=rng.randint(20, 700))
            photo = rng.choice(self.avatar_pool) if rng.random() < 0.3 else None
            u_rows.append(self.user_row(uid, email, name, "customer", dob=dob, city=CITIES[ci][0], lat=lat, lon=lon,
                                        photo=photo, ip=ip, created=created))
            m = MemberRec(uid, i + 1, email, name, g.id, branch, lat, lon, height, weight, minor)
            self.members.append(m)
            g.members.append(uid)
            guardian = (f"{rng.choice(FIRST)} {ln}", f"+91 9{rng.randint(100000000, 999999999)}",
                        f"guardian_{i + 1:05d}@{ltenv.EMAIL_DOMAIN}", "Parent") if minor else (None, None, None, None)
            prof_rows.append((uid, rng.choice(GOALS), rng.choice(LEVELS), height, weight, None, guardian[0], guardian[1],
                              guardian[2], guardian[3], guardian[0], guardian[1], False, None, None, None,
                              dtime(rng.choice([5, 6, 6, 7]), rng.choice([0, 15, 30, 45])), rng.random() < 0.7, None, None,
                              "self", None, None, created, created))
            if rng.random() < 0.3:
                for kind, cname in rng.sample(CONDITIONS, k=rng.randint(1, 2)):
                    cond_rows.append((cond_id, uid, kind, cname, normalize_condition(cname), None, "self", created))
                    cond_id += 1
        await self.cp.copy("users", self.USER_COLS, u_rows)
        await self.cp.copy("customer_profiles", ["user_id", "goal", "fitness_level", "height_cm", "weight_kg",
                                                 "health_notes", "guardian_name", "guardian_phone", "guardian_email",
                                                 "guardian_relation", "emergency_contact_name",
                                                 "emergency_contact_phone", "activity_suspended", "suspension_reason",
                                                 "suspended_at", "suspended_by", "wake_time", "sleep_reminders_enabled",
                                                 "last_bedtime_reminder_on", "ac_recommendation", "intake_source",
                                                 "registered_by_id", "health_embedding", "created_at", "updated_at"],
                           prof_rows)
        await self.cp.copy("health_conditions", ["id", "customer_id", "kind", "name", "tag", "notes", "source",
                                                 "created_at"], cond_rows)
        await self.build_metrics(bmi)
        await self.build_member_extras()

    async def build_metrics(self, bmi) -> None:  # noqa: ANN001
        rng = self.rng
        n_rows = 0
        mid = await self.next_id("metric_entries", 0)

        def rows() -> Iterable[tuple]:
            nonlocal mid, n_rows
            for m in self.members:
                r = rng.random()
                days = rng.randint(5, 15) if r < 0.5 else (rng.randint(16, 40) if r < 0.85 else rng.randint(41, 90))
                trend = rng.uniform(-0.06, 0.04)
                w = m.weight - trend * days
                for d in range(days, 0, -1):
                    day = self.today - timedelta(days=d)
                    w = round(w + trend + rng.uniform(-0.12, 0.12), 1)
                    mid += 1
                    n_rows += 1
                    created = datetime.combine(day, dtime(1, 30), tzinfo=UTC) + timedelta(minutes=rng.randint(0, 600))
                    yield (mid, m.id, day, m.height, w, bmi(m.height, w), "manual", "none", [], None, True, created)
                m.weight = w

        await self.cp.copy("metric_entries", ["id", "customer_id", "recorded_on", "height_cm", "weight_kg", "bmi", "source",
                                              "anomaly_level", "anomaly_reasons", "ai_note", "confirmed", "created_at"],
                           rows(), chunk=100_000)
        self.ids["metric_entries"] = mid

    async def build_member_extras(self) -> None:
        rng = self.rng
        alert_rows, task_rows, well_rows = [], [], []
        aid = await self.next_id("health_alerts", len(self.members))
        tid = await self.next_id("tasks", len(self.members) * 4)
        wid = await self.next_id("wellness_checks", len(self.members) * 2)
        for m in self.members:
            created = self.now - timedelta(days=rng.randint(1, 60))
            if rng.random() < 0.03:
                alert_rows.append((aid, m.id, None, None, rng.choice(["info", "warning"]), "Weight changed faster than usual",
                                   "Your weight changed by more than 1.5 kg in a week.", "Keep logging daily; talk to your trainer.",
                                   "rule", False, None if rng.random() < 0.6 else created + timedelta(hours=5), created))
                aid += 1
            for title, kind in rng.sample(TASKS, k=rng.randint(2, 4)):
                due = self.now + timedelta(hours=rng.randint(-24 * 10, 24 * 14))
                done = due < self.now and rng.random() < 0.7
                task_rows.append((tid, m.id, title, None, kind, due, due - timedelta(hours=1) if kind == "reminder" else None,
                                  False, due + timedelta(minutes=30) if done else None,
                                  due - timedelta(hours=1) if kind == "reminder" and due < self.now else None,
                                  rng.choice(["user", "user", "ai", "system"]), due - timedelta(days=3), due - timedelta(days=3)))
                tid += 1
            if rng.random() < 0.15:
                for _ in range(rng.randint(1, 2)):
                    answers = [rng.randint(0, 4) for _ in range(4)]
                    score = answers[0] + (4 - answers[1]) + (4 - answers[2]) + answers[3]
                    level = "low" if score <= 5 else ("moderate" if score <= 10 else "high")
                    well_rows.append((wid, m.id, answers, score, ["none"], None, level,
                                      "Keep a regular sleep schedule and take short walks between work blocks.", None,
                                      self.now - timedelta(days=rng.randint(1, 90))))
                    wid += 1
        await self.cp.copy("health_alerts", ["id", "customer_id", "metric_entry_id", "wellness_check_id", "level", "title",
                                             "message", "recommendation", "source", "guardian_notified",
                                             "acknowledged_at", "created_at"], alert_rows)
        await self.cp.copy("tasks", ["id", "user_id", "title", "notes", "kind", "due_at", "remind_at", "all_day",
                                     "completed_at", "reminded_at", "created_by", "created_at", "updated_at"], task_rows)
        await self.cp.copy("wellness_checks", ["id", "customer_id", "pss_answers", "pss_score", "symptoms",
                                               "face_analysis", "stress_level", "recommendations", "ai_provider",
                                               "created_at"], well_rows)

    # -------------------------------------------------------------------------------------- subscriptions + payments
    async def build_subscriptions(self) -> None:
        rng = self.rng
        sub_id = await self.next_id("subscriptions", len(self.members) * 2)
        tx_id = await self.next_id("transactions", len(self.members) * 2)
        tc_id = await self.next_id("trainer_clients", len(self.members))
        s_rows, t_rows, tc_rows = [], [], []

        def add(m: MemberRec, g: GymRec, code: str, status: str, starts: date) -> bool:
            nonlocal sub_id, tx_id, tc_id
            pid, mode, price, pname = g.plans[code]
            branch = m.branch if mode != "online" else None
            if mode != "online" and branch is None:
                return False
            trainer = rng.choice(g.trainers) if "trainer" in code and g.trainers else None
            when = datetime.combine(starts, dtime(5, 0), tzinfo=UTC) + timedelta(minutes=rng.randint(0, 600))
            ends = starts + timedelta(days=30)
            channel = "online_checkout" if mode == "online" else "branch_desk_otp"
            s_rows.append((sub_id, m.id, g.id, pid, branch, trainer, status, channel, price, g.currency, starts, ends, when,
                           None, True if branch else None, when if branch else None, m.lat, m.lon, None, 0, [],
                           self.user_ip[m.id], when, when))
            t_rows.append((tx_id, g.id, "incoming", "subscription", "success", price, g.currency, f"{pname} - {m.name}"[:255],
                           m.name, "test", f"LT-SUB-{sub_id}", None, None, None, m.id, None, sub_id, None, None, None, None,
                           None, {}, when, when, when))
            if trainer and status == "active":
                tc_rows.append((tc_id, trainer, m.id, sub_id, "online" if mode == "online" else "offline", True, when))
                tc_id += 1
            sub_id += 1
            tx_id += 1
            return True

        for m in self.members:
            g = self.gym_by_id[m.gym]
            codes = list(g.plans) if m.branch else [c for c in g.plans if g.plans[c][1] == "online"]
            m.has_active = False
            if not codes:
                continue
            if rng.random() < 0.95:  # 5 % of the members have no active plan (shop / browsing only)
                m.has_active = add(m, g, rng.choice(codes), "active", self.today - timedelta(days=rng.randint(0, 13)))
            if rng.random() < 0.4:
                code = rng.choice(codes)
                add(m, g, code, "expired", self.today - timedelta(days=rng.randint(45, 330)))
        await self.cp.copy("subscriptions", ["id", "customer_id", "gym_id", "plan_id", "branch_id", "trainer_id", "status",
                                             "channel", "price", "currency", "starts_on", "ends_on", "activated_at",
                                             "cancelled_at", "physically_present", "otp_verified_at", "customer_latitude",
                                             "customer_longitude", "distance_km", "fraud_score", "fraud_flags",
                                             "created_ip", "created_at", "updated_at"], s_rows)
        await self.cp.copy("transactions", self.TX_COLS, t_rows)
        await self.cp.copy("trainer_clients", ["id", "employee_id", "customer_id", "subscription_id", "mode", "active",
                                               "created_at"], tc_rows)
        self.ids["transactions"] = tx_id - 1

    TX_COLS = ["id", "gym_id", "direction", "category", "status", "amount", "currency", "description", "party_label",
               "provider", "provider_order_id", "provider_payment_id", "provider_reference", "payment_session_id",
               "customer_id", "employee_id", "subscription_id", "salary_statement_id", "created_by_id", "proof_key",
               "proof_check", "proof_verified", "meta", "completed_at", "created_at", "updated_at"]

    # -------------------------------------------------------------------------------------- identity documents
    async def build_identity(self) -> None:
        rng = self.rng
        ids = list(self.user_names)
        did = await self.next_id("identity_documents", len(ids))
        rows = []
        for uid in ids:
            when = self.now - timedelta(days=rng.randint(10, 600))
            rows.append((did, uid, None, rng.choice(["aadhaar", "national_id", "driving_license", "passport"]), "IN",
                         self.id_front_key, None, "verified", f"••••{uid % 10000:04d}", None, self.user_names[uid], None, None,
                         ["seed"], None, [{"check": "Load-test document", "passed": True,
                                           "detail": "Seeded specimen - verification skipped"}], 1.0,
                         "Load-test identity document.", None, 1, uid, None, None, when, when, when))
            did += 1
        await self.cp.copy("identity_documents", ["id", "user_id", "employee_id", "doc_type", "country_code", "front_key",
                                                  "back_key", "status", "id_number_masked", "id_number_hash",
                                                  "name_on_id", "dob_on_id", "expires_on", "engines", "engine_results",
                                                  "checks", "consensus_score", "ai_summary", "rejection_reason",
                                                  "attempts", "uploaded_by_id", "reviewed_by_id", "reviewed_at",
                                                  "verified_at", "created_at", "updated_at"], rows)

    # -------------------------------------------------------------------------------------- products
    async def build_products(self) -> None:
        rng = self.rng
        from app.ai.embeddings import embed
        from app.core.utils import slugify
        from app.models.shop import ProductCategory
        from app.services.shop import embedding_text

        n_products = self.n(20000, 20)
        gym_w = [max(0.6, g.size ** 0.8) for g in self.gyms]
        tot = sum(gym_w)
        per_gym = [max(4, int(round(n_products * w / tot))) for w in gym_w]
        keys = list(self.catalogue)
        pid = await self.next_id("products", sum(per_gym))
        img_id = await self.next_id("product_images", sum(per_gym) * 3)
        inv_id = await self.next_id("inventory_movements", sum(per_gym) * 4)
        self.ids["inventory_movements"] = inv_id - 1
        emb_cache: dict[str, str] = {}
        p_rows, i_rows, m_rows = [], [], []
        for g, count in zip(self.gyms, per_gym):
            slugs: set[str] = set()
            for j in range(count):
                key = keys[j % len(keys)] if j < len(keys) else rng.choice(keys)
                c = self.catalogue[key]
                cat = c["category"]
                variant = rng.choice(VARIANTS.get(cat, ["Standard"]))
                brand = c["brand"] if rng.random() < 0.4 else rng.choice(BRANDS.get(cat, [c["brand"]]))
                base = c["name"]
                name = f"{base} - {variant}"[:160]
                slug = slugify(name, max_len=160)
                n = 2
                while slug in slugs:
                    slug = f"{slugify(name, max_len=150)}-{n}"
                    n += 1
                slugs.add(slug)
                price = nice_price(float(c["price"]) * rng.uniform(0.75, 1.35))
                mrp = None
                if c.get("mrp") and rng.random() < 0.7:
                    mrp = max(price, nice_price(float(price) * rng.uniform(1.05, 1.35)))
                r = rng.random()
                stock = 0 if r < 0.07 else (rng.randint(1, 5) if r < 0.18 else rng.randint(6, 220))
                active = rng.random() > 0.04
                fulfillment = c["fulfillment"] if g.published or c["fulfillment"] == "delivery" else "delivery"
                text = embedding_text(SimpleNamespace(name=name, brand=brand, category=ProductCategory(cat),
                                                      description=c["description"], ingredients=c.get("ingredients")))
                vec = emb_cache.get(text)
                if vec is None:
                    vec = emb_cache[text] = "[" + ",".join(format(x, ".6g") for x in embed(text)) + "]"
                created = self.now - timedelta(days=rng.randint(1, 500))
                p_rows.append((pid, g.id, name, slug, cat, brand, c["description"], price, mrp, "INR", Decimal(c["tax"]),
                               stock, c["threshold"], f"{c['sku']}-{g.id}-{j}"[:64], active, c["supplement"],
                               c["age_restricted"], list(c["allergens"]), list(c["cautions"]), c.get("ingredients"),
                               c.get("nutrition"), fulfillment, None, vec, created, created))
                imgs = self.product_images.get(c["sku"]) or []
                for s, ik in enumerate(imgs[: rng.randint(1, max(1, min(3, len(imgs))))]):
                    i_rows.append((img_id, pid, ik, name, s, created))
                    img_id += 1
                if stock:
                    self.ids["inventory_movements"] += 1
                    m_rows.append((self.ids["inventory_movements"], pid, stock + rng.randint(0, 40), "restock", None,
                                   g.owner_id, "Opening stock", created))
                self.products[pid] = ProductRec(pid, g.id, name, slug, price, Decimal(c["tax"]), fulfillment, stock, active)
                if active:
                    g.products.append(pid)
                pid += 1
        await self.cp.copy("products", ["id", "gym_id", "name", "slug", "category", "brand", "description", "price",
                                        "compare_at_price", "currency", "tax_rate", "stock", "low_stock_threshold", "sku",
                                        "is_active", "is_supplement", "age_restricted", "allergens", "caution_tags",
                                        "ingredients", "nutrition_facts", "fulfillment", "pickup_branch_id", "embedding",
                                        "created_at", "updated_at"], p_rows, chunk=5_000)
        await self.cp.copy("product_images", ["id", "product_id", "image_key", "alt", "sort_order", "created_at"], i_rows)
        await self.cp.copy("inventory_movements", ["id", "product_id", "change", "reason", "order_id", "actor_id", "note",
                                                   "created_at"], m_rows)
        log(f"  embeddings computed: {len(emb_cache)} distinct product texts")

    # -------------------------------------------------------------------------------------- orders
    async def build_orders(self) -> None:
        rng = self.rng
        from app.services.shop import build_quote, delivery_fee

        n_orders = self.n(20000, 20)
        statuses = ["completed", "paid", "processing", "ready_for_pickup", "shipped", "cancelled", "pending_payment",
                    "payment_failed", "refund_requested", "partially_refunded", "refunded"]
        weights = [58, 6, 6, 4, 4, 8, 3, 2, 3, 2, 4]
        oid0 = await self.next_id("shop_orders", n_orders)
        item_id = await self.next_id("shop_order_items", n_orders * 4)
        audit_id = await self.next_id("audit_logs", n_orders * 6)
        ref_id = await self.next_id("refunds", n_orders)
        rev_id = await self.next_id("product_reviews", n_orders)
        o_rows, it_rows, a_rows, t_rows, r_rows, m_rows, rv_rows = [], [], [], [], [], [], []
        reviewed: set[tuple[int, int]] = set()
        shoppers = [m for m in self.members if self.gym_by_id[m.gym].products]
        for k in range(n_orders):
            oid = oid0 + k
            m = rng.choice(shoppers)
            g = self.gym_by_id[m.gym]
            if rng.random() < 0.2:
                other = rng.choice(self.gyms)
                if other.products:
                    g = other
            picks = rng.sample(g.products, k=min(len(g.products), rng.choice([1, 1, 1, 2, 2, 3])))
            prods = [self.products[p] for p in picks]
            qtys = [rng.choice([1, 1, 1, 2, 3]) for _ in prods]
            status = weighted(rng, statuses, weights)
            can_pickup = bool(g.published) and all(p.fulfillment in ("pickup", "both") for p in prods)
            can_delivery = all(p.fulfillment in ("delivery", "both") for p in prods)
            if status == "ready_for_pickup" and not can_pickup:
                status = "completed"
            if status == "shipped" and not can_delivery:
                status = "completed"
            pickup = can_pickup and (status == "ready_for_pickup" or (status != "shipped" and (not can_delivery or rng.random() < 0.55)))
            if not pickup and not can_delivery:
                pickup = True  # nothing deliverable and no branch: keep as a pickup order at the gym (rare)
            sub = sum((p.price * q for p, q in zip(prods, qtys)), Decimal(0))
            quote = build_quote([(p.id, p.name, p.price, p.tax, q) for p, q in zip(prods, qtys)], g.currency,
                                shipping=Decimal(0) if pickup else delivery_fee(g.currency, sub))
            age_days = min(180.0, rng.expovariate(1 / 35.0))
            if status == "pending_payment":
                age_days = rng.uniform(0.01, 0.4)
            placed = self.now - timedelta(days=age_days, minutes=rng.randint(0, 600))
            paid_statuses = {"completed", "paid", "processing", "ready_for_pickup", "shipped", "refund_requested",
                             "partially_refunded", "refunded"}
            cancelled_paid = status == "cancelled" and rng.random() < 0.5
            paid = status in paid_statuses or cancelled_paid
            paid_at = placed + timedelta(minutes=2) if paid else None
            done_statuses = {"completed", "refund_requested", "partially_refunded", "refunded"}
            completed_at = min(placed + timedelta(days=rng.uniform(1, 4)), self.now - timedelta(hours=1)) if status in done_statuses else None
            cancelled_at = placed + timedelta(hours=rng.uniform(1, 30)) if status == "cancelled" else None
            branch = rng.choice(g.published) if pickup and g.published else None
            address = None if pickup else {"name": m.name, "phone": "", "line1": f"{rng.randint(1, 400)}, {rng.choice(['2nd Main', '5th Cross', 'Lake View Road'])}",
                                           "city": CITIES[g.branch_city.get(m.branch, g.home_city) if m.branch else g.home_city][0],
                                           "postal_code": "560001", "country_code": "IN"}
            refunded_total = Decimal(0)
            number = f"IPO-{placed.year}-L{k + 1:05d}"
            items = []
            for p, q, ql in zip(prods, qtys, quote.lines):
                items.append([item_id, oid, p.id, p.name[:160], ql.unit_price, ql.tax_rate, q, ql.total, 0])
                item_id += 1
            if status in ("partially_refunded", "refunded", "refund_requested"):
                first = items[0]
                qty = first[6] if status != "partially_refunded" or first[6] == 1 else 1
                amount = (first[7] / first[6] * qty).quantize(Decimal("0.01"))
                if status == "partially_refunded" and len(items) == 1 and qty == first[6]:
                    status = "refunded"
                done = status != "refund_requested"
                when = (completed_at or placed) + timedelta(days=rng.uniform(1, 5))
                when = min(when, self.now - timedelta(minutes=10))
                tx = None
                if done:
                    first[8] = qty
                    refunded_total = amount
                    self.ids["transactions"] += 1
                    tx = self.ids["transactions"]
                    t_rows.append((tx, g.id, "outgoing", "refund", "success", amount, g.currency,
                                   f"Refund for shop order {number}", f"{m.name} <{m.email}>", "test", None, None,
                                   f"LT-RF-{oid}", None, m.id, None, None, None, g.owner_id, None, None, None,
                                   {"shop_order_id": oid, "order_number": number, "refund_id": ref_id,
                                    "kind": "shop_refund", "simulated": True}, when, when, when))
                r_rows.append((ref_id, oid, amount, rng.choice(["Damaged on arrival", "Wrong size", "Changed my mind",
                                                                "Flavour not as described"]),
                               [{"order_item_id": first[0], "quantity": qty}], rng.random() < 0.5,
                               "completed" if done else "requested", m.id, g.owner_id if done else None,
                               "Approved - refunded to the original payment method." if done else None,
                               f"LT-RF-{oid}" if done else None, tx, when if done else None, when - timedelta(hours=6), when))
                a_rows.append((audit_id, m.id, g.id, "shop.refund.request", "shop_order", oid, {"amount": str(amount)},
                               self.user_ip[m.id], when - timedelta(hours=6)))
                audit_id += 1
                if done:
                    a_rows.append((audit_id, g.owner_id, g.id, "shop.refund.approve", "shop_order", oid,
                                   {"amount": str(amount)}, self.user_ip[g.owner_id], when))
                    audit_id += 1
                ref_id += 1
            o_rows.append((oid, number, g.id, m.id, f"{m.name} <{m.email}>"[:160], status, "pickup" if pickup else "delivery",
                           branch, address, quote.subtotal, quote.tax_total, quote.shipping_fee, quote.discount_total,
                           quote.total, refunded_total, g.currency, None, "test", f"LT-PAY-{oid}", None, paid_at,
                           cancelled_at, completed_at, None, self.user_ip[m.id], placed,
                           completed_at or cancelled_at or paid_at or placed))
            for it in items:
                it_rows.append(tuple(it))
            if paid:
                self.ids["transactions"] += 1
                t_rows.append((self.ids["transactions"], g.id, "incoming", "shop_sale", "success", quote.total, g.currency,
                               f"Shop order {number} · {g.name}"[:255], f"{m.name} <{m.email}>"[:160], "test", number,
                               f"LT-PAY-{oid}", None, None, m.id, None, None, None, None, None, None, None,
                               {"shop_order_id": oid, "order_number": number, "kind": "shop_sale"}, paid_at, paid_at, paid_at))
                for it in items:
                    self.ids["inventory_movements"] += 1
                    m_rows.append((self.ids["inventory_movements"], it[2], -it[6], "sale", oid, None, None, paid_at))
            # status events (the order timeline reads them from audit_logs)
            chain: list[tuple[str, str, float]] = []
            if paid and status not in ("paid",):
                chain.append(("paid", "processing", 1))
                if status in ("ready_for_pickup", "shipped", "completed", "refund_requested", "partially_refunded", "refunded") or cancelled_paid:
                    if status != "cancelled":
                        chain.append(("processing", "ready_for_pickup" if pickup else "shipped", 20))
                if status in done_statuses:
                    chain.append(("ready_for_pickup" if pickup else "shipped", "completed", 40))
            for frm, to, hours in chain:
                when = min(placed + timedelta(hours=hours + rng.uniform(0, 6)), self.now - timedelta(minutes=5))
                a_rows.append((audit_id, g.owner_id, g.id, f"shop.order.{to}", "shop_order", oid,
                               {"from": frm, "to": to, "note": None}, self.user_ip[g.owner_id], when))
                audit_id += 1
            if status == "cancelled":
                a_rows.append((audit_id, m.id, g.id, "shop.order.cancel", "shop_order", oid,
                               {"from": "paid" if cancelled_paid else "pending_payment", "to": "cancelled"},
                               self.user_ip[m.id], cancelled_at))
                audit_id += 1
            if status == "completed" and rng.random() < 0.25:
                p = prods[0]
                if (p.id, m.id) not in reviewed:
                    reviewed.add((p.id, m.id))
                    written = min(completed_at + timedelta(days=1), self.now - timedelta(minutes=30))
                    rv_rows.append((rev_id, p.id, m.id, rng.choice([5, 5, 4, 4, 4, 3, 2]),
                                    rng.choice(["Great value", "Does the job", "Tastes good", "Good quality", "Okay"]),
                                    rng.choice(["Would buy again.", "Arrived on time and well packed.", "Mixes well.",
                                                "Fits as expected.", None]), False, written, written))
                    rev_id += 1
            m.orders.append(oid)
        await self.cp.copy("shop_orders", ["id", "order_number", "gym_id", "customer_id", "customer_label", "status",
                                           "fulfillment", "pickup_branch_id", "shipping_address", "subtotal", "tax_total",
                                           "shipping_fee", "discount_total", "total", "refunded_total", "currency",
                                           "coupon_code", "payment_provider", "provider_order_id", "payment_session_id",
                                           "paid_at", "cancelled_at", "completed_at", "notes", "created_ip", "created_at",
                                           "updated_at"], o_rows)
        await self.cp.copy("shop_order_items", ["id", "order_id", "product_id", "product_name", "unit_price", "tax_rate",
                                                "quantity", "line_total", "refunded_quantity"], it_rows)
        await self.cp.copy("transactions", self.TX_COLS, t_rows)
        await self.cp.copy("refunds", ["id", "order_id", "amount", "reason", "items", "restock", "status",
                                       "requested_by_id", "decided_by_id", "decision_note", "provider_refund_id",
                                       "transaction_id", "processed_at", "created_at", "updated_at"], r_rows)
        await self.cp.copy("inventory_movements", ["id", "product_id", "change", "reason", "order_id", "actor_id", "note",
                                                   "created_at"], m_rows)
        await self.cp.copy("product_reviews", ["id", "product_id", "user_id", "rating", "title", "body", "is_hidden",
                                               "created_at", "updated_at"], rv_rows)
        self.audit_rows = a_rows
        self.ids["audit_logs"] = audit_id - 1

    # -------------------------------------------------------------------------------------- chat
    async def build_chat(self) -> None:
        rng = self.rng
        n_threads, n_messages = self.n(5000, 10), self.n(100000, 50)
        thid = await self.next_id("chat_threads", n_threads)
        msg_id = await self.next_id("chat_messages", n_messages + n_threads * 2)
        candidates = [m for m in self.members if m.has_active]
        seen: set[tuple[int, int, int | None]] = set()
        threads = []
        tries = 0
        while len(threads) < n_threads and tries < n_threads * 5:
            tries += 1
            m = rng.choice(candidates)
            g = self.gym_by_id[m.gym]
            emp = rng.choice(g.trainers) if g.trainers and rng.random() < 0.6 else None
            key = (g.id, m.id, emp)
            if key in seen:
                continue
            seen.add(key)
            threads.append((m, g, emp))
        sizes = []
        for _ in threads:
            r = rng.random()
            sizes.append(rng.randint(5, 20) if r < 0.7 else (rng.randint(21, 60) if r < 0.95 else rng.randint(61, 250)))
        factor = n_messages / max(1, sum(sizes))
        sizes = [max(1, int(round(s * factor))) for s in sizes]
        t_rows, m_rows = [], []
        for i, ((m, g, emp), size) in enumerate(zip(threads, sizes)):
            tid = thid + i
            start = self.now - timedelta(days=rng.uniform(1, 120))
            step = (self.now - start).total_seconds() / (size + 1)
            staff_user = g.trainer_user.get(emp) if emp else g.owner_id
            staff_role = "staff" if emp else "owner"
            when, body = start, ""
            for j in range(size):
                when = when + timedelta(seconds=step * rng.uniform(0.3, 1.0))
                mine = j % 2 == 0 or rng.random() < 0.3
                body = rng.choice(MEMBER_LINES if mine else STAFF_LINES)
                msg_id += 1
                m_rows.append((msg_id, tid, m.id if mine else staff_user, "customer" if mine else staff_role, body, when))
            status = "open" if rng.random() < 0.92 else "closed"
            t_rows.append((tid, g.id, m.id, emp, rng.choice(THREAD_SUBJECTS), status, when, body[:140],
                           rng.choice([0, 0, 0, 1, 2]), rng.choice([0, 0, 1, 3]), start, when))
            if status == "open":
                m.threads.append(tid)
            if emp:
                self.threads_by_emp[emp].append(tid)
            else:
                self.threads_by_gym_team[g.id].append(tid)
        await self.cp.copy("chat_threads", ["id", "gym_id", "customer_id", "employee_id", "subject", "status",
                                            "last_message_at", "last_message_preview", "customer_unread", "staff_unread",
                                            "created_at", "updated_at"], t_rows)
        await self.cp.copy("chat_messages", ["id", "thread_id", "sender_id", "sender_role", "body", "created_at"],
                           m_rows, chunk=100_000)

    # -------------------------------------------------------------------------------------- notifications
    async def build_notifications(self) -> None:
        rng = self.rng
        n_total = self.n(100000, 50)
        nid = await self.next_id("notifications", n_total + 10)
        links = {"subs": "/me/subscriptions", "me": "/me", "chat": "/chat", "health": "/me/health", "account": "/account",
                 "orders": "/shop/orders", "calendar": "/calendar", "videos": "/me/videos"}
        targets: list[tuple[int, int, str]] = []  # (user id, count, kind)
        heavy = set(rng.sample(range(len(self.members)), k=max(1, len(self.members) // 100)))
        for i, m in enumerate(self.members):
            targets.append((m.id, rng.randint(150, 300) if i in heavy else rng.randint(1, 5), "member"))
        for o in self.owners:
            targets.append((o["id"], rng.randint(20, 150), "owner"))
        for s in self.staff_accounts:
            targets.append((s["id"], rng.randint(3, 15), "staff"))
        factor = n_total / max(1, sum(t[1] for t in targets))
        rows = []
        for uid, count, kind in targets:
            for _ in range(max(1, int(round(count * factor)))):
                if kind == "owner":
                    cat, sev, title, body, link = rng.choice(OWNER_NOTIFY)
                else:
                    cat, sev, title, body, lk = rng.choice(NOTIFY)
                    link = links[lk]
                created = self.now - timedelta(minutes=rng.randint(5, 60 * 24 * 60))
                read = created + timedelta(minutes=rng.randint(1, 3000)) if rng.random() < 0.75 else None
                if read and read > self.now:
                    read = self.now - timedelta(minutes=1)
                nid += 1
                rows.append((nid, uid, title, body, cat, sev, link, read, True, created))
        await self.cp.copy("notifications", ["id", "user_id", "title", "body", "category", "severity", "link", "read_at",
                                             "emailed", "created_at"], rows, chunk=100_000)

    # -------------------------------------------------------------------------------------- jobs
    async def build_jobs(self) -> None:
        rng = self.rng
        n_jobs = self.n(2000, 5)
        gym_w = [max(0.3, g.size) for g in self.gyms]
        jid = await self.next_id("job_postings", n_jobs)
        eqid = await self.next_id("job_enquiries", n_jobs * 5)
        j_rows, e_rows = [], []
        self.open_jobs: list[int] = []
        for k in range(n_jobs):
            g = weighted(rng, self.gyms, gym_w)
            role, title, etype, (lo, hi) = rng.choice(JOB_TEMPLATES)
            r = rng.random()
            if role == "software_developer" or not g.branches or r < 0.12:
                branch, location = None, rng.choice(["Remote", "Remote (India)", "Remote - IST hours"])
            elif r < 0.30:
                branch, location = None, CITIES[g.home_city][0]
            else:
                branch = rng.choice(g.published or g.branches)
                location = f"{CITIES[g.branch_city[branch]][0]} - {rng.choice(CITIES[g.branch_city[branch]][7])}"
            is_open = rng.random() < 0.85
            created = self.now - timedelta(days=rng.uniform(0, 90))
            smin = Decimal(rng.randrange(lo, hi, 500))
            smax = smin + Decimal(rng.randrange(2000, 15000, 500))
            j_rows.append((jid, g.id, branch, title, role, etype, location[:160], smin, smax, "INR", "month",
                           "2+ years of experience; good communication; certification preferred.",
                           f"Join {g.name} as a {title.lower()} - friendly team, steady members, growth path.", is_open,
                           created, created))
            if is_open:
                self.open_jobs.append(jid)
            n_enq = rng.randint(1, 4) if rng.random() < 0.4 else 0
            self.jobs_by_gym[g.id].append((jid, n_enq))
            if n_enq:
                for e in range(n_enq):
                    fn, ln = self.person()
                    e_rows.append((eqid, jid, f"{fn} {ln}", f"applicant_{eqid}@{ltenv.EMAIL_DOMAIN}", None,
                                   "I'd love to join your team - I have relevant experience and can start soon.", None,
                                   rng.choice(["new", "new", "reviewed", "contacted"]), ltenv.random_ip(ltenv.VISITOR_NET, rng),
                                   created + timedelta(days=rng.uniform(0, 5))))
                    eqid += 1
            jid += 1
        await self.cp.copy("job_postings", ["id", "gym_id", "branch_id", "title", "role", "employment_type", "location",
                                            "salary_min", "salary_max", "currency", "salary_period", "requirements",
                                            "description", "is_open", "created_at", "updated_at"], j_rows)
        await self.cp.copy("job_enquiries", ["id", "job_id", "name", "email", "phone", "message", "resume_key", "status",
                                             "ip", "created_at"], e_rows)

    # -------------------------------------------------------------------------------------- audit trail, money, misc
    async def build_audit_and_money(self) -> None:
        rng = self.rng
        target = self.n(50000, 50)
        a_rows = list(self.audit_rows)
        audit_id = self.ids["audit_logs"]
        actions = [("staff.update", "employee"), ("staff.shift.add", "employee"), ("branch.update", "branch"),
                   ("product.update", "product"), ("product.stock", "product"), ("plan.update", "plan"),
                   ("customer.register", "user"), ("gym.update", "gym"), ("chat.close", "chat_thread"),
                   ("coupon.create", "coupon"), ("job.create", "job_posting"), ("payroll.generate", "salary_statement")]
        while len(a_rows) < target:
            g = rng.choice(self.gyms)
            action, entity = rng.choice(actions)
            audit_id += 1
            a_rows.append((audit_id, g.owner_id, g.id, action, entity, rng.randint(1, 50000), {"via": "web"},
                           self.user_ip[g.owner_id], self.now - timedelta(days=rng.uniform(0, 300))))
        await self.cp.copy("audit_logs", ["id", "actor_id", "gym_id", "action", "entity_type", "entity_id", "detail", "ip",
                                          "created_at"], a_rows, chunk=100_000)

        t_rows = []
        tx = self.ids["transactions"]
        expenses = ["Electricity bill", "Equipment maintenance", "Rent", "Cleaning supplies", "Internet", "Marketing"]
        for g in self.gyms:
            plans = list(g.plans.values())
            per_month = max(2, int(g.size * rng.uniform(1.5, 3.0)))
            for month in range(1, 13):
                for _ in range(rng.randint(max(1, per_month // 2), per_month)):
                    pid, mode, price, pname = rng.choice(plans)
                    when = self.now - timedelta(days=30 * month - rng.uniform(0, 29))
                    tx += 1
                    t_rows.append((tx, g.id, "incoming", "subscription", "success", price, g.currency,
                                   f"{pname} - walk-in member", "Member", "test", f"LT-WALK-{tx}", None, None, None, None,
                                   None, None, None, None, None, None, None, {}, when, when, when))
                for _ in range(rng.randint(1, 3)):
                    when = self.now - timedelta(days=30 * month - rng.uniform(0, 29))
                    tx += 1
                    t_rows.append((tx, g.id, "outgoing", "expense", "success", money(rng.uniform(1500, 40000) * max(1, g.size ** 0.5)),
                                   g.currency, rng.choice(expenses), None, "manual", None, None, None, None, None, None, None,
                                   None, g.owner_id, None, None, None, {}, when, when, when))
        emp_rows = await self.conn.fetch("SELECT id, gym_id, full_name, monthly_salary FROM employees WHERE status = 'active'")
        for e in emp_rows:
            for month in range(1, 4):
                when = datetime.combine(self.today.replace(day=1) - timedelta(days=30 * (month - 1)), dtime(6, 0), tzinfo=UTC)
                tx += 1
                t_rows.append((tx, e["gym_id"], "outgoing", "salary", "success", e["monthly_salary"], "INR",
                               f"Salary - {e['full_name']}"[:255], e["full_name"], "manual", None, None,
                               f"UTR{tx:010d}", None, None, e["id"], None, None, None, None, None, None, {}, when, when, when))
        await self.cp.copy("transactions", self.TX_COLS, t_rows, chunk=100_000)
        self.ids["transactions"] = tx

        c_rows, w_rows, cart_rows = [], [], []
        cid = await self.next_id("coupons", len(self.gyms) * 3)
        for g in self.gyms:
            for code in rng.sample(["WELCOME10", "FIT15", "SUMMER200", "PULSE5", "NEWYEAR20", "STRONG100"], k=rng.randint(1, 3)):
                pct = code.startswith(("WELCOME", "FIT", "PULSE", "NEWYEAR"))
                c_rows.append((cid, g.id, code, int("".join(ch for ch in code if ch.isdigit()) or 10) if pct else None,
                               None if pct else Decimal("".join(ch for ch in code if ch.isdigit())),
                               Decimal("999") if not pct else None, self.now + timedelta(days=rng.randint(10, 120)),
                               rng.choice([None, 100, 500]), rng.randint(0, 40), True, self.now, self.now))
                cid += 1
        wid = await self.next_id("wishlist_items", len(self.members) * 6)
        crt = await self.next_id("cart_items", len(self.members) * 2)
        for m in self.members:
            g = self.gym_by_id[m.gym]
            if g.products and rng.random() < 0.25:
                for p in rng.sample(g.products, k=min(len(g.products), rng.randint(1, 6))):
                    w_rows.append((wid, m.id, p, self.now - timedelta(days=rng.uniform(0, 60))))
                    self.wishlist_by_user[m.id].add(p)
                    wid += 1
            if g.products and rng.random() < 0.08:
                for p in rng.sample(g.products, k=min(len(g.products), rng.randint(1, 2))):
                    when = self.now - timedelta(days=rng.uniform(0, 7))
                    cart_rows.append((crt, m.id, p, 1, when, when))
                    crt += 1
        await self.cp.copy("coupons", ["id", "gym_id", "code", "percent_off", "amount_off", "min_order_total", "valid_until",
                                       "max_uses", "used_count", "is_active", "created_at", "updated_at"], c_rows)
        await self.cp.copy("wishlist_items", ["id", "user_id", "product_id", "created_at"], w_rows)
        await self.cp.copy("cart_items", ["id", "user_id", "product_id", "quantity", "created_at", "updated_at"], cart_rows)

    # -------------------------------------------------------------------------------------- id helper
    async def fresh_id(self, table: str, count: int) -> int:
        """First id of a block of ``count`` ids above everything already in ``table`` (COPYed rows of this
        transaction included) - earlier steps may have used a few more ids than they reserved (rounding)."""
        db_max = int(await self.conn.fetchval(f"SELECT coalesce(max(id), 0) FROM {table}"))
        self.ids[table] = max(self.ids.get(table, 0), db_max)
        start = self.ids[table] + 1
        self.ids[table] += count
        return start

    # -------------------------------------------------------------------------------------- heavy tail
    async def build_heavy_tail(self) -> None:
        """The worst cases the pages must cope with - write_outputs puts these accounts FIRST in accounts.json (indexes
        0-3 of each pool, then every 50th member), so every load step uses them:
          * the owners of the 3 largest chains: 20-50k notifications each (90 % read) and +1,000 team chat threads
          * ~2 % of the members: 200-400 calendar tasks (<= 100 open; the cap is 500) and 50-80 wishlist items
          * 4 trainers of the largest chains with 200 clients each"""
        rng = self.rng
        n_heavy_owners = min(3, len(self.owners))
        order = sorted(range(len(self.owners)),
                       key=lambda j: -sum(self.gym_by_id[g].size for g in self.owners[j]["gyms"]))
        self.heavy_owner_idx = order[:n_heavy_owners]
        # ---- notifications of the heavy owners
        counts = [rng.randint(20_000, 50_000) for _ in self.heavy_owner_idx] if self.scale >= 0.5 else \
            [self.n(30_000, 50) for _ in self.heavy_owner_idx]
        nid = await self.fresh_id("notifications", sum(counts) + 10)
        rows = []
        for j, cnt in zip(self.heavy_owner_idx, counts):
            uid = self.owners[j]["id"]
            for _ in range(cnt):
                cat, sev, title, body, link = rng.choice(OWNER_NOTIFY)
                created = self.now - timedelta(minutes=rng.randint(5, 60 * 24 * 180))
                read = created + timedelta(minutes=rng.randint(1, 600)) if rng.random() < 0.9 else None
                if read and read > self.now:
                    read = self.now - timedelta(minutes=1)
                rows.append((nid, uid, title, body, cat, sev, link, read, True, created))
                nid += 1
        await self.cp.copy("notifications", ["id", "user_id", "title", "body", "category", "severity", "link", "read_at",
                                             "emailed", "created_at"], rows, chunk=100_000)
        COUNTS["heavy_owner_notifications"] = len(rows)
        # ---- +1,000 team chat threads in each heavy owner's largest gym (one team thread per customer and gym)
        t_rows, m_rows = [], []
        n_extra = self.n(1000, 5)
        existing = {(r["gym_id"], r["customer_id"]) for r in await self.conn.fetch(
            "SELECT gym_id, customer_id FROM chat_threads WHERE employee_id IS NULL")}
        tid = await self.fresh_id("chat_threads", n_extra * n_heavy_owners + 10)
        mid = await self.fresh_id("chat_messages", (n_extra * 8 + 10) * n_heavy_owners)
        for j in self.heavy_owner_idx:
            g = max((self.gym_by_id[x] for x in self.owners[j]["gyms"]), key=lambda x: len(x.members))
            pool = [u for u in g.members if (g.id, u) not in existing]
            rng.shuffle(pool)
            members = {m.id: m for m in self.members if m.gym == g.id}
            for uid in pool[:n_extra]:
                m = members.get(uid)
                if m is None:
                    continue
                start = self.now - timedelta(days=rng.uniform(1, 200))
                size = rng.randint(2, 8)
                step = (self.now - start).total_seconds() / (size + 1)
                when, body = start, ""
                for k in range(size):
                    when = when + timedelta(seconds=step * rng.uniform(0.3, 1.0))
                    mine = k % 2 == 0
                    body = rng.choice(MEMBER_LINES if mine else STAFF_LINES)
                    m_rows.append((mid, tid, uid if mine else g.owner_id, "customer" if mine else "owner", body, when))
                    mid += 1
                status = "open" if rng.random() < 0.8 else "closed"
                t_rows.append((tid, g.id, uid, None, rng.choice(THREAD_SUBJECTS), status, when, body[:140],
                               rng.choice([0, 0, 1]), rng.choice([0, 0, 0, 1, 2]), start, when))
                self.threads_by_gym_team[g.id].append(tid)
                if status == "open":
                    m.threads.append(tid)
                tid += 1
        await self.cp.copy("chat_threads", ["id", "gym_id", "customer_id", "employee_id", "subject", "status",
                                            "last_message_at", "last_message_preview", "customer_unread", "staff_unread",
                                            "created_at", "updated_at"], t_rows)
        await self.cp.copy("chat_messages", ["id", "thread_id", "sender_id", "sender_role", "body", "created_at"],
                           m_rows, chunk=100_000)
        # ---- heavy members: long task lists and wishlists (chat threads preferred: they are handed out first)
        shoppers = [m for m in self.members if self.gym_by_id[m.gym].products and not m.minor]
        with_t = [m for m in shoppers if m.threads]
        n_heavy = max(4, len(self.members) // 50)
        heavy = with_t[:4] + rng.sample([m for m in shoppers if m not in with_t[:4]], k=min(len(shoppers) - 4, n_heavy - 4))
        self.heavy_member_ids = [m.id for m in heavy]
        tid = await self.fresh_id("tasks", n_heavy * 400 + 10)
        wid = await self.fresh_id("wishlist_items", n_heavy * 80 + 10)
        task_rows, w_rows = [], []
        all_products = [p for p in self.products.values() if p.active]
        for m in heavy:
            n_tasks = rng.randint(200, 400)
            open_left = rng.randint(40, 100)
            for _ in range(n_tasks):
                title, kind = rng.choice(TASKS)
                due = self.now + timedelta(hours=rng.randint(-24 * 180, 24 * 30))
                done = due < self.now and (open_left <= 0 or rng.random() < 0.85)
                if not done:
                    if open_left <= 0:
                        due = self.now - timedelta(hours=rng.randint(1, 24 * 180))
                        done = True
                    else:
                        open_left -= 1
                task_rows.append((tid, m.id, title, None, kind, due, due - timedelta(hours=1) if kind == "reminder" else None,
                                  False, due + timedelta(minutes=30) if done else None,
                                  due - timedelta(hours=1) if kind == "reminder" and due < self.now else None,
                                  rng.choice(["user", "user", "ai", "system"]), due - timedelta(days=3), due - timedelta(days=3)))
                tid += 1
            have = self.wishlist_by_user.get(m.id, set())
            own = [p for p in self.gym_by_id[m.gym].products if p not in have]
            want = rng.randint(50, 80)
            picks = rng.sample(own, k=min(len(own), want))
            while len(picks) < want:
                p = rng.choice(all_products).id
                if p not in have and p not in picks:
                    picks.append(p)
            for p in picks:
                w_rows.append((wid, m.id, p, self.now - timedelta(days=rng.uniform(0, 300))))
                wid += 1
        await self.cp.copy("tasks", ["id", "user_id", "title", "notes", "kind", "due_at", "remind_at", "all_day",
                                     "completed_at", "reminded_at", "created_by", "created_at", "updated_at"], task_rows)
        await self.cp.copy("wishlist_items", ["id", "user_id", "product_id", "created_at"], w_rows)
        # ---- 4 trainers with 200 clients each (the largest chains' trainers)
        gyms = sorted((g for g in self.gyms if g.trainers), key=lambda g: -len(g.members))
        self.heavy_trainer_emps: list[int] = []
        tc_rows = []
        tcid = await self.fresh_id("trainer_clients", 4 * 200 + 10)
        assigned = {(r["employee_id"], r["customer_id"]) for r in await self.conn.fetch(
            "SELECT employee_id, customer_id FROM trainer_clients")}
        for g in gyms:
            if len(self.heavy_trainer_emps) >= 4:
                break
            eid = g.trainers[0]
            self.heavy_trainer_emps.append(eid)
            pool = [u for u in g.members if (eid, u) not in assigned]
            for uid in rng.sample(pool, k=min(len(pool), self.n(200, 5))):
                tc_rows.append((tcid, eid, uid, None, "online" if g.online and rng.random() < 0.4 else "offline", True,
                                self.now - timedelta(days=rng.uniform(1, 120))))
                tcid += 1
        await self.cp.copy("trainer_clients", ["id", "employee_id", "customer_id", "subscription_id", "mode", "active",
                                               "created_at"], tc_rows)
        log(f"  heavy tail: owners {[self.owners[j]['e'] for j in self.heavy_owner_idx]} "
            f"(+{len(rows):,} notifications, +{len(t_rows):,} team threads), {len(heavy)} members "
            f"(+{len(task_rows):,} tasks, +{len(w_rows):,} wishlist items), {len(self.heavy_trainer_emps)} trainers "
            f"(+{len(tc_rows)} clients)")

    # -------------------------------------------------------------------------------------- operational history
    async def build_history(self) -> None:
        """What a platform in production has collected - the super-admin pages, the ban sync, the security triage and
        retention read these tables, so a run must not start them empty:
          security_events ~150k / 90 days (unreviewed only within the last 48 h - older ones the triage closes as
          'expired', as the app does), ip_bans 5k (~30 % permanent, most lifted or expired, ~150 active),
          ai_call_logs ~60k / 30 days, email_outbox ~150k / 30 days with bodies (sent / logged / failed - nothing
          queued, so the outbox worker has nothing to send), jobs ~30k finished / 14 days, user_sessions ~20k / 7 days,
          assistant_messages for ~30 % of the members (embeddings computed once per distinct text).
        Every address is from 100.64.0.0/10 (RFC 6598) or the account's own fixed address - never one the load
        generator uses for anonymous visitors or abusers, so no seeded ban can hit a simulated user."""
        rng = self.rng
        now = self.now
        attackers = [f"100.{rng.randint(64, 127)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}" for _ in range(8000)]

        def attacker() -> str:  # a few addresses do most of the damage
            return attackers[min(len(attackers) - 1, int(rng.paretovariate(1.1)) - 1)] if rng.random() < 0.5 \
                else rng.choice(attackers)

        member_ids = [m.id for m in self.members]
        # ---- security events
        cats = [("rate_limit", 30, (1, 2)), ("probe", 22, (1, 3)), ("scanner", 14, (3, 5)), ("bruteforce", 12, (2, 4)),
                ("sqli", 6, (4, 5)), ("xss", 4, (3, 5)), ("traversal", 4, (4, 5)), ("csrf", 3, (2, 3)),
                ("ai_abuse", 2, (2, 3)), ("upload", 1, (3, 4)), ("fraud", 1, (3, 5)), ("command_injection", 0.5, (5, 5)),
                ("other", 0.5, (1, 2))]
        paths = {
            "rate_limit": [("GET", "/shop?q=protein"), ("POST", "/api/assistant/chat"), ("GET", "/gyms"), ("POST", "/auth/login")],
            "probe": [("GET", p) for p in ("/wp-login.php", "/.env", "/admin", "/phpmyadmin/", "/.git/config", "/backup.zip",
                                           "/xmlrpc.php", "/api/v1/users", "/config.json", "/server-status")],
            "scanner": [("GET", p) for p in ("/.env", "/wp-admin/", "/cgi-bin/test.cgi", "/.aws/credentials", "/actuator/env")],
            "bruteforce": [("POST", "/auth/login")],
            "sqli": [("GET", "/shop?q=1%27%20OR%20%271%27=%271"), ("GET", "/gyms?q=%27%20UNION%20SELECT%20NULL--"),
                     ("POST", "/auth/login")],
            "xss": [("GET", "/shop?q=%3Cscript%3Ealert(1)%3C/script%3E"), ("GET", "/jobs?q=%3Cimg%20src=x%20onerror=alert(1)%3E")],
            "traversal": [("GET", "/static/../../etc/passwd"), ("GET", "/media/..%2f..%2fetc%2fpasswd")],
            "csrf": [("POST", "/account/password"), ("POST", "/shop/cart/add")],
            "ai_abuse": [("POST", "/api/assistant/chat"), ("POST", "/api/plans/ask")],
            "upload": [("POST", "/account/photo"), ("POST", "/identity/upload")],
            "fraud": [("POST", "/checkout/123/presence"), ("POST", "/subscribe/iron-gym")],
            "command_injection": [("GET", "/maps/search?q=%3Bcat%20/etc/passwd")],
            "other": [("GET", "/")],
        }
        details = {"rate_limit": "rate limit exceeded on '{b}'", "probe": "{n} not-found responses in 5 minutes",
                   "scanner": "scanner signature: {ua}", "bruteforce": "{n} failed sign-ins for unknown accounts",
                   "sqli": "SQL injection pattern in query", "xss": "script injection pattern in query",
                   "traversal": "path traversal sequence", "csrf": "missing or invalid CSRF token",
                   "ai_abuse": "AI spam: repeated identical prompts", "upload": "file type does not match its content",
                   "fraud": "check-in location far from the branch", "command_injection": "shell metacharacters in query",
                   "other": "malformed request"}
        bad_uas = ["sqlmap/1.8.2#stable (https://sqlmap.org)", "Nikto/2.5.0", "Mozilla/5.0 (compatible; Nuclei)",
                   "python-requests/2.32.3", "curl/8.7.1", "Go-http-client/1.1", "masscan/1.3", "zgrab/0.x"]
        ok_uas = ["Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                  "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1"]
        n_events = self.n(150_000, 100)
        sid = await self.fresh_id("security_events", n_events + 10)
        se_rows = []
        weights = [c[1] for c in cats]
        bursts = [(attacker(), now - timedelta(days=rng.uniform(0, 90))) for _ in range(400)]
        for _ in range(n_events):
            cat, _w, (lo, hi) = weighted(rng, cats, weights)
            if rng.random() < 0.3:  # an attack burst: one address, minutes apart
                ip, t = rng.choice(bursts)
                created = t + timedelta(seconds=rng.uniform(0, 1800))
            else:
                ip, created = attacker(), now - timedelta(seconds=rng.uniform(0, 90 * 86400))
            created = min(created, now - timedelta(seconds=5))
            method, path = rng.choice(paths[cat])
            ua = rng.choice(bad_uas) if cat in ("scanner", "sqli", "probe", "traversal", "command_injection") or rng.random() < 0.3 \
                else rng.choice(ok_uas)
            sev = rng.randint(lo, hi)
            detail = details[cat].format(b=path.split("?")[0], n=rng.randint(10, 60), ua=ua.split("/")[0])
            action = "blocked" if sev >= 4 or cat in ("rate_limit", "csrf") else "logged"
            recent = created > now - timedelta(hours=48)
            reviewed = not recent or sev < 2 or rng.random() < 0.6
            verdict = None
            if reviewed and sev >= 2:
                if recent or rng.random() < 0.7:
                    v = "malicious" if sev >= 4 else rng.choice(["suspicious", "benign"])
                    verdict = json.dumps({"verdict": v, "action": "ban" if v == "malicious" and rng.random() < 0.3 else "none",
                                          "source": rng.choice(["ai", "rules"]), "applied": "none",
                                          "rationale": f"{detail} from {ip}", "at": (created + timedelta(minutes=5)).isoformat()})
                else:
                    verdict = json.dumps({"verdict": "expired", "action": "none", "source": "rules", "applied": "none",
                                          "rationale": "Not reviewed within 48 h - closed without action.",
                                          "at": (created + timedelta(hours=48)).isoformat()})
            uid = rng.choice(member_ids) if cat in ("csrf", "ai_abuse", "fraud") and rng.random() < 0.6 else None
            se_rows.append((sid, ip, uid, method, path[:512], ua[:300], cat, sev, detail[:500], action, reviewed, verdict,
                            created))
            sid += 1
        await self.cp.copy("security_events", ["id", "ip", "user_id", "method", "path", "user_agent", "category",
                                               "severity", "detail", "action", "ai_reviewed", "ai_verdict", "created_at"],
                           se_rows, chunk=50_000)
        # ---- IP bans
        n_bans = self.n(5000, 20)
        bid = await self.fresh_id("ip_bans", n_bans + 10)
        superadmin_id = await self.conn.fetchval("SELECT id FROM users WHERE email = $1", ltenv.SUPERADMIN_EMAIL)
        ban_rows = []
        used_active: set[str] = set()
        reasons = ["rate-limit abuse on 'auth'", "directory brute-forcing (404 flood)", "sqlmap signature; SQL injection",
                   "credential stuffing against unknown accounts", "AI spam flood", "path traversal attempts",
                   "repeated CSRF failures", "manual ban: card testing"]
        for _ in range(n_bans):
            ip = attacker()
            created = now - timedelta(seconds=rng.uniform(600, 90 * 86400))
            permanent = rng.random() < 0.3
            sev = rng.randint(2, 5)
            until = None if permanent else created + timedelta(hours=rng.choice([1, 6, 24, 24 * 7]))
            lifted = None
            r = rng.random()
            if permanent and r < 0.85:
                lifted = min(now - timedelta(minutes=1), created + timedelta(days=rng.uniform(0.5, 30)))
            elif not permanent and until > now and r < 0.5:
                lifted = min(now - timedelta(minutes=1), created + timedelta(hours=rng.uniform(0.2, 5)))
            active = lifted is None and (until is None or until > now)
            if active and ip in used_active:  # at most one active ban per address
                lifted = now - timedelta(minutes=rng.randint(1, 600))
            if lifted is None and (until is None or until > now):
                used_active.add(ip)
            by = rng.choice(["auto", "auto", "auto", "ai", "superadmin"])
            ban_rows.append((bid, ip, None, rng.choice(reasons), sev, until, by, rng.randint(1, 5), False, lifted,
                             superadmin_id if lifted and by == "superadmin" else None, created))
            bid += 1
        await self.cp.copy("ip_bans", ["id", "ip", "user_id", "reason", "severity", "banned_until", "created_by",
                                       "offense_count", "had_subscription", "lifted_at", "lifted_by_id", "created_at"],
                           ban_rows)
        COUNTS["ip_bans_active"] = len(used_active)
        # ---- AI call logs
        n_ai = self.n(60_000, 50)
        aid = await self.fresh_id("ai_call_logs", n_ai + 10)
        feats = [("assistant", 30), ("shop", 25), ("plans", 15), ("coach", 10), ("health_metrics", 8), ("wellness_face", 3),
                 ("id_card", 3), ("ocr", 2), ("gear", 2), ("staff_certification", 1), ("health_scan", 1)]
        provs = [("gateway", "gemma", 85), ("docker", "gemma3:4b", 12), ("anthropic", "claude-opus-5-5", 3)]
        ai_rows = []
        for _ in range(n_ai):
            feat = weighted(rng, [f[0] for f in feats], [f[1] for f in feats])
            prov, model, _w = weighted(rng, provs, [p[2] for p in provs])
            ok = rng.random() > 0.04
            lat = int(min(120_000, rng.lognormvariate(7.4, 0.6))) if ok else rng.choice([90_000, 1_500, 30_000])
            uid = rng.choice(member_ids) if feat in ("assistant", "shop", "plans", "coach", "health_metrics") else None
            kind = "user" if uid else rng.choice(["job", "system"])
            ai_rows.append((aid, feat, prov, model, ok, lat, None if ok else rng.choice(["timeout", "connection refused",
                                                                                       "HTTP 503 from provider"]),
                            uid, kind, int(rng.expovariate(1 / 300.0)), rng.random() < 0.01,
                            now - timedelta(seconds=rng.uniform(0, 30 * 86400))))
            aid += 1
        await self.cp.copy("ai_call_logs", ["id", "feature", "provider", "model", "ok", "latency_ms", "error", "user_id",
                                            "owner_kind", "queue_ms", "cancelled", "created_at"], ai_rows)
        # ---- e-mail outbox (bodies of a realistic size; nothing left queued)
        n_mail = self.n(150_000, 50)
        eid = await self.fresh_id("email_outbox", n_mail + 10)
        subjects = [("Your IronPulse sign-in from a new device", "We noticed a new sign-in to your account from {ip}."),
                    ("Payment received - thank you", "We received your payment for {plan}. Your receipt is attached."),
                    ("Your order {order} is ready for pickup", "Your shop order {order} is ready at the front desk."),
                    ("Your weekly IronPulse summary", "You logged {n} workouts this week. Keep it up!"),
                    ("Reminder: your plan renews in 3 days", "Your {plan} renews in 3 days."),
                    ("Verify your e-mail address", "Confirm your e-mail address to finish setting up your account."),
                    ("Low stock: {n} products need a restock", "{n} products in your store are below their threshold.")]
        accounts_mail = [(m.id, m.email, m.name) for m in self.members] + \
                        [(o["id"], o["e"], self.user_names[o["id"]]) for o in self.owners]
        mail_rows = []
        for _ in range(n_mail):
            uid, email, name = rng.choice(accounts_mail)
            subj, line = rng.choice(subjects)
            fill = {"ip": self.user_ip.get(uid, "198.18.0.1"), "plan": "Gym + Trainer (monthly)",
                    "order": f"IPO-2026-{rng.randint(0, 999999):06d}", "n": rng.randint(2, 30)}
            subject = subj.format(**fill)
            text = (f"Hi {name},\n\n{line.format(**fill)}\n\nIf this wasn't you, reply to this e-mail or open your account "
                    "settings to secure your account.\n\nThe IronPulse team\n--\nYou receive this e-mail because you have "
                    "an IronPulse account. Manage your e-mail preferences at https://ironpulse.test/account/notifications\n")
            html = ("<!doctype html><html><body style=\"font-family:system-ui,sans-serif;background:#f6f7f9;margin:0\">"
                    "<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\"><tr><td align=\"center\">"
                    f"<table width=\"560\" style=\"background:#fff;border-radius:12px;padding:32px\"><tr><td><h1 style=\""
                    f"font-size:20px\">{subject}</h1><p>Hi {name},</p><p>{line.format(**fill)}</p><p><a href=\""
                    "https://ironpulse.test/account\" style=\"background:#2a78d6;color:#fff;padding:10px 16px;"
                    "border-radius:8px;text-decoration:none\">Open IronPulse</a></p><p style=\"color:#6b7280;font-size:12px\">"
                    "You receive this e-mail because you have an IronPulse account. Manage your e-mail preferences in your "
                    "account settings.</p></td></tr></table></td></tr></table></body></html>")
            created = now - timedelta(seconds=rng.uniform(60, 30 * 86400))
            r = rng.random()
            status = "logged" if r < 0.70 else ("sent" if r < 0.98 else "failed")
            mail_rows.append((eid, uid, email, [], subject[:200], text, html, status, 1 if status != "failed" else 5,
                              "SMTP 451: temporary failure" if status == "failed" else None, None,
                              created + timedelta(seconds=rng.uniform(1, 90)) if status != "failed" else None, None, created))
            eid += 1
        await self.cp.copy("email_outbox", ["id", "user_id", "to_email", "cc", "subject", "text_body", "html_body",
                                            "status", "attempts", "last_error", "next_attempt_at", "sent_at",
                                            "notification_id", "created_at"], mail_rows, chunk=20_000)
        # ---- finished background jobs (never queued / running: the job workers would pick those up)
        n_jobs = self.n(30_000, 20)
        jid = await self.fresh_id("jobs", n_jobs + 10)
        kinds = [("verify_identity", 30), ("health_ai_note", 20), ("health_ac_note", 10), ("verify_branch", 8),
                 ("staff_activation_check", 8), ("notify_members", 8), ("coach_plan", 8), ("payroll_explain", 5),
                 ("verify_payment_proof", 3)]
        job_rows = []
        for _ in range(n_jobs):
            kind = weighted(rng, [k[0] for k in kinds], [k[1] for k in kinds])
            created = now - timedelta(seconds=rng.uniform(300, 14 * 86400))
            r = rng.random()
            status = "done" if r < 0.92 else ("failed" if r < 0.97 else "cancelled")
            finished = created + timedelta(seconds=rng.uniform(0.5, 120))
            uid = rng.choice(member_ids) if kind in ("health_ai_note", "health_ac_note", "coach_plan", "verify_identity") else None
            job_rows.append((jid, kind, {"seeded": True, "ref": rng.randint(1, 50_000)}, status,
                             1 if status == "done" else rng.randint(1, 5), 5, created, created + timedelta(seconds=1),
                             finished, "provider timeout" if status == "failed" else None, None,
                             1.0 if status == "done" else None, uid, "keep", None,
                             "requester left" if status == "cancelled" else None, created))
            jid += 1
        await self.cp.copy("jobs", ["id", "kind", "payload", "status", "attempts", "max_attempts", "run_after", "locked_at",
                                    "finished_at", "last_error", "dedupe_key", "progress", "user_id", "exit_policy",
                                    "paused_at", "cancel_reason", "created_at"], job_rows)
        # ---- sessions of the last 7 days (random token hashes: never presented by anyone)
        n_sess = self.n(20_000, 20)
        sess_users = [(m.id, self.user_ip[m.id]) for m in self.members] * 3 + \
                     [(o["id"], o["ip"]) for o in self.owners] * 5 + [(s["id"], s["ip"]) for s in self.staff_accounts]
        uas = ok_uas + ["Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36"]
        sess_rows = []
        for _ in range(n_sess):
            uid, ip = rng.choice(sess_users)
            created = now - timedelta(seconds=rng.uniform(60, 7 * 86400))
            seen = created + timedelta(seconds=rng.uniform(0, (now - created).total_seconds()))
            revoked = seen + timedelta(minutes=rng.randint(1, 120)) if rng.random() < 0.1 else None
            if revoked and revoked > now:
                revoked = now - timedelta(seconds=30)
            sess_rows.append((f"{rng.getrandbits(256):064x}", uid, created, created + timedelta(hours=168), seen, ip,
                              rng.choice(uas), revoked))
        await self.cp.copy("user_sessions", ["id", "user_id", "created_at", "expires_at", "last_seen_at", "ip",
                                             "user_agent", "revoked_at"], sess_rows)
        # ---- assistant conversations (~30 % of the members; heavy members 100-200 messages)
        from app.ai.embeddings import embed

        pairs = [("Remind me to drink water at 4 pm", "Done - I added a reminder for 4:00 pm today."),
                 ("What's on my calendar this week?", "You have 3 workouts and 2 reminders this week."),
                 ("Add a task: stretch for 10 minutes tonight", "Added 'Stretch for 10 minutes' for tonight."),
                 ("How many workouts did I log this week?", "You logged 4 workouts this week - nice consistency."),
                 ("Plan a light recovery day for tomorrow", "Tomorrow: 20 minutes of walking and 10 minutes of mobility."),
                 ("Move my leg day to Friday morning", "Moved 'Leg day' to Friday at 7:00 am."),
                 ("What should I eat after training?", "Protein with some carbs within an hour helps recovery."),
                 ("Remind me to log my weight every Monday", "Added a weekly reminder for Mondays at 7:00 am.")]
        vec_cache: dict[str, str] = {}

        def vec(text: str) -> str:
            v = vec_cache.get(text)
            if v is None:
                v = vec_cache[text] = "[" + ",".join(format(x, ".6g") for x in embed(text)) + "]"
            return v

        heavy = set(getattr(self, "heavy_member_ids", []))
        talkers = [m for m in self.members if m.id in heavy or rng.random() < 0.3]
        n_msgs = sum(150 if m.id in heavy else 16 for m in talkers)
        amid = await self.fresh_id("assistant_messages", n_msgs * 2 + 10)
        am_rows = []
        for m in talkers:
            turns = rng.randint(50, 100) if m.id in heavy else rng.randint(1, 15)
            t = now - timedelta(days=rng.uniform(1, 60))
            step = (now - t).total_seconds() / (turns + 1)
            for _ in range(turns):
                q, a = rng.choice(pairs)
                t = t + timedelta(seconds=step * rng.uniform(0.5, 1.0))
                am_rows.append((amid, m.id, "user", q, None, None, vec(q), t))
                amid += 1
                am_rows.append((amid, m.id, "assistant", a, [], "gateway", vec(a), t + timedelta(seconds=rng.uniform(1, 6))))
                amid += 1
        await self.cp.copy("assistant_messages", ["id", "user_id", "role", "content", "actions", "provider", "embedding",
                                                  "created_at"], am_rows, chunk=20_000)
        log(f"  history: {len(se_rows):,} security events, {len(ban_rows):,} bans ({len(used_active)} active), "
            f"{len(ai_rows):,} AI calls, {len(mail_rows):,} e-mails, {len(job_rows):,} jobs, {len(sess_rows):,} sessions, "
            f"{len(am_rows):,} assistant messages")

    async def build_abusers(self) -> None:
        n = 80  # ~2.5 % of the users are abusers: 40 at the 1,600-user spike, up to 80 for a 3,200-user LT_STEPS run
        uid = await self.next_id("users", n)
        rows = []
        for j in range(n):
            email = f"lt_abuser_{j + 1:02d}@{ltenv.EMAIL_DOMAIN}"
            rows.append(self.user_row(uid + j, email, f"Load Abuser {j + 1:02d}", "customer", dob=self.dob(20, 40),
                                      city="Bengaluru", lat=None, lon=None, photo=None,
                                      ip=ltenv.account_ip("abuser", j + 1), created=self.now - timedelta(days=30)))
            self.abusers.append({"e": email, "id": uid + j})
        await self.cp.copy("users", self.USER_COLS, rows)
        await self.cp.copy("customer_profiles", ["user_id", "created_at", "updated_at"],
                           [(a["id"], self.now, self.now) for a in self.abusers])

    async def build_platform_admins(self) -> None:
        """lt_superadmin_01 .. N_SUPERADMINS: one session per account (SINGLE_SESSION_PER_ACCOUNT) - every simulated
        super-admin signs in with an account of its own (a shared one would sign the others out: 'signed in
        elsewhere'). Same password hash as lt_superadmin (bootstrap made it from SUPERADMIN_PASSWORD = the test
        password). Own random generator: the rest of the data does not move."""
        rng = random.Random(SEED + 3)
        n = ltenv.N_SUPERADMINS
        uid = await self.next_id("users", n)
        rows = []
        self.superadmins: list[dict[str, Any]] = []
        for j in range(n):
            email = ltenv.superadmin_email(j + 1)
            ip = ltenv.account_ip("superadmin", j + 1)
            rows.append(self.user_row(uid + j, email, f"Platform Admin {j + 1:02d}", "superadmin",
                                      dob=self.today - timedelta(days=rng.randint(30, 50) * 365), city="Bengaluru",
                                      lat=None, lon=None, photo=None, ip=ip, created=self.now - timedelta(days=400)))
            self.superadmins.append({"e": email, "id": uid + j, "ip": ip})
        await self.cp.copy("users", self.USER_COLS, rows)

    async def build_junk_accounts(self) -> None:
        """lt_junk_001 .. N_JUNK_ACCOUNTS: the signed-in half of the JunkUser class (locustfile.py) - members with a
        profile and an active ONLINE membership of a random gym (so the member forms, the gym chat and the shop answer
        them), and nothing else: every order, thread, task, notification ... id they try is somebody else's (the IDOR
        probes expect 403 / 404). Fixed addresses of their own block (ltenv.account_ip("junk", n))."""
        rng = random.Random(SEED + 4)
        n = ltenv.N_JUNK_ACCOUNTS
        uid0 = await self.next_id("users", n)
        sub_id = await self.fresh_id("subscriptions", n)
        u_rows, p_rows, s_rows, t_rows = [], [], [], []
        online = [g for g in self.gyms if any(p[1] == "online" and "trainer" not in code for code, p in g.plans.items())]
        self.junk_accounts: list[dict[str, Any]] = []
        for j in range(n):
            uid = uid0 + j
            email = f"lt_junk_{j + 1:03d}@{ltenv.EMAIL_DOMAIN}"
            ip = ltenv.account_ip("junk", j + 1)
            created = self.now - timedelta(days=rng.randint(5, 300))
            dob = self.today - timedelta(days=rng.randint(19, 45) * 365 + rng.randint(15, 340))
            u_rows.append(self.user_row(uid, email, f"Junk Tester {j + 1:03d}", "customer", dob=dob,
                                        city="Bengaluru", lat=None, lon=None, photo=None, ip=ip, created=created))
            p_rows.append((uid, "general_fitness", "beginner", 170.0, 70.0, created, created))
            acc = {"e": email, "id": uid, "ip": ip, "g": None, "gid": None}
            if online:
                g = rng.choice(online)
                code = next(c for c, p in g.plans.items() if p[1] == "online" and "trainer" not in c)
                pid, _mode, price, pname = g.plans[code]
                starts = self.today - timedelta(days=rng.randint(0, 10))
                when = datetime.combine(starts, dtime(6, 0), tzinfo=UTC)
                s_rows.append((sub_id, uid, g.id, pid, None, None, "active", "online_checkout", price, g.currency, starts,
                               starts + timedelta(days=30), when, None, None, None, None, None, None, 0, [], ip, when, when))
                self.ids["transactions"] += 1
                t_rows.append((self.ids["transactions"], g.id, "incoming", "subscription", "success", price, g.currency,
                               f"{pname} - Junk Tester {j + 1:03d}"[:255], f"Junk Tester {j + 1:03d}", "test",
                               f"LT-SUB-{sub_id}", None, None, None, uid, None, sub_id, None, None, None, None, None, {},
                               when, when, when))
                acc.update({"g": g.slug, "gid": g.id, "sub": sub_id})
                sub_id += 1
            self.junk_accounts.append(acc)
        await self.cp.copy("users", self.USER_COLS, u_rows)
        await self.cp.copy("customer_profiles", ["user_id", "goal", "fitness_level", "height_cm", "weight_kg",
                                                 "created_at", "updated_at"], p_rows)
        await self.cp.copy("subscriptions", ["id", "customer_id", "gym_id", "plan_id", "branch_id", "trainer_id", "status",
                                             "channel", "price", "currency", "starts_on", "ends_on", "activated_at",
                                             "cancelled_at", "physically_present", "otp_verified_at", "customer_latitude",
                                             "customer_longitude", "distance_km", "fraud_score", "fraud_flags",
                                             "created_ip", "created_at", "updated_at"], s_rows)
        await self.cp.copy("transactions", self.TX_COLS, t_rows)

    async def build_remembered(self, member_ids: list[int]) -> None:
        """The sign-in page's account chooser (remembered accounts) for these members (write_outputs picks 30 % of
        the accounts.json positions, the first one included): an opaque reference (accounts.json "rr") and the
        browser's device key ("rd"); the database keeps only their SHA-256, as app/services/remembered_accounts.py
        does. Those members sign in like a returning browser: POST /auth/remembered (the chooser's lookup), then the
        form with account_ref instead of the e-mail."""
        import hashlib

        rng = random.Random(SEED + 5)
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"  # token_urlsafe's alphabet
        rows = []
        self.remembered = {}
        labels = ["Chrome on Windows", "Safari on iPhone", "Chrome on Android", "Firefox on Windows", "Safari on macOS"]
        for uid in member_ids:
            ref = "".join(rng.choice(alphabet) for _ in range(43))
            device = "".join(rng.choice(alphabet) for _ in range(32))
            when = self.now - timedelta(days=rng.uniform(1, 60))
            rows.append((hashlib.sha256(ref.encode()).hexdigest(), uid, hashlib.sha256(device.encode()).hexdigest(),
                         None, rng.choice(labels), when, when))
            self.remembered[uid] = (ref, device)
        await self.cp.copy("remembered_accounts", ["id", "user_id", "device_hash", "session_id", "label", "created_at",
                                                   "last_used_at"], rows)
        await self.conn.execute("ANALYZE remembered_accounts")

    async def backfill_ratings(self) -> int:
        """products.rating_avg / review_count from the visible reviews - ONE set-based UPDATE right after the COPY
        (COPY leaves the server defaults NULL / 0). Without it the app's hourly reconcile (app/services/shop.py
        backfill_product_ratings, first run 60 s after start) would rewrite thousands of products DURING a run.
        Same expression as the app's (round(avg(rating), 4) of the reviews that are not hidden); only rows that
        disagree are written, updated_at is left alone (a review is not an edit of the listing)."""
        status = await self.conn.execute(
            "UPDATE products p SET rating_avg = s.avg, review_count = s.n "
            "FROM (SELECT p2.id, r.avg, coalesce(r.n, 0) AS n FROM products p2 LEFT JOIN "
            "      (SELECT product_id, round(avg(rating), 4) AS avg, count(*) AS n FROM product_reviews "
            "       WHERE NOT is_hidden GROUP BY product_id) r ON r.product_id = p2.id) s "
            "WHERE p.id = s.id AND (p.rating_avg IS DISTINCT FROM s.avg OR p.review_count <> s.n)")
        fixed = int(status.split()[-1])
        drift = await self.conn.fetchval(
            "SELECT count(*) FROM products p LEFT JOIN (SELECT product_id, round(avg(rating), 4) AS avg, count(*) AS n "
            "FROM product_reviews WHERE NOT is_hidden GROUP BY product_id) r ON r.product_id = p.id "
            "WHERE p.rating_avg IS DISTINCT FROM r.avg OR p.review_count <> coalesce(r.n, 0)")
        if drift:
            raise RuntimeError(f"rating backfill left {drift} products out of line with their reviews")
        COUNTS["rating_backfill_products"] = fixed
        log(f"  rating summary: {fixed:,} products got rating_avg / review_count from their reviews (0 left to the "
            "hourly reconcile)")
        return fixed


# ================================================================================================ orchestration
# Per-database settings of ironpulse_loadtest (ALTER DATABASE ... SET; the shared cluster's defaults stay as they are):
# JIT off (an OLTP app: JIT compilation only adds latency to short queries) and random_page_cost 1.1 (SSD storage).
DB_SETTINGS = {"jit": "off", "random_page_cost": "1.1"}
# runtime tables: what a run adds on top of the seed (reset_runtime deletes rows above the seeded maximum id)
RUNTIME_TABLES = ("ip_bans", "security_events", "ai_call_logs", "jobs", "email_outbox", "notifications",
                  "chat_messages", "assistant_messages", "audit_logs", "metric_entries")


async def recreate_database(drop_database: bool) -> None:
    """A brand-new, empty ironpulse_loadtest owned by the app role, with the extensions created by the superuser.

    Default: when the database already exists its ``public`` schema is dropped and recreated (CASCADE: every table,
    sequence and extension object) - the same empty database without DROP DATABASE, which forces an IMMEDIATE
    checkpoint of the whole shared cluster (measured: 67 s of fsyncs over ~200k files while the other teams' test
    databases are busy). ``--drop-database`` does the literal DROP + CREATE DATABASE instead."""
    import asyncpg

    role = str(ltenv.db_parts()["user"]).replace('"', '""')
    admin = await asyncpg.connect(ltenv.superuser_dsn("postgres"))
    try:
        exists = bool(await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", ltenv.DB_NAME))
        await admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1 "
                            "AND pid <> pg_backend_pid()", ltenv.DB_NAME)
        if exists and drop_database:
            await admin.execute(f'DROP DATABASE "{ltenv.DB_NAME}" WITH (FORCE)')
            exists = False
        if exists:
            await admin.execute(f'ALTER DATABASE "{ltenv.DB_NAME}" OWNER TO "{role}"')
            await admin.execute(f'ALTER DATABASE "{ltenv.DB_NAME}" CONNECTION LIMIT {ltenv.DB_CONN_LIMIT}')
        else:
            await admin.execute(f'CREATE DATABASE "{ltenv.DB_NAME}" OWNER "{role}" CONNECTION LIMIT {ltenv.DB_CONN_LIMIT}')
    finally:
        await admin.close()
    admin = await asyncpg.connect(ltenv.superuser_dsn("postgres"))
    try:  # production-like planner settings for THIS database only (SSD: random_page_cost 1.1; OLTP: no JIT)
        for key, value in DB_SETTINGS.items():
            await admin.execute(f'ALTER DATABASE "{ltenv.DB_NAME}" SET {key} = {value}')
    finally:
        await admin.close()
    conn = await asyncpg.connect(ltenv.superuser_dsn(ltenv.DB_NAME))
    try:
        if await conn.fetchval("SELECT current_database()") != ltenv.DB_NAME:
            raise SystemExit("not connected to the load-test database")
        if exists:
            await conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
            await conn.execute("CREATE SCHEMA public")
            await conn.execute("ALTER SCHEMA public OWNER TO pg_database_owner")  # PostgreSQL 15+ default ownership
            await conn.execute("GRANT USAGE ON SCHEMA public TO PUBLIC")
        for ext in ("vector", "citext", "pg_trgm"):
            await conn.execute(f'CREATE EXTENSION IF NOT EXISTS "{ext}"')
    finally:
        await conn.close()


def clean_var_dir() -> None:
    var = ltenv.VAR_DIR.resolve()
    if var.parent != ltenv.LT_DIR.resolve() or var.name != "var":
        raise SystemExit(f"refusing to clean {var}")
    for sub in ("uploads", "mail", "logs"):
        shutil.rmtree(var / sub, ignore_errors=True)
    var.mkdir(parents=True, exist_ok=True)


async def flush_redis() -> None:
    import redis.asyncio as aioredis

    url = ltenv.redis_url()
    r = aioredis.from_url(url)
    try:
        if int(r.connection_pool.connection_kwargs.get("db", 0)) != ltenv.REDIS_DB:
            raise SystemExit("refusing to FLUSHDB a Redis database that is not the load-test one")
        await r.flushdb()
    finally:
        await r.aclose()


async def demo_and_schema() -> None:
    from app.db import create_all, dispose_engine, session_scope
    from app.services.bootstrap import bootstrap
    from scripts.demo.loader import load_dataset, seed_demo

    with Step("schema (app.db.create_all + sync_schema)"):
        await create_all()
    with Step("bootstrap (super-admin, exercise library)"):
        async with session_scope() as db:
            await bootstrap(db)
    with Step("demo dataset (scripts/seed_demo.py seed_demo, write_credentials=False)"):
        data = load_dataset()
        data["email_domain"] = ltenv.DEMO_EMAIL_DOMAIN
        for g in data["gyms"]:  # the contact/website addresses of the dataset point at the reserved .test TLD too
            if g.get("website"):
                g["website"] = f"https://{g['slug']}.ironpulse.test"
        async with session_scope() as db:
            result = await seed_demo(db, password=ltenv.PASSWORD, dataset=data, write_credentials=False)
        log(f"  demo: {result['stats']}")
    await dispose_engine()


async def bulk(scale: float) -> Builder:
    import asyncpg

    conn = await asyncpg.connect(ltenv.loadtest_db_url("postgresql"))
    await conn.execute("SET statement_timeout = 0; SET maintenance_work_mem = '256MB'")
    b = Builder(conn, scale)
    try:
        with Step("pools of the demo images"):
            await b.load_pools()
        hnsw = await conn.fetchval("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_products_embedding_hnsw'")
        if hnsw:
            await conn.execute("DROP INDEX ix_products_embedding_hnsw")  # rebuilt once after the load (much faster)
        async with conn.transaction():
            with Step("gyms, owners, branches (+hours, features, photos)"):
                await b.build_gyms()
            with Step("plans"):
                await b.build_plans()
            with Step("staff accounts, employees, shifts"):
                await b.build_staff()
            with Step("members, profiles, conditions, metrics, alerts, tasks, wellness"):
                await b.build_members()
            with Step("subscriptions, payments, trainer clients"):
                await b.build_subscriptions()
            with Step("abuser accounts"):
                await b.build_abusers()
            with Step(f"super-admin accounts (lt_superadmin_01..{ltenv.N_SUPERADMINS:02d}: one per simulated admin)"):
                await b.build_platform_admins()
            with Step(f"junk-tester accounts (lt_junk_001..{ltenv.N_JUNK_ACCOUNTS:03d}, online memberships)"):
                await b.build_junk_accounts()
            with Step("identity documents"):
                await b.build_identity()
            with Step("products (+images, embeddings, stock)"):
                await b.build_products()
            with Step("orders, items, events, sales, refunds, reviews"):
                await b.build_orders()
            with Step("chat threads + messages"):
                await b.build_chat()
            with Step("notifications"):
                await b.build_notifications()
            with Step("job postings + enquiries"):
                await b.build_jobs()
            with Step("audit trail, transactions, coupons, wishlists, carts"):
                await b.build_audit_and_money()
            with Step("heavy tail (big owners' notifications + threads, long task lists / wishlists, busy trainers)"):
                await b.build_heavy_tail()
            with Step("history (security events, bans, AI calls, outbox, jobs, sessions, assistant)"):
                await b.build_history()
            with Step("product review summary: rating_avg / review_count backfill (one set-based UPDATE)"):
                await b.backfill_ratings()
        with Step("sequences"):
            for t in await conn.fetch(
                    "SELECT table_name AS t FROM information_schema.columns WHERE table_schema = 'public' "
                    "AND column_name = 'id' AND column_default LIKE 'nextval(%'"):
                await conn.execute(f"SELECT setval(pg_get_serial_sequence('{t['t']}', 'id'), "
                                   f"greatest(1, (SELECT coalesce(max(id), 0) FROM \"{t['t']}\")))")
        if hnsw:
            with Step("HNSW index on products.embedding (rebuilt)"):
                await conn.execute(hnsw)
        with Step("VACUUM ANALYZE"):
            await conn.execute("VACUUM (ANALYZE)")
        return b
    finally:
        await conn.close()


async def write_outputs(b: Builder) -> None:
    import asyncpg

    conn = await asyncpg.connect(ltenv.loadtest_db_url("postgresql"))
    try:
        pub = await conn.fetch(
            "SELECT b.id, b.gym_id, b.city, b.latitude, b.longitude FROM branches b JOIN gyms g ON g.id = b.gym_id "
            "WHERE b.verification_status = 'verified' AND b.is_open AND g.offline_enabled AND g.is_active "
            "AND (SELECT count(*) FROM branch_photos p WHERE p.branch_id = b.id) >= 3 ORDER BY b.id")
        by_gym: dict[int, list[int]] = defaultdict(list)
        for r in pub:
            by_gym[r["gym_id"]].append(r["id"])
        gyms = await conn.fetch(
            "SELECT g.id, g.slug, g.name, g.online_enabled AND g.online_open AS online FROM gyms g JOIN users u ON u.id = g.owner_id "
            "WHERE g.is_active AND u.deleted_at IS NULL ORDER BY g.id")
        listed = [g for g in gyms if by_gym.get(g["id"]) or g["online"]]
        products = await conn.fetch("SELECT p.id, p.slug, p.gym_id FROM products p JOIN gyms g ON g.id = p.gym_id "
                                    "WHERE p.is_active AND g.is_active ORDER BY p.id")
        jobs = await conn.fetch("SELECT j.id FROM job_postings j JOIN gyms g ON g.id = j.gym_id JOIN users u ON u.id = g.owner_id "
                                "WHERE j.is_open AND g.is_active AND u.deleted_at IS NULL ORDER BY j.id")
        trainers = await conn.fetch("SELECT DISTINCT e.user_id FROM employees e WHERE e.status = 'active' AND e.is_public "
                                    "AND e.deleted_at IS NULL AND e.user_id IS NOT NULL ORDER BY 1 LIMIT 3000")
        cities = await conn.fetch("SELECT b.city, count(*) n FROM branches b GROUP BY 1 ORDER BY 2 DESC LIMIT 40")
        stores = await conn.fetch("SELECT id FROM gyms WHERE is_active ORDER BY id")
        demo = await conn.fetch("SELECT email FROM users WHERE email LIKE $1 ORDER BY id", f"%@{ltenv.DEMO_EMAIL_DOMAIN}")
        # owner samples: orders, products, employees, customers, team threads
        o_orders = defaultdict(list)
        for r in await conn.fetch("SELECT gym_id, id FROM (SELECT gym_id, id, row_number() OVER (PARTITION BY gym_id "
                                  "ORDER BY created_at DESC) rn FROM shop_orders) s WHERE rn <= 15"):
            o_orders[r["gym_id"]].append(r["id"])
        o_customers = defaultdict(list)
        for r in await conn.fetch("SELECT gym_id, customer_id FROM (SELECT gym_id, customer_id, row_number() OVER "
                                  "(PARTITION BY gym_id ORDER BY id DESC) rn FROM subscriptions WHERE status = 'active') s WHERE rn <= 15"):
            o_customers[r["gym_id"]].append(r["customer_id"])
        clients = defaultdict(list)
        for r in await conn.fetch("SELECT employee_id, customer_id FROM trainer_clients WHERE active"):
            if len(clients[r["employee_id"]]) < 10:
                clients[r["employee_id"]].append(r["customer_id"])
        # other people's objects for the JunkUser's IDOR probes (the lt_junk_* accounts own none of them): a fixed,
        # spread-out sample per table (id modulo a prime - the same on every rebuild) and each table's largest id
        junk_ids = [a["id"] for a in b.junk_accounts]
        foreign: dict[str, Any] = {}
        samples = {
            "orders": "SELECT id FROM shop_orders WHERE id % 61 = 7", "threads": "SELECT id FROM chat_threads WHERE id % 13 = 3",
            "tasks": "SELECT id FROM tasks WHERE id % 409 = 5", "notifications": "SELECT id FROM notifications WHERE id % 487 = 11",
            "alerts": "SELECT id FROM health_alerts WHERE id % 3 = 0",
            "conditions": "SELECT id FROM health_conditions WHERE id % 23 = 1",
            "wellness": "SELECT id FROM wellness_checks WHERE id % 7 = 2",
            "identity_docs": "SELECT id FROM identity_documents WHERE id % 53 = 4 AND user_id <> ALL($1::bigint[])",
            "subscriptions": "SELECT id FROM subscriptions WHERE id % 97 = 6 AND customer_id <> ALL($1::bigint[])",
            "customers": "SELECT id FROM users WHERE role = 'customer' AND id % 83 = 9 AND id <> ALL($1::bigint[])",
            "employees": "SELECT id FROM employees WHERE id % 11 = 1", "enquiries": "SELECT id FROM job_enquiries WHERE id % 5 = 1",
            "branches": "SELECT id FROM branches WHERE id % 9 = 2", "products": "SELECT id FROM products WHERE id % 71 = 3",
            "jobs": "SELECT id FROM job_postings WHERE id % 7 = 3", "statements": "SELECT id FROM salary_statements",
            "refunds": "SELECT id FROM refunds", "reviews": "SELECT id FROM product_reviews WHERE id % 7 = 1",
            "emails": "SELECT id FROM email_outbox WHERE id % 997 = 5", "reports": "SELECT id FROM misconduct_reports",
            "metrics": "SELECT id FROM metric_entries WHERE id % 1999 = 7",
        }
        for key, sql in samples.items():
            try:
                args = (junk_ids,) if "$1" in sql else ()
                foreign[key] = [r[0] for r in await conn.fetch(f"{sql} ORDER BY id LIMIT 300", *args)]
            except Exception as exc:  # noqa: BLE001 - a table the app no longer has: no probes of that kind
                log(f"  foreign ids: {key} skipped ({exc.__class__.__name__})")
                foreign[key] = []
        foreign["max_ids"] = {t: int(await conn.fetchval(f"SELECT coalesce(max(id), 0) FROM {t}")) for t in
                              ("shop_orders", "chat_threads", "tasks", "notifications", "users", "products", "job_postings",
                               "branches", "employees", "subscriptions")}
    finally:
        await conn.close()

    rng = random.Random(SEED + 1)
    owners = []
    for j, o in enumerate(b.owners):
        gids = o["gyms"]
        g_products = [p for gid in gids for p in b.gym_by_id[gid].products][:30]
        g_emps = [e for gid in gids for e in b.gym_by_id[gid].staff][:20]
        owner_jobs = sorted((jt for gid in gids for jt in b.jobs_by_gym.get(gid, [])), key=lambda jt: -jt[1])
        owners.append({**o, "orders": [x for gid in gids for x in o_orders.get(gid, [])][:20], "products": g_products,
                       "employees": g_emps, "customers": [x for gid in gids for x in o_customers.get(gid, [])][:20],
                       "threads": [t for gid in gids for t in b.threads_by_gym_team.get(gid, [])][:10],
                       "jobs": [jt[0] for jt in owner_jobs][:10],
                       "branches": sum(b.gym_by_id[gid].size for gid in gids), "heavy": j in b.heavy_owner_idx})
    # Locust takes accounts in this order (worker i of n: i, i+n, ...): the heavy owners FIRST, so every load step -
    # including the 200-user SLO step - has the biggest chains' owners signed in; the rest shuffled (Pareto mix)
    heavy_owners = [owners[j] for j in b.heavy_owner_idx]
    rest = [o for j, o in enumerate(owners) if j not in b.heavy_owner_idx]
    rng.shuffle(rest)
    owners = heavy_owners + rest
    staff = [{**s, "clients": clients.get(s["emp"], []), "threads": b.threads_by_emp.get(s["emp"], [])[:10],
              "heavy": s["emp"] in b.heavy_trainer_emps} for s in b.staff_accounts]
    staff = [x for x in staff if x["heavy"]] + [x for x in staff if not x["heavy"]]  # busy trainers first
    members = []
    for m in b.members:
        g = b.gym_by_id[m.gym]
        members.append({"e": m.email, "id": m.id, "ip": ltenv.account_ip("member", m.idx), "g": g.slug, "gid": g.id,
                        "b": m.branch, "t": m.threads[:8], "o": m.orders[-6:],
                        "p": rng.sample(g.products, k=min(8, len(g.products))) if g.products else [],
                        "h": m.height, "w": m.weight, "active": m.has_active, "minor": m.minor})
    # Locust takes members in this order: interleave so that the share of accounts with chat threads is the same at
    # any number of users (the very first account has threads, so even a 3-member smoke exercises the chat tasks)
    with_t = [m for m in members if m["t"]]
    without = [m for m in members if not m["t"]]
    every = max(1, round(len(members) / max(1, len(with_t))))
    ordered = []
    while with_t or without:
        if with_t and (len(ordered) % every == 0 or not without):
            ordered.append(with_t.pop(0))
        else:
            ordered.append(without.pop(0))
    # heavy members (long task lists / wishlists) at indexes 0-3 and then every 50th position
    heavy_ids = set(b.heavy_member_ids)
    heavy_list = [m for m in ordered if m["id"] in heavy_ids]
    light = [m for m in ordered if m["id"] not in heavy_ids]
    members = []
    while heavy_list or light:
        pos = len(members)
        if heavy_list and (pos < 4 or pos % 50 == 0 or not light):
            members.append(heavy_list.pop(0))
        else:
            members.append(light.pop(0))
    for m in members:
        m["heavy"] = m["id"] in heavy_ids
    # the account chooser: 30 % of the browsers remember their account - positions 0, 3, 6 of every ten, so the first
    # members Locust hands out (a 3-member smoke too) sign in both ways
    chosen = [m for pos, m in enumerate(members) if pos % 10 in (0, 3, 6)]
    conn = await asyncpg.connect(ltenv.loadtest_db_url("postgresql"))
    try:
        b.conn, b.cp = conn, Copier(conn)
        async with conn.transaction():
            await b.build_remembered([m["id"] for m in chosen])
    finally:
        await conn.close()
    for m in chosen:
        m["rr"], m["rd"] = b.remembered[m["id"]]
    accounts = {
        "_note": "Load-test accounts of ironpulse_loadtest (throw-away data). Every account uses the password below. "
                 "Gitignored - never commit.",
        "password": ltenv.PASSWORD, "base_url": ltenv.BASE_URL, "generated_at": datetime.now(UTC).isoformat(),
        "superadmin": {"e": ltenv.SUPERADMIN_EMAIL, "note": "the server's bootstrap admin - no simulated user signs in "
                                                            "with it (one session per account: see 'superadmins')"},
        "superadmins": b.superadmins,  # one per simulated super-admin (SINGLE_SESSION_PER_ACCOUNT)
        "owners": owners, "staff": staff, "members": members, "abusers": b.abusers,
        "junk": b.junk_accounts,  # the signed-in JunkUser browsers, one account each
        "demo_accounts": [r["email"] for r in demo],
    }
    catalogue = {
        "gyms": [{"id": g["id"], "slug": g["slug"], "name": g["name"], "online": bool(g["online"]),
                  "branches": by_gym.get(g["id"], [])[:60], "n_branches": len(by_gym.get(g["id"], []))} for g in listed],
        "branch_points": [[r["id"], r["latitude"], r["longitude"]] for r in pub][::3],
        "products": [[p["id"], p["slug"], p["gym_id"]] for p in products],
        "jobs": [j["id"] for j in jobs],
        "trainers": [t["user_id"] for t in trainers],
        "cities": [{"name": c["city"], "n": c["n"]} for c in cities],
        "city_centres": [{"name": c[0], "lat": c[2], "lon": c[3]} for c in CITIES],
        "stores": [s["id"] for s in stores],
        "categories": ["supplements", "nutrition", "apparel", "accessories", "wearables", "equipment"],
        "search": SEARCH_TERMS, "typos": TYPO_TERMS,
        "store_queries": sorted({a.lower()[:4] for a in GYM_A} | {"fit", "gym", "club", "iron", "pulse", "forge"}),
        "foreign": foreign,  # other people's object ids (JunkUser IDOR probes: expected 403 / 404)
        "junk": {"share": JUNK["share"], "seed": JUNK_SEED, "markers": list(junkgen.XSS_MARKERS)},
    }
    ltenv.ACCOUNTS_FILE.write_text(json.dumps(accounts, separators=(",", ":")), encoding="utf-8")
    os.chmod(ltenv.ACCOUNTS_FILE, 0o600)
    ltenv.CATALOGUE_FILE.write_text(json.dumps(catalogue, separators=(",", ":")), encoding="utf-8")
    log(f"wrote {ltenv.ACCOUNTS_FILE.name} ({len(members)} members ({len(b.remembered)} with a remembered account), "
        f"{len(owners)} owners, {len(staff)} staff, {len(b.superadmins)} super-admins, {len(b.abusers)} abusers, "
        f"{len(b.junk_accounts)} junk testers) and {ltenv.CATALOGUE_FILE.name} ({len(listed)} listed gyms, {len(pub)} "
        f"published branches, {len(products)} public products, {len(jobs)} open jobs, "
        f"{sum(len(v) for k, v in foreign.items() if k != 'max_ids')} foreign ids for the IDOR probes)")


async def table_counts() -> dict[str, int]:
    import asyncpg

    conn = await asyncpg.connect(ltenv.loadtest_db_url("postgresql"))
    try:
        rows = await conn.fetch("SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY relname")
        size = await conn.fetchval("SELECT pg_size_pretty(pg_database_size(current_database()))")
        out = {r["relname"]: int(r["n_live_tup"]) for r in rows}
        out["_database_size"] = size
        return out
    finally:
        await conn.close()


async def seeded_floors(conn: Any) -> dict[str, int]:
    """max(id) of every runtime table right after the seed: what a run adds lies above it."""
    out = {}
    for t in RUNTIME_TABLES:
        out[t] = int(await conn.fetchval(f"SELECT coalesce(max(id), 0) FROM {t}"))
    return out


async def reset_runtime() -> None:
    """Between runs: remove what a run added on top of the seed and keep the seeded history - rows above the seeded
    maximum id of the runtime tables (seed_summary.json "runtime_floor"), sessions created after the seed finished,
    login lockouts; Redis db 9 is flushed and the spooled mail removed. A full re-seed (~1.5 min) is still the clean
    way: a run also places orders (stock), posts chat messages and logs metrics."""
    import asyncpg

    summary = json.loads((ltenv.LT_DIR / "seed_summary.json").read_text(encoding="utf-8"))
    floors = summary.get("runtime_floor") or {}
    finished = datetime.fromisoformat(summary["finished_at"])
    if not floors:
        raise SystemExit("seed_summary.json has no runtime_floor (seeded by an older kit) - re-seed instead")
    conn = await asyncpg.connect(ltenv.loadtest_db_url("postgresql"))
    try:
        if await conn.fetchval("SELECT current_database()") != ltenv.DB_NAME:
            raise SystemExit("not connected to the load-test database")
        removed = {}
        for t in RUNTIME_TABLES:
            if t in floors:  # no TRUNCATE ... CASCADE: never reach into other tables
                removed[t] = int((await conn.execute(f"DELETE FROM {t} WHERE id > $1", int(floors[t]))).split()[-1])
        removed["user_sessions"] = int((await conn.execute("DELETE FROM user_sessions WHERE created_at > $1",
                                                           finished)).split()[-1])
        await conn.execute("UPDATE users SET failed_login_count = 0, locked_until = NULL WHERE locked_until IS NOT NULL "
                           "OR failed_login_count > 0")
        await conn.execute("VACUUM (ANALYZE) user_sessions, ip_bans, security_events, jobs, notifications")
        log(f"removed run leftovers: {removed}")
    finally:
        await conn.close()
    await flush_redis()
    shutil.rmtree(ltenv.VAR_DIR / "mail", ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scale", type=float, default=1.0, help="data volume factor (1.0 = production-like, default)")
    ap.add_argument("--reset-runtime", action="store_true",
                    help="keep the data; remove what a run added (rows above the seeded ids, new sessions), Redis db 9")
    ap.add_argument("--force", action="store_true", help="run even if something listens on the load-test port")
    ap.add_argument("--drop-database", action="store_true",
                    help="DROP + CREATE DATABASE instead of recreating its schema (forces a cluster-wide checkpoint)")
    ap.add_argument("--junk-share", type=float, default=0.10,
                    help="share of the seeded display texts replaced by random junk (default 0.10; deterministic)")
    ap.add_argument("--no-junk", action="store_true", help="no random junk in the seeded data (same as --junk-share 0)")
    args = ap.parse_args()
    if not 0.0 <= args.junk_share <= 1.0:
        ap.error("--junk-share must be between 0 and 1")
    JUNK["share"] = 0.0 if args.no_junk else args.junk_share
    if port_in_use(ltenv.PORT) and not args.force:
        print(f"[seed] something listens on {ltenv.HOST}:{ltenv.PORT} - stop the load-test server first "
              "(loadtest/run_server.sh stop)", file=sys.stderr)
        return 2
    configure_environment()
    if args.reset_runtime:
        asyncio.run(reset_runtime())
        log("runtime state cleared (run leftovers above the seeded ids, new sessions, lockouts, Redis db 9)")
        return 0

    async def run() -> Builder:
        with Step(f"fresh {ltenv.DB_NAME} (superuser: {'drop + create' if args.drop_database else 'create or wipe schema'}), extensions"):
            await recreate_database(args.drop_database)
        clean_var_dir()
        await demo_and_schema()
        b = await bulk(args.scale)
        with Step("accounts.json + catalogue.json"):
            await write_outputs(b)
        with Step("Redis db 9 FLUSHDB"):
            await flush_redis()
        return b

    asyncio.run(run())
    counts = asyncio.run(table_counts())

    async def floors() -> dict[str, int]:
        import asyncpg

        conn = await asyncpg.connect(ltenv.loadtest_db_url("postgresql"))
        try:
            return await seeded_floors(conn)
        finally:
            await conn.close()

    runtime_floor = asyncio.run(floors())
    total = time.perf_counter() - T0
    print("\n[seed] timings")
    for name, dt in TIMINGS:
        print(f"  {dt:7.1f}s  {name}")
    print(f"  {total:7.1f}s  TOTAL")
    print("\n[seed] rows per table (pg_stat_user_tables after ANALYZE)")
    for t, n in sorted(counts.items(), key=lambda kv: (kv[0].startswith("_"), kv[0])):
        if t.startswith("_") or n:
            print(f"  {t:28} {n}")
    junk_total = sum(JUNK_COUNTS.values())
    if JUNK["share"]:
        print(f"\n[seed] random junk: {junk_total:,} values (share {JUNK['share']:g}, seed {JUNK_SEED}) in "
              f"{len(JUNK_COUNTS)} columns, e.g.")
        for s in JUNK_SAMPLES[:8]:
            print(f"  {s}")
    summary = {"timings": dict(TIMINGS), "total_seconds": round(total, 1), "copied_rows": dict(COUNTS),
               "table_rows": counts, "scale": args.scale, "finished_at": datetime.now(UTC).isoformat(),
               "runtime_floor": runtime_floor, "db_settings": DB_SETTINGS, "connection_limit": ltenv.DB_CONN_LIMIT,
               "junk": {"share": JUNK["share"], "seed": JUNK_SEED, "values": junk_total,
                        "by_column": dict(sorted(JUNK_COUNTS.items())), "samples": JUNK_SAMPLES[:40]},
               "accounts": {"superadmins": ltenv.N_SUPERADMINS, "junk_testers": ltenv.N_JUNK_ACCOUNTS,
                            "one_session_per_account": True}}
    (ltenv.LT_DIR / "seed_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
