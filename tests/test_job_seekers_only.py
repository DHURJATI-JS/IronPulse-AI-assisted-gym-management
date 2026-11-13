"""Job enquiries are for job seekers (the owner: "enquire for jobs must be disabled for people in job or gym owners").

A signed-in gym owner (also one who is a gym member somewhere), a super-admin and a staff account with a CURRENT
employment (active or suspended, at any gym) see an explanation instead of the enquiry form - on the job page and the
jobs list - and the enquiry route refuses them before the form is read and before any spam / cooldown accounting
(403 page, or documented JSON). Guests, members and staff whose employment ended enquire as before. The job page's
statement count is the same for every visitor."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select

from app.db import QueryCounter
from app.models import JobEnquiry
from app.models.enums import EmploymentStatus, Role
from tests.conftest import login_as, post_form
from tests.factories import make_customer, make_employee, make_plans, make_subscription, make_user
from tests.test_job_enquiry_spam import form, url
from tests.test_public import make_job, published_gym

BLOCKED = ("owner", "owner_member", "superadmin", "employed", "suspended_staff")
ALLOWED = ("anonymous", "member", "ex_staff", "invited_staff")
FORM = 'action="/jobs/{}/enquire'


async def visitor(db: Any, kind: str) -> Any:
    if kind == "anonymous":
        return None
    if kind == "member":
        return await make_customer(db)
    if kind == "owner":
        return (await published_gym(db))[0]
    if kind == "owner_member":  # a gym owner who also holds a membership at another gym
        owner, gym, _ = await published_gym(db, plans=False)
        other_owner, other, _ = await published_gym(db, plans=False)
        plans = await make_plans(db, other)
        await make_subscription(db, owner, plans[4])
        return owner
    if kind == "superadmin":
        return await make_user(db, role=Role.SUPERADMIN)
    _, gym, _ = await published_gym(db, plans=False)
    staff = await make_user(db, role=Role.STAFF)
    status = {"employed": EmploymentStatus.ACTIVE, "suspended_staff": EmploymentStatus.SUSPENDED,
              "ex_staff": EmploymentStatus.TERMINATED, "invited_staff": EmploymentStatus.INVITED}[kind]
    await make_employee(db, gym, user=staff, status=status)
    return staff


async def enquiries(db: Any) -> int:
    return int(await db.scalar(select(func.count(JobEnquiry.id))) or 0)


@pytest.mark.parametrize("kind", BLOCKED + ALLOWED)
async def test_the_job_page_shows_the_form_only_to_job_seekers(client, db, kind):
    _, gym, _ = await published_gym(db, name="Hiring Gym")
    job = await make_job(db, gym)
    who = await visitor(db, kind)
    if who is not None:
        await login_as(client, who)
    page = await client.get(f"/jobs/{job.id}")
    assert page.status_code == 200
    if kind in BLOCKED:
        assert "Enquiries are for job seekers" in page.text and "data-enquiry-blocked" in page.text
        assert FORM.format(job.id) not in page.text and "Enquire now" not in page.text
        board = await client.get("/jobs")
        assert "data-enquiry-blocked" in board.text and "Enquiries are for job seekers" in board.text
    else:
        assert FORM.format(job.id) in page.text and "data-enquiry-blocked" not in page.text
        assert "data-enquiry-blocked" not in (await client.get("/jobs")).text


async def test_the_reasons_name_the_role(client, db):
    _, gym, _ = await published_gym(db, name="Hiring Gym")
    job = await make_job(db, gym)
    for kind, words in (("owner", "signed in as a gym owner"), ("superadmin", "platform super-admin"),
                        ("employed", "staff member of Pulse Gym")):
        await login_as(client, await visitor(db, kind))
        assert words in (await client.get(f"/jobs/{job.id}")).text


@pytest.mark.parametrize("kind", BLOCKED)
async def test_a_direct_post_is_refused_before_any_spam_accounting(client, db, anon_client_factory, kind):
    _, gym, _ = await published_gym(db, name="Hiring Gym")
    job = await make_job(db, gym)
    await login_as(client, await visitor(db, kind))
    r = await post_form(client, url(job), form(job))
    assert r.status_code == 403 and "Enquiries are for job seekers" in r.text
    assert FORM.format(job.id) not in r.text
    r = await client.post(url(job), data=form(job), headers={"Accept": "application/json",
                                                             "X-CSRF-Token": await _csrf(client)})
    assert r.status_code == 403
    body = r.json()
    assert body["code"] == "job_seekers_only" and body["detail"].startswith("Enquiries are for job seekers")
    assert await enquiries(db) == 0
    # nothing was counted against this network address: a job seeker on it enquires at once (no cooldown)
    seeker = anon_client_factory("127.0.0.1")
    r = await post_form(seeker, url(job), form(job, email="seeker@example.com"))
    assert r.status_code == 303 and await enquiries(db) == 1


@pytest.mark.parametrize("kind", ALLOWED)
async def test_job_seekers_still_enquire(client, db, kind):
    _, gym, _ = await published_gym(db, name="Hiring Gym")
    job = await make_job(db, gym)
    who = await visitor(db, kind)
    if who is not None:
        await login_as(client, who)
    r = await post_form(client, url(job), form(job))
    assert r.status_code == 303, r.text[:300]
    assert await enquiries(db) == 1


async def test_an_ended_employment_makes_a_job_seeker_again(client, db):
    _, gym, _ = await published_gym(db, name="Hiring Gym")
    job = await make_job(db, gym)
    staff = await visitor(db, "employed")
    await login_as(client, staff)
    assert (await post_form(client, url(job), form(job))).status_code == 403
    from app.models import Employee

    emp = await db.scalar(select(Employee).where(Employee.user_id == staff.id))
    emp.status = EmploymentStatus.TERMINATED
    await db.commit()
    assert (await post_form(client, url(job), form(job))).status_code == 303


@pytest.mark.parametrize("kind", ("anonymous", "member", "owner", "superadmin", "employed", "ex_staff"))
async def test_the_job_page_statement_count_is_the_same_for_everyone(client, db, kind):
    _, gym, _ = await published_gym(db, name="Hiring Gym")
    job = await make_job(db, gym)
    who = await visitor(db, kind)
    if who is not None:
        await login_as(client, who)
    with QueryCounter() as qc:
        r = await client.get(f"/jobs/{job.id}")
    assert r.status_code == 200
    assert qc.count == 2 + (who is not None)  # posting (+ the employment check) + "more roles"; + the session lookup


async def _csrf(client: Any) -> str:
    from tests.conftest import csrf_token

    return await csrf_token(client)
