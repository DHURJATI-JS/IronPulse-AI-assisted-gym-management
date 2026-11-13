"""WP-C1 hiring: job postings (create/edit/toggle -> navbar "Enquire for jobs"), enquiries (status e-mails,
résumé download), ownership checks and constant query counts."""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import func, select

from app.core.nav import nav_state, refresh_nav_state
from app.db import QueryCounter
from app.models import AuditLog, EmailOutbox, JobEnquiry, JobPosting
from app.models.enums import EmploymentType, EmployeeRole, EnquiryStatus, Role
from app.services import staffing as S
from app.services import storage
from tests.conftest import login_as, post_form
from tests.factories import make_branch, make_gym, make_owner, make_user, png_bytes


def job_form(**overrides: str) -> dict[str, str]:
    data = {
        "title": "Strength & conditioning coach", "role": "trainer", "employment_type": "full_time", "branch_id": "",
        "location": "Bengaluru", "salary_min": "30000", "salary_max": "45000", "currency": "INR",
        "salary_period": "month", "requirements": "ACE or NASM certification and 2+ years of coaching",
        "description": "Morning shifts, small groups.", "is_open": "1",
    }
    data.update(overrides)
    return data


async def make_job(db, gym, *, is_open: bool = True, title: str = "Front desk lead") -> JobPosting:
    job = JobPosting(gym_id=gym.id, title=title, role=EmployeeRole.RECEPTIONIST, employment_type=EmploymentType.PART_TIME,
                     location="Bengaluru", currency="INR", requirements="Friendly, organised, English + Kannada",
                     is_open=is_open)
    db.add(job)
    await db.commit()
    return job


async def make_enquiry(db, job, *, name: str = "Vikram Singh", email: str = "vikram@example.com",
                       status: EnquiryStatus = EnquiryStatus.NEW, resume: bool = False) -> JobEnquiry:
    key = None
    if resume:
        stored, _ = await storage.save_private_file(png_bytes(text="CV"), kind="resumes")
        key = stored.key
    enq = JobEnquiry(job_id=job.id, name=name, email=email, phone="+91 98450 12345",
                     message="I'd love to join - 4 years of experience.", resume_key=key, status=status)
    db.add(enq)
    await db.commit()
    return enq


async def test_create_job_validation_and_nav_state(client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    branch = await make_branch(db, gym, city="Bengaluru")
    other_gym_branch = await make_branch(db, await make_gym(db, await make_owner(db)))
    await login_as(client, owner)
    url = f"/owner/gyms/{gym.id}/jobs/new"
    assert (await client.get(url)).status_code == 200
    cases = [
        (job_form(requirements=""), "Requirements are mandatory"),
        (job_form(title="x"), "Give the position a title"),
        (job_form(salary_min="50000", salary_max="40000"), "at least the minimum"),
        (job_form(salary_min="abc"), "plain number"),
        (job_form(branch_id=str(other_gym_branch.id)), "Choose one of this gym"),
        (job_form(location="", branch_id=""), "Enter a city"),
        (job_form(salary_period="week"), "per month, per year or per hour"),
        (job_form(currency="RUPEES"), "3-letter ISO currency"),
        (job_form(role="wizard"), "Choose a role."),
    ]
    for data, message in cases:
        r = await post_form(client, url, data)
        assert r.status_code == 400, message
        assert message in r.text.replace("&#39;", "'"), message
    assert await db.scalar(select(func.count(JobPosting.id))) == 0

    await refresh_nav_state(db)
    assert nav_state.open_jobs == 0
    r = await post_form(client, url, job_form(branch_id=str(branch.id), location=""))
    assert r.status_code == 303
    job = await db.scalar(select(JobPosting))
    assert job.location == "Bengaluru" and job.branch_id == branch.id and job.salary_min == Decimal("30000.00")
    assert nav_state.open_jobs == 1
    page = await client.get("/owner/jobs")
    assert "Enquire for jobs" in page.text and "live in the navigation" in page.text

    r = await post_form(client, url, job_form(title="Remote nutrition coach", remote="1", location="", is_open=""))
    assert r.status_code == 303
    remote = await db.scalar(select(JobPosting).where(JobPosting.title == "Remote nutrition coach"))
    assert remote.location == "Remote" and remote.is_open is False
    assert nav_state.open_jobs == 1
    assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "job.create")) == 2


async def test_toggle_updates_nav_state(client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    job = await make_job(db, gym, is_open=True)
    await refresh_nav_state(db)
    assert nav_state.open_jobs == 1
    await login_as(client, owner)
    r = await post_form(client, f"/owner/jobs/{job.id}/toggle", {"next": "/owner/jobs"})
    assert r.status_code == 303 and r.headers["location"] == "/owner/jobs"
    assert nav_state.open_jobs == 0
    page = await client.get("/owner/staff")
    assert "Enquire for jobs" not in page.text  # the navbar entry disappeared
    r = await post_form(client, f"/owner/jobs/{job.id}/toggle")
    assert r.status_code == 303 and r.headers["location"] == f"/owner/jobs/{job.id}"
    assert nav_state.open_jobs == 1
    page = await client.get("/owner/staff")
    assert "Enquire for jobs" in page.text
    actions = (await db.execute(select(AuditLog.action).order_by(AuditLog.id))).scalars().all()
    assert actions == ["job.close", "job.open"]


async def test_edit_job(client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    job = await make_job(db, gym)
    await login_as(client, owner)
    page = await client.get(f"/owner/jobs/{job.id}")
    assert page.status_code == 200 and "Front desk lead" in page.text
    values = S.job_values_from_posting(job)
    values.update(title="Front desk supervisor", salary_max="28000", salary_period="month", remote="1")
    values.pop("is_open")
    r = await post_form(client, f"/owner/jobs/{job.id}", {k: v for k, v in values.items() if v not in (False, None)})
    assert r.status_code == 303
    await db.refresh(job)
    assert job.title == "Front desk supervisor" and job.salary_max == Decimal("28000.00") and job.location == "Remote"
    values.update(requirements="short")
    r = await post_form(client, f"/owner/jobs/{job.id}", {k: v for k, v in values.items() if v not in (False, None)})
    assert r.status_code == 400 and "Requirements are mandatory" in r.text


async def test_jobs_ownership(client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    job = await make_job(db, gym)
    enq = await make_enquiry(db, job, resume=True)
    intruder = await make_owner(db)
    await login_as(client, intruder)
    assert (await client.get(f"/owner/jobs/{job.id}")).status_code == 404
    assert (await client.get(f"/owner/jobs/{job.id}/enquiries")).status_code == 404
    assert (await client.get(f"/owner/enquiries/{enq.id}/resume")).status_code == 404
    assert (await client.get(f"/owner/gyms/{gym.id}/jobs/new")).status_code == 404
    assert (await post_form(client, f"/owner/jobs/{job.id}", job_form())).status_code == 404
    assert (await post_form(client, f"/owner/jobs/{job.id}/toggle")).status_code == 404
    assert (await post_form(client, f"/owner/enquiries/{enq.id}/status", {"status": "rejected"})).status_code == 404
    listing = await client.get("/owner/jobs")
    assert listing.status_code == 200 and "Front desk lead" not in listing.text
    await db.refresh(enq)
    assert enq.status == EnquiryStatus.NEW
    staff = await make_user(db, role=Role.STAFF)
    await login_as(client, staff)
    assert (await client.get("/owner/jobs")).status_code == 403


async def test_enquiry_status_emails_the_applicant(client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner, name="PulseFit")
    job = await make_job(db, gym, title="Front desk lead")
    enq = await make_enquiry(db, job)
    await login_as(client, owner)
    page = await client.get(f"/owner/jobs/{job.id}/enquiries")
    assert page.status_code == 200 and "Vikram Singh" in page.text and "4 years of experience" in page.text
    r = await post_form(client, f"/owner/enquiries/{enq.id}/status", {"status": "contacted"})
    assert r.status_code == 303 and r.headers["location"] == f"/owner/jobs/{job.id}/enquiries"
    await db.refresh(enq)
    assert enq.status == EnquiryStatus.CONTACTED
    mail = await db.scalar(select(EmailOutbox).where(EmailOutbox.to_email == "vikram@example.com"))
    assert mail is not None and "PulseFit would like to talk to you" in mail.subject and "Front desk lead" in mail.text_body
    assert await db.scalar(select(AuditLog).where(AuditLog.action == "job.enquiry_status"))
    # unchanged status -> no new mail; invalid status -> 400
    await post_form(client, f"/owner/enquiries/{enq.id}/status", {"status": "contacted"})
    r = await post_form(client, f"/owner/enquiries/{enq.id}/status", {"status": "ghosted"})
    assert r.status_code == 400 and "Choose a valid status" in r.text
    assert await db.scalar(select(func.count(EmailOutbox.id))) == 1
    r = await post_form(client, f"/owner/enquiries/{enq.id}/status", {"status": "rejected"})
    assert r.status_code == 303
    subjects = (await db.execute(select(EmailOutbox.subject).order_by(EmailOutbox.id))).scalars().all()
    assert len(subjects) == 2 and "Update on your application" in subjects[1]
    r = await post_form(client, f"/owner/enquiries/{enq.id}/status", {"status": "hired"})
    assert r.status_code == 303
    page = await client.get(f"/owner/jobs/{job.id}/enquiries?status=hired")
    assert "Add as staff" in page.text
    link = next(part for part in page.text.split('"') if part.startswith(f"/owner/gyms/{gym.id}/staff/new?"))
    query = parse_qs(urlsplit(link.replace("&amp;", "&")).query)
    assert query == {"name": ["Vikram Singh"], "email": ["vikram@example.com"], "role": ["receptionist"]}
    form = await client.get(link.replace("&amp;", "&"))
    assert form.status_code == 200 and 'value="Vikram Singh"' in form.text and 'value="vikram@example.com"' in form.text


def test_enquiry_status_email_texts():
    for status in (EnquiryStatus.REVIEWED, EnquiryStatus.CONTACTED, EnquiryStatus.REJECTED, EnquiryStatus.HIRED):
        title, body = S.enquiry_status_email(status, job_title="Coach", gym_name="PulseFit")
        assert title and "PulseFit" in body
    assert S.enquiry_status_email(EnquiryStatus.NEW, job_title="Coach", gym_name="PulseFit") is None


async def test_resume_download_is_authorised(client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    job = await make_job(db, gym)
    with_cv = await make_enquiry(db, job, resume=True)
    without = await make_enquiry(db, job, name="No CV", email="nocv@example.com")
    await login_as(client, owner)
    r = await client.get(f"/owner/enquiries/{with_cv.id}/resume")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert "no-store" in r.headers["cache-control"]
    assert (await client.get(f"/owner/enquiries/{without.id}/resume")).status_code == 404


async def test_jobs_new_redirects(client, db):
    owner = await make_owner(db)
    await login_as(client, owner)
    r = await client.get("/owner/jobs")
    assert r.status_code == 200 and "Create a gym to start hiring" in r.text
    r = await client.get("/owner/jobs/new")
    assert r.status_code == 303 and r.headers["location"] == "/owner/gyms/new"
    gym = await make_gym(db, owner)
    r = await client.get("/owner/jobs/new")
    assert r.status_code == 303 and r.headers["location"] == f"/owner/gyms/{gym.id}/jobs/new"
    assert "No job postings yet" in (await client.get("/owner/jobs")).text


async def test_jobs_list_constant_queries(client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    branch = await make_branch(db, gym)

    async def add(n: int) -> None:
        for i in range(n):
            job = await make_job(db, gym, title=f"Role {i}", is_open=i % 2 == 0)
            job.branch_id = branch.id
            await db.commit()
            await make_enquiry(db, job, email=f"a{i}@example.com")
            await make_enquiry(db, job, email=f"b{i}@example.com", status=EnquiryStatus.HIRED)

    await add(1)
    await login_as(client, owner)
    await client.get("/owner/jobs")
    with QueryCounter() as qc:
        r = await client.get("/owner/jobs")
    few = qc.count
    assert r.status_code == 200
    await add(5)
    with QueryCounter() as qc:
        r = await client.get("/owner/jobs")
    assert r.status_code == 200 and r.text.count("enquiries</a>") == 6
    assert qc.count == few
    job = await db.scalar(select(JobPosting).limit(1))
    with QueryCounter() as qc:
        r = await client.get(f"/owner/jobs/{job.id}/enquiries")
    one = qc.count
    for i in range(4):
        await make_enquiry(db, job, email=f"c{i}@example.com")
    with QueryCounter() as qc:
        await client.get(f"/owner/jobs/{job.id}/enquiries")
    assert qc.count == one
