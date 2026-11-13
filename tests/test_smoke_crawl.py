"""Whole-app smoke crawl: seed the demo dataset, then GET every route as every role.

Fails on any 5xx (template errors, lazy-load errors, crashes). Path parameters are filled from the
seeded rows. Also flags pages that run an excessive number of SQL queries (N+1 smell)."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select
from starlette.routing import Route

from app.db import QueryCounter
from app.models import (
    Branch,
    ChatThread,
    Employee,
    Gym,
    IdentityDocument,
    JobPosting,
    MisconductReport,
    SalaryStatement,
    Subscription,
    User,
    Video,
)
from app.models.enums import Role, VerificationStatus
from tests.conftest import login_as, make_client

MAX_QUERIES_PER_PAGE = 45
SKIP_PATHS = {"/ws", "/maps/tiles/{z}/{x}/{y}.png", "/media/{key:path}", "/checkout/return", "/auth/logout"}
SKIP_PREFIXES = ("/static", "/api/geo", "/superadmin/files", "/owner/finance/transactions.csv")


async def _context(db) -> dict:
    gym = (await db.execute(select(Gym).where(Gym.slug == "forge-and-flow"))).scalar_one()
    branch = (await db.execute(select(Branch).where(Branch.gym_id == gym.id,
                                                   Branch.verification_status == VerificationStatus.VERIFIED))).scalars().first()
    draft = (await db.execute(select(Branch).where(Branch.gym_id == gym.id,
                                                  Branch.verification_status != VerificationStatus.VERIFIED))).scalars().first()
    emp = (await db.execute(select(Employee).where(Employee.gym_id == gym.id))).scalars().first()
    job = (await db.execute(select(JobPosting))).scalars().first()
    thread = (await db.execute(select(ChatThread))).scalars().first()
    report = (await db.execute(select(MisconductReport))).scalars().first()
    sub = (await db.execute(select(Subscription))).scalars().first()
    video = (await db.execute(select(Video))).scalars().first()
    statement = (await db.execute(select(SalaryStatement))).scalars().first()
    doc = (await db.execute(select(IdentityDocument))).scalars().first()
    customer = (await db.execute(select(User).where(User.email == "ishaan@demo.ironpulse.local"))).scalar_one()
    return dict(gym=gym, branch=branch, draft=draft, emp=emp, job=job, thread=thread, report=report, sub=sub,
                video=video, statement=statement, doc=doc, customer=customer)


def _fill(path: str, c: dict) -> str | None:
    def val(name: str) -> str | None:
        p = path
        if name in ("slug", "gym_slug"):
            return c["gym"].slug
        if name == "side":
            return "front"
        if name == "doc_id":
            return str(c["doc"].id) if c["doc"] else None
        if name == "customer_id":
            return str(c["customer"].id)
        if name == "employee_id":
            return str(c["emp"].id)
        if name == "gym_id":
            return str(c["gym"].id)
        if name in ("sid", "subscription_id"):
            return str(c["sub"].id) if c["sub"] else None
        if name == "branch_id":
            return str(c["branch"].id)
        if name != "id":
            return None
        if "/branches/" in p:
            return str(c["branch"].id)
        if p.startswith("/owner/gyms/"):
            return str(c["gym"].id)
        if p.startswith(("/owner/staff/", "/staff/")):
            return str(c["emp"].id)
        if "jobs" in p:
            return str(c["job"].id) if c["job"] else None
        if "chat" in p:
            return str(c["thread"].id) if c["thread"] else None
        if "reports" in p:
            return str(c["report"].id) if c["report"] else None
        if "payroll" in p:
            return str(c["statement"].id) if c["statement"] else None
        if "customers" in p:
            return str(c["customer"].id)
        if "videos" in p:
            return c["video"].slug if c["video"] else None
        return None

    out = path
    for name in re.findall(r"{([a-z_]+)(?::[a-z]+)?}", path):
        v = val(name)
        if v is None:
            return None
        out = re.sub(r"{" + name + r"(?::[a-z]+)?}", v, out, count=1)
    return out


def _iter_routes(routes):
    """Flatten routes; FastAPI >= 0.16x wraps included routers in ``_IncludedRouter`` objects."""
    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from _iter_routes(inner.routes)
        elif isinstance(r, Route):
            yield r


def _get_routes(app) -> list[str]:
    paths = []
    for r in _iter_routes(app.routes):
        if "GET" in (r.methods or set()):
            if r.path in SKIP_PATHS or r.path.startswith(SKIP_PREFIXES):
                continue
            paths.append(r.path)
    return sorted(set(paths))


def test_crawler_actually_finds_routes(app):
    routes = _get_routes(app)
    assert len(routes) > 60, routes  # guard against a vacuous crawl
    assert "/gyms" in routes and "/owner" in routes and "/me" in routes


SMOKE_GYMS = ["forge-and-flow", "harbor-strength", "desert-strength", "pulse-anywhere"]


@pytest.mark.parametrize("role", ["guest", "customer", "minor", "owner", "staff", "superadmin"])
async def test_every_page_renders_for_every_role(app, db, role):
    from app.services.bootstrap import ensure_superadmin
    from scripts.demo.loader import seed_demo

    await ensure_superadmin(db)
    await db.commit()
    # a cross-section of the demo dataset: INR gyms with branches, an AED gym and an online-only gym
    await seed_demo(db, password="Demo-Passw0rd!", gyms=SMOKE_GYMS)
    await db.commit()
    ctx = await _context(db)
    emails = {
        "customer": "ishaan@demo.ironpulse.local", "minor": "anaya@demo.ironpulse.local",
        "owner": "owner@demo.ironpulse.local", "staff": "kabir@demo.ironpulse.local",
    }
    async with make_client() as client:
        if role == "superadmin":
            user = (await db.execute(select(User).where(User.role == Role.SUPERADMIN))).scalars().first()
            if user is None:
                pytest.skip("no super-admin configured")
            await login_as(client, user)
        elif role != "guest":
            user = (await db.execute(select(User).where(User.email == emails[role]))).scalar_one()
            await login_as(client, user)
        failures, heavy = [], []
        for path in _get_routes(app):
            url = _fill(path, ctx)
            if url is None:
                continue
            with QueryCounter() as qc:
                r = await client.get(url)
            if r.status_code >= 500:
                failures.append(f"{r.status_code} {url}")
            elif qc.count > MAX_QUERIES_PER_PAGE:
                heavy.append(f"{qc.count} queries {url}")
        assert not failures, f"[{role}] server errors:\n" + "\n".join(failures)
        assert not heavy, f"[{role}] suspiciously many queries (N+1?):\n" + "\n".join(heavy)
