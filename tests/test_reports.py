"""WP-F misconduct reports: who may report whom, validation, owner replies (apology / reason), notifications."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from app.core import caps
from app.db import QueryCounter
from app.models import AuditLog, MisconductReport, Notification
from app.models.enums import ReportStatus, SubscriptionStatus
from tests.conftest import login_as, make_client, post_form
from tests.factories import make_customer, make_employee, make_gym, make_owner, make_plans, make_subscription


async def world(db) -> dict[str, Any]:
    owner = await make_owner(db, full_name="Olivia Owner")
    gym = await make_gym(db, owner, name="Pulse Koramangala")
    plans = await make_plans(db, gym)
    trainer = await make_employee(db, gym)
    member = await make_customer(db, full_name="Maya Member")
    await make_subscription(db, member, plans[0])
    return {"owner": owner, "gym": gym, "plans": plans, "trainer": trainer, "member": member}


def report_form(emp_id: int, **over: Any) -> dict[str, Any]:
    data = {"employee_id": str(emp_id), "category": "rude", "incident_date": date.today().isoformat(),
            "description": "The trainer shouted at me in front of everyone during the evening class."}
    data.update(over)
    return data


async def file_report(db, w: dict[str, Any]) -> MisconductReport:
    async with make_client() as c:
        await login_as(c, w["member"])
        r = await post_form(c, "/reports/new", report_form(w["trainer"].id))
        assert r.status_code == 303, r.text[-1500:]
    return (await db.execute(select(MisconductReport))).scalar_one()


def no_csp_violations(html: str) -> None:
    assert "<script>" not in html and " onclick=" not in html and ' style="' not in html


# ====================================================================== filing a report
async def test_member_files_report_owner_notified_and_audited(client, db):
    w = await world(db)
    await login_as(client, w["member"])
    r = await client.get(f"/reports/new?employee={w['trainer'].id}")
    assert r.status_code == 200 and w["trainer"].full_name in r.text
    no_csp_violations(r.text)
    r = await post_form(client, "/reports/new", report_form(w["trainer"].id))
    assert r.status_code == 303 and r.headers["location"] == "/reports"
    report = (await db.execute(select(MisconductReport))).scalar_one()
    assert report.status == ReportStatus.OPEN and report.gym_id == w["gym"].id and report.reporter_id == w["member"].id
    assert report.category == "rude" and report.incident_date == date.today()
    owner_note = (await db.execute(select(Notification).where(Notification.user_id == w["owner"].id))).scalar_one()
    assert owner_note.link == f"/owner/reports/{report.id}" and "misconduct" in owner_note.title.lower()
    assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "report.create")) == 1
    r = await client.get("/reports")
    assert r.status_code == 200 and "Awaiting reply" in r.text and "shouted at me" in r.text


async def test_report_validation(client, db):
    w = await world(db)
    await login_as(client, w["member"])
    cases = [
        report_form(w["trainer"].id, description="Too short"),
        report_form(w["trainer"].id, description="x" * 4001),
        report_form(w["trainer"].id, category="annoying"),
        report_form(w["trainer"].id, incident_date=(date.today() + timedelta(days=1)).isoformat()),
        report_form(w["trainer"].id, incident_date=(date.today() - timedelta(days=400)).isoformat()),
        report_form(w["trainer"].id, incident_date="yesterday"),
        report_form(0),
        report_form(w["trainer"].id, employee_id="abc"),
    ]
    for data in cases:
        r = await post_form(client, "/reports/new", data)
        assert r.status_code == 400, data
        assert "Please fix the following" in r.text
    assert await db.scalar(select(func.count(MisconductReport.id))) == 0


async def test_only_staff_of_gyms_with_membership_can_be_reported(client, db):
    w = await world(db)
    other_owner = await make_owner(db)
    other_gym = await make_gym(db, other_owner)
    foreign = await make_employee(db, other_gym)
    await login_as(client, w["member"])
    r = await post_form(client, "/reports/new", report_form(foreign.id))
    assert r.status_code == 400
    # a non-member sees the empty state and cannot report anyone
    outsider = await make_customer(db)
    async with make_client() as c:
        await login_as(c, outsider)
        r = await c.get("/reports/new")
        assert r.status_code == 200 and "No staff to report" in r.text
        r = await post_form(c, "/reports/new", report_form(w["trainer"].id))
        assert r.status_code == 400
    # a former member (expired plan) still can
    former = await make_customer(db)
    await make_subscription(db, former, w["plans"][0], status=SubscriptionStatus.EXPIRED)
    async with make_client() as c:
        await login_as(c, former)
        r = await post_form(c, "/reports/new", report_form(w["trainer"].id))
        assert r.status_code == 303
    # owners and staff cannot use the customer form
    async with make_client() as c:
        await login_as(c, w["owner"])
        assert (await c.get("/reports/new")).status_code == 403


async def test_duplicate_open_report_is_blocked(client, db):
    w = await world(db)
    await file_report(db, w)
    await login_as(client, w["member"])
    r = await post_form(client, "/reports/new", report_form(w["trainer"].id))
    assert r.status_code == 400 and "already have an open report" in r.text
    assert await db.scalar(select(func.count(MisconductReport.id))) == 1


# ====================================================================== owner replies
async def test_accept_requires_an_apology(client, db):
    w = await world(db)
    report = await file_report(db, w)
    await login_as(client, w["owner"])
    r = await client.get(f"/owner/reports/{report.id}")
    assert r.status_code == 200 and "shouted at me" in r.text
    no_csp_violations(r.text)
    for data in ({"decision": "accept", "response": "Ok"}, {"decision": "accept", "response": "We will look into the matter."},
                 {"decision": "maybe", "response": "We're sorry about this."}, {"decision": "", "response": ""}):
        r = await post_form(client, f"/owner/reports/{report.id}", data)
        assert r.status_code == 400, data
    await db.refresh(report)
    assert report.status == ReportStatus.OPEN

    apology = "We're sorry this happened. We spoke with the trainer and it won't happen again."
    r = await post_form(client, f"/owner/reports/{report.id}", {"decision": "accept", "response": apology})
    assert r.status_code == 303
    await db.refresh(report)
    assert report.status == ReportStatus.ACCEPTED and report.admin_response == apology
    assert report.resolved_by_id == w["owner"].id and report.resolved_at is not None
    note = (await db.execute(select(Notification).where(Notification.user_id == w["member"].id)
                             .order_by(Notification.id.desc()))).scalars().first()
    assert note.title == "Your report was accepted" and "spoke with the trainer" in note.body
    assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "report.accept")) == 1
    # answering twice is not possible
    r = await post_form(client, f"/owner/reports/{report.id}", {"decision": "decline", "response": "Changed my mind entirely"})
    assert r.status_code == 303
    await db.refresh(report)
    assert report.status == ReportStatus.ACCEPTED
    async with make_client() as c:
        await login_as(c, w["member"])
        r = await c.get("/reports")
        assert "Accepted" in r.text and "spoke with the trainer" in r.text


async def test_decline_requires_a_reason(client, db):
    w = await world(db)
    report = await file_report(db, w)
    await login_as(client, w["owner"])
    r = await post_form(client, f"/owner/reports/{report.id}", {"decision": "decline", "response": "No."})
    assert r.status_code == 400 and "Explain why" in r.text
    reason = "We reviewed the CCTV and the class was conducted normally."
    r = await post_form(client, f"/owner/reports/{report.id}", {"decision": "decline", "response": reason})
    assert r.status_code == 303
    await db.refresh(report)
    assert report.status == ReportStatus.DECLINED and report.admin_response == reason
    note = (await db.execute(select(Notification).where(Notification.user_id == w["member"].id)
                             .order_by(Notification.id.desc()))).scalars().first()
    assert "declined" in note.body and "CCTV" in note.body
    assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "report.decline")) == 1


async def test_other_owner_cannot_see_or_answer(client, db):
    w = await world(db)
    report = await file_report(db, w)
    other_owner = await make_owner(db)
    await make_gym(db, other_owner)
    await login_as(client, other_owner)
    assert (await client.get(f"/owner/reports/{report.id}")).status_code == 404
    r = await post_form(client, f"/owner/reports/{report.id}", {"decision": "decline", "response": "Not my problem at all"})
    assert r.status_code == 404
    r = await client.get("/owner/reports")
    assert r.status_code == 200 and "shouted at me" not in r.text
    async with make_client() as c:
        await login_as(c, w["member"])
        assert (await c.get(f"/owner/reports/{report.id}")).status_code == 403


async def test_owner_list_filters_and_constant_queries(db):
    w = await world(db)
    await file_report(db, w)

    async def add_reports(n: int) -> None:
        for _ in range(n):
            m = await make_customer(db)
            await make_subscription(db, m, w["plans"][0])
            db.add(MisconductReport(gym_id=w["gym"].id, employee_id=w["trainer"].id, reporter_id=m.id,
                                    category="negligence", description="Left me alone with heavy weights on the bench.",
                                    incident_date=date.today()))
        await db.commit()

    async def count() -> int:
        async with make_client() as c:
            await login_as(c, w["owner"])
            with QueryCounter() as qc:
                r = await c.get("/owner/reports")
            assert r.status_code == 200
            no_csp_violations(r.text)
            return qc.count

    await add_reports(2)
    small = await count()
    await add_reports(12)
    assert await count() == small
    async with make_client() as c:
        await login_as(c, w["owner"])
        r = await c.get("/owner/reports?status=open&q=heavy")
        assert r.status_code == 200 and "heavy weights" in r.text and "shouted at me" not in r.text
        r = await c.get("/owner/reports?status=accepted")
        assert "No reports match" in r.text


# ====================================================================== race-safe duplicates and caps (F12)
async def test_parallel_submits_file_exactly_one_report(db):
    """F12: "one open report per staff member" holds under parallel submits (several tabs, a script): the member's
    report lock serialises them - one report and one owner notification, the rest get the friendly duplicate error."""
    w = await world(db)
    async with make_client() as c:
        await login_as(c, w["member"])
        results = await asyncio.gather(*(post_form(c, "/reports/new", report_form(w["trainer"].id)) for _ in range(12)))
    codes = sorted(r.status_code for r in results)
    assert codes.count(303) == 1 and codes.count(400) == 11, codes
    assert all("already have an open report" in r.text for r in results if r.status_code == 400)
    assert await db.scalar(select(func.count(MisconductReport.id))) == 1
    assert await db.scalar(select(func.count(Notification.id)).where(
        Notification.user_id == w["owner"].id, Notification.title == "New misconduct report")) == 1


@pytest.fixture
def low_caps(monkeypatch):
    values: dict[str, int] = {}
    real = caps.limit

    def fake(name: str) -> int:
        return values.get(name) or real(name)

    monkeypatch.setattr(caps, "limit", fake)
    return values


async def test_report_caps_are_friendly(db, low_caps):
    w = await world(db)
    second = await make_employee(db, w["gym"])
    low_caps["open_reports_per_member"] = 1
    async with make_client() as c:
        await login_as(c, w["member"])
        assert (await post_form(c, "/reports/new", report_form(w["trainer"].id))).status_code == 303
        r = await post_form(c, "/reports/new", report_form(second.id))
        assert r.status_code == 409 and "waiting for a reply" in r.text
    report = (await db.execute(select(MisconductReport))).scalar_one()
    report.status, report.admin_response = ReportStatus.ACCEPTED, "We're sorry - we have spoken to the trainer."
    await db.commit()
    low_caps["reports_per_member_day"] = 1  # answered, so the open cap has room - the daily one has not
    async with make_client() as c:
        await login_as(c, w["member"])
        r = await post_form(c, "/reports/new", report_form(second.id))
        assert r.status_code == 429 and "in the last 24 hours" in r.text
    assert await db.scalar(select(func.count(MisconductReport.id))) == 1
