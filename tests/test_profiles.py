"""Profile pages: everyone has one; privacy levels decide who may see what."""

from __future__ import annotations

from sqlalchemy import select

from app.db import QueryCounter
from app.models import TrainerClient
from app.models.enums import EmploymentStatus, Role, ShiftMode
from tests.conftest import login_as, make_client
from tests.factories import (
    make_branch,
    make_customer,
    make_employee,
    make_gym,
    make_owner,
    make_plans,
    make_shift,
    make_subscription,
    make_user,
)


async def _world(db):
    owner = await make_owner(db, full_name="Olivia Owner")
    other_owner = await make_owner(db, full_name="Oscar Other")
    gym = await make_gym(db, owner, name="Pulse Central")
    branch = await make_branch(db, gym)
    plans = await make_plans(db, gym)
    trainer_user = await make_user(db, role=Role.STAFF, full_name="Tara Trainer")
    trainer = await make_employee(db, gym, user=trainer_user)
    await make_shift(db, trainer, branch=branch, weekday=0, start="07:00", end="11:00")
    customer = await make_customer(db, full_name="Cara Customer", conditions=["Asthma", "lower back pain"])
    await make_subscription(db, customer, plans[0], branch=branch)
    stranger = await make_customer(db, full_name="Sam Stranger")
    return dict(owner=owner, other_owner=other_owner, gym=gym, trainer_user=trainer_user, trainer=trainer,
                customer=customer, stranger=stranger)


async def test_own_profile_redirect_and_private_details(client, db):
    w = await _world(db)
    await login_as(client, w["customer"])
    r = await client.get("/profile")
    assert r.status_code == 303 and r.headers["location"] == f"/profile/{w['customer'].id}"
    r = await client.get(f"/profile/{w['customer'].id}")
    assert r.status_code == 200
    assert w["customer"].email in r.text and "Asthma" in r.text and "Private details" in r.text
    assert "Back support strap" in r.text  # protective-gear advice for the back problem


async def test_owner_and_staff_profiles_are_public(db):
    w = await _world(db)
    async with make_client() as anon:
        r = await anon.get(f"/profile/{w['owner'].id}")
        assert r.status_code == 200 and "Pulse Central" in r.text and w["owner"].email not in r.text
        r = await anon.get(f"/profile/{w['trainer_user'].id}")
        assert r.status_code == 200 and "Weekly schedule" in r.text and "07:00" in r.text


async def test_customer_profile_is_private(db):
    w = await _world(db)
    async with make_client() as anon:
        assert (await anon.get(f"/profile/{w['customer'].id}")).status_code == 404
    async with make_client() as c:
        await login_as(c, w["stranger"])
        assert (await c.get(f"/profile/{w['customer'].id}")).status_code == 404
    async with make_client() as c:
        await login_as(c, w["other_owner"])
        assert (await c.get(f"/profile/{w['customer'].id}")).status_code == 404


async def test_gym_owner_and_assigned_trainer_see_health_info(db):
    w = await _world(db)
    async with make_client() as c:
        await login_as(c, w["owner"])
        r = await c.get(f"/profile/{w['customer'].id}")
        assert r.status_code == 200 and "Asthma" in r.text and "Health &amp; safety" in r.text
        assert w["customer"].email not in r.text  # private box only for self/admin
    async with make_client() as c:
        await login_as(c, w["trainer_user"])
        assert (await c.get(f"/profile/{w['customer'].id}")).status_code == 404  # not assigned yet
    db.add(TrainerClient(employee_id=w["trainer"].id, customer_id=w["customer"].id, mode=ShiftMode.OFFLINE))
    await db.commit()
    async with make_client() as c:
        await login_as(c, w["trainer_user"])
        r = await c.get(f"/profile/{w['customer'].id}")
        assert r.status_code == 200 and "lower back pain" in r.text


async def test_inactive_staff_hidden_and_deleted_users_404(db):
    w = await _world(db)
    w["trainer"].status = EmploymentStatus.SUSPENDED
    await db.commit()
    async with make_client() as anon:
        assert (await anon.get(f"/profile/{w['trainer_user'].id}")).status_code == 404
    from app.services.accounts import soft_delete_user

    cust = (await db.execute(select(type(w["stranger"])).where(type(w["stranger"]).id == w["stranger"].id))).scalar_one()
    await soft_delete_user(db, cust, reason="test")
    await db.commit()
    async with make_client() as c:
        await login_as(c, w["owner"])
        assert (await c.get(f"/profile/{w['stranger'].id}")).status_code == 404


async def test_profile_query_count_is_constant(db):
    w = await _world(db)
    async with make_client() as c:
        await login_as(c, w["owner"])
        with QueryCounter() as qc:
            assert (await c.get(f"/profile/{w['customer'].id}")).status_code == 200
        assert qc.count <= 16
