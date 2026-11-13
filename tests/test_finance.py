"""WP-C2 finance + payroll routes: pages, ownership, payouts, proofs, settlement/termination, CSV, N+1 guards."""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select

from app.db import QueryCounter
from app.models import AuditLog, Job, SalaryStatement, TrainerShift, Transaction
from app.models.enums import EmploymentStatus, Role, StatementStatus, TxCategory, TxDirection, TxStatus
from app.services import payroll as pr
from tests.conftest import login_as, post_form, run_jobs
from tests.factories import make_branch, make_customer, make_employee, make_gym, make_owner, make_shift, make_user, png_bytes

D = Decimal
TODAY = date.today()
PREV = TODAY.replace(day=1) - timedelta(days=1)


# ============================================================================================ helpers
async def staff(db, *, country="IN", currency="INR", salary="30000", hire=date(2019, 1, 15), created=None):
    owner = await make_owner(db)
    gym = await make_gym(db, owner, country=country, currency=currency)
    emp = await make_employee(db, gym, salary=salary, hire_date=hire, country=country, currency=currency)
    if created is not None:
        emp.created_at = created
        await db.commit()
    return owner, gym, emp


async def statement(db, emp, *, start=None, end=None, status=StatementStatus.DUE, due=None):
    start = start or PREV.replace(day=1)
    end = end or PREV
    draft = pr.compute_statement(monthly_salary=emp.monthly_salary, currency=emp.currency, country_code=emp.country_code,
                                 hire_date=emp.hire_date, period_start=start, period_end=end)
    st = SalaryStatement(**draft.model_kwargs(), employee_id=emp.id, gym_id=emp.gym_id, status=status,
                         due_date=due or pr.due_date_for(end))
    db.add(st)
    await db.commit()
    return st


def tx(gym, *, direction=TxDirection.INCOMING, amount="1000", status=TxStatus.SUCCESS, category=TxCategory.SUBSCRIPTION,
       description="Membership", reference=None, currency="INR", completed_at=None):
    return Transaction(gym_id=gym.id, direction=direction, category=category, status=status, amount=D(amount),
                       currency=currency, description=description, provider="test", provider_reference=reference,
                       completed_at=completed_at)


def fake_ocr(monkeypatch, text):
    from app.services import ocr
    from app.services.ocr import MultiOcrResult, OcrEngineResult

    async def _ocr(data, content_type, **kwargs):
        return MultiOcrResult([OcrEngineResult("tesseract", True, text, 0.9)])

    monkeypatch.setattr(ocr, "ocr_document", _ocr)


def proof_file(color="white"):
    return {"proof": ("receipt.png", png_bytes(color=color, text="paid"), "image/png")}


# ============================================================================================ pages + access control
async def test_owner_pages_render(client, db):
    owner, gym, emp = await staff(db)
    db.add(tx(gym, amount="1499", description="Offline plan - Asha"))
    await db.commit()
    st = await statement(db, emp)
    await login_as(client, owner)
    expectations = {
        "/owner/finance": ["Money in &amp; out", "Last 12 months", "Salaries to pay", "Offline plan - Asha"],
        "/owner/finance/transactions": ["Transactions", "Offline plan - Asha", "Export CSV"],
        "/owner/payroll": ["Run payroll", emp.display_name, "₹28,200.00"],
        f"/owner/payroll/{st.id}": ["Breakdown", "Salary by days", "EPF - employee 12%", "Payout instructions",
                                    "How this was calculated", "Upload &amp; verify"],
        f"/owner/staff/{emp.id}/settlement": ["Outstanding salary", "Final settlement", "End the employment"],
    }
    for url, needles in expectations.items():
        r = await client.get(url)
        assert r.status_code == 200, (url, r.text[:500])
        for n in needles:
            assert n in r.text, (url, n)
        main = r.text[r.text.index("<main"):r.text.index("</main>")]
        assert "<script" not in main and " onclick=" not in main and ' style="' not in main


async def test_anonymous_customer_and_staff_are_kept_out(db, anon_client_factory):
    owner, gym, emp = await staff(db)
    st = await statement(db, emp)
    anon = anon_client_factory()
    r = await anon.get("/owner/finance")
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")
    for i, user in enumerate((await make_customer(db), await make_user(db, role=Role.STAFF))):
        c = anon_client_factory(f"203.0.113.{40 + i}")
        await login_as(c, user)
        for url in ("/owner/finance", "/owner/payroll", f"/owner/payroll/{st.id}", "/owner/finance/transactions.csv",
                    f"/owner/staff/{emp.id}/settlement"):
            assert (await c.get(url)).status_code == 403, url
        assert (await post_form(c, f"/owner/payroll/{st.id}/pay")).status_code == 403


async def test_other_owners_get_404_everywhere(client, db):
    owner, gym, emp = await staff(db)
    st = await statement(db, emp)
    intruder = await make_owner(db)
    await make_gym(db, intruder)
    await login_as(client, intruder)
    for url in (f"/owner/payroll/{st.id}", f"/owner/payroll/{st.id}/status", f"/owner/payroll/{st.id}/proof",
                f"/owner/staff/{emp.id}/settlement", f"/owner/finance/transactions?gym={gym.id}",
                f"/owner/finance?gym={gym.id}", f"/owner/payroll?employee={emp.id}", f"/owner/payroll?gym={gym.id}",
                f"/owner/finance/transactions.csv?gym={gym.id}"):
        assert (await client.get(url)).status_code == 404, url
    for url, data in ((f"/owner/payroll/{st.id}/pay", {}), (f"/owner/payroll/{st.id}/confirm", {"justification": "x" * 20}),
                      (f"/owner/payroll/{st.id}/adjust", {"unpaid_leave_days": "1"}), (f"/owner/payroll/{st.id}/explain", {}),
                      (f"/owner/staff/{emp.id}/settlement", {"termination_date": TODAY.isoformat(), "reason": "resignation"}),
                      (f"/owner/staff/{emp.id}/terminate", {"confirm": "1"}),
                      ("/owner/payroll/generate", {"month": f"{PREV:%Y-%m}", "gym": str(gym.id)})):
        assert (await post_form(client, url, data)).status_code == 404, url
    r = await post_form(client, f"/owner/payroll/{st.id}/proof", {"reference": "UTR12345678", "amount": "28200"},
                        files=proof_file())
    assert r.status_code == 404
    await db.refresh(st)
    await db.refresh(emp)
    assert st.status == StatementStatus.DUE and emp.status == EmploymentStatus.ACTIVE
    assert await db.scalar(select(func.count(SalaryStatement.id))) == 1
    db.add(tx(gym, description="Victim income"))
    await db.commit()
    assert "Victim income" not in (await client.get("/owner/finance/transactions")).text
    assert "Victim income" not in (await client.get("/owner/finance/transactions.csv")).text
    assert "Victim income" not in (await client.get("/owner/finance")).text


# ============================================================================================ generate
async def test_generate_route_creates_once_and_validates(client, db):
    owner, gym, emp = await staff(db)
    await login_as(client, owner)
    r = await post_form(client, "/owner/payroll/generate", {"month": f"{PREV:%Y-%m}", "gym": ""})
    assert r.status_code == 303 and r.headers["location"] == "/owner/payroll?status=unpaid"
    assert await db.scalar(select(func.count(SalaryStatement.id))) == 1
    r = await post_form(client, "/owner/payroll/generate", {"month": f"{PREV:%Y-%m}", "gym": str(gym.id)})
    assert r.status_code == 303 and r.headers["location"] == "/owner/payroll"
    assert await db.scalar(select(func.count(SalaryStatement.id))) == 1
    future = TODAY.replace(day=28) + timedelta(days=5)
    for month, needle in (("2026-13", "Choose a month"), ("garbage", "Choose a month"),
                          (f"{future:%Y-%m}", "future month"), ("2001-01", "last 36 months")):
        r = await post_form(client, "/owner/payroll/generate", {"month": month})
        assert r.status_code == 400 and needle in r.text, month
    assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "payroll.generate")) == 2


# ============================================================================================ payout + proof + confirm
async def test_test_mode_payout_route(client, db):
    owner, gym, emp = await staff(db)
    st = await statement(db, emp)
    await login_as(client, owner)
    page = await client.get(f"/owner/payroll/{st.id}")
    assert "Simulate payout (test mode)" in page.text
    r = await post_form(client, f"/owner/payroll/{st.id}/pay")
    assert r.status_code == 303 and r.headers["location"].endswith("#proof")
    await db.refresh(st)
    payment = await db.get(Transaction, st.transaction_id)
    assert st.status == StatementStatus.AWAITING_PROOF and payment.provider == "test"
    assert payment.status == TxStatus.AWAITING_PROOF and payment.direction == TxDirection.OUTGOING
    page = await client.get(f"/owner/payroll/{st.id}")
    assert payment.provider_reference in page.text  # the proof form is pre-filled with the payout reference
    again = await post_form(client, f"/owner/payroll/{st.id}/pay")
    assert again.status_code == 400 and "only possible while the statement is due" in again.text
    owner2, gym2, emp2 = await staff(db, country="AE", currency="AED")
    st2 = await statement(db, emp2)
    await login_as(client, owner2)
    r = await post_form(client, f"/owner/payroll/{st2.id}/pay")
    assert r.status_code == 400 and "INR" in r.text


async def test_proof_route_validates_then_verifies(client, db, monkeypatch):
    owner, gym, emp = await staff(db)
    st = await statement(db, emp)
    await login_as(client, owner)
    url = f"/owner/payroll/{st.id}/proof"
    good = {"reference": "UTR412345678901", "amount": "28200.00", "paid_on": TODAY.isoformat()}
    cases = [
        ({**good}, None, "Attach the proof"),
        ({**good, "reference": "x"}, proof_file(), "bank reference"),
        ({**good, "amount": "27000"}, proof_file(), "must match the net pay"),
        ({**good, "amount": "abc"}, proof_file(), "Enter the amount"),
        ({**good, "paid_on": (TODAY + timedelta(days=3)).isoformat()}, proof_file(), "future"),
        ({**good}, {"proof": ("note.txt", b"definitely not an image", "text/plain")}, "not a valid image"),
    ]
    for data, files, needle in cases:
        r = await post_form(client, url, data, files=files)
        assert r.status_code == 400 and needle in r.text, (needle, r.status_code)
    await db.refresh(st)
    assert st.status == StatementStatus.DUE
    r = await post_form(client, url, good, files=proof_file())
    assert r.status_code == 303
    await db.refresh(st)
    assert st.status == StatementStatus.PROOF_REVIEW
    assert (await client.get(f"/owner/payroll/{st.id}/status")).json()["status"] == "proof_review"
    busy = await post_form(client, url, good, files=proof_file())
    assert busy.status_code == 400 and "already being verified" in busy.text
    fake_ocr(monkeypatch, f"IMPS transfer successful UTR 412345678901 amount INR 28,200.00 date {TODAY:%d/%m/%Y}")
    assert await run_jobs() >= 1
    await db.refresh(st)
    assert st.status == StatementStatus.PAID
    payment = await db.get(Transaction, st.transaction_id)
    await db.refresh(payment)
    assert payment.status == TxStatus.SUCCESS and payment.proof_verified is True and payment.provider == "manual"
    page = await client.get(f"/owner/payroll/{st.id}")
    assert "Payment recorded" in page.text and "View proof" in page.text
    f = await client.get(f"/owner/payroll/{st.id}/proof")
    assert f.status_code == 200 and f.headers["content-type"] == "image/jpeg"
    assert f.headers["cache-control"] == "no-store" and f.content[:2] == b"\xff\xd8"


async def test_mismatch_then_confirm_route(client, db, monkeypatch):
    owner, gym, emp = await staff(db)
    st = await statement(db, emp)
    await login_as(client, owner)
    await post_form(client, f"/owner/payroll/{st.id}/proof",
                    {"reference": "UTR555666777", "amount": "28200", "paid_on": TODAY.isoformat()}, files=proof_file())
    fake_ocr(monkeypatch, "blurry screenshot with no numbers")
    await run_jobs()
    await db.refresh(st)
    assert st.status == StatementStatus.AWAITING_PROOF
    page = await client.get(f"/owner/payroll/{st.id}")
    assert "The last proof didn" in page.text and "Confirm despite the mismatch" in page.text
    short = await post_form(client, f"/owner/payroll/{st.id}/confirm", {"justification": "ok"})
    assert short.status_code == 400 and "at least 15 characters" in short.text
    ok = await post_form(client, f"/owner/payroll/{st.id}/confirm",
                         {"justification": "Paid from the HDFC current account, screenshot was cropped by the app"})
    assert ok.status_code == 303
    await db.refresh(st)
    assert st.status == StatementStatus.PAID
    assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "payroll.confirm_mismatch")) == 1
    late = await post_form(client, f"/owner/payroll/{st.id}/confirm", {"justification": "x" * 30})
    assert late.status_code == 400


async def test_adjust_route(client, db):
    owner, gym, emp = await staff(db)
    st = await statement(db, emp)
    await login_as(client, owner)
    r = await post_form(client, f"/owner/payroll/{st.id}/adjust", {"unpaid_leave_days": "2"})
    assert r.status_code == 303
    await db.refresh(st)
    days = (PREV - PREV.replace(day=1)).days + 1
    assert st.unpaid_leave_days == 2 and st.days_worked == days - 2
    assert st.gross_pay == (D("30000") * (days - 2) / days).quantize(D("0.01"))
    for bad, needle in (("abc", "whole number"), ("-1", "whole number"), (str(days + 1), "exceeds")):
        r = await post_form(client, f"/owner/payroll/{st.id}/adjust", {"unpaid_leave_days": bad})
        assert r.status_code == 400 and needle in r.text, bad


async def test_explain_route_without_and_with_ai(client, db, ai_down, fake_ai):
    owner, gym, emp = await staff(db)
    st = await statement(db, emp)
    await login_as(client, owner)
    from app.ai.client import FakeProvider, ai

    ai.use_providers([FakeProvider("gateway", fail=True)])
    for _ in range(3):  # trip the circuit so the status reports offline
        try:
            await ai.complete(feature="t", system="s", prompt="p")
        except Exception:
            pass
    r = await post_form(client, f"/owner/payroll/{st.id}/explain")
    assert r.status_code == 303
    assert await db.scalar(select(func.count(Job.id)).where(Job.kind == "payroll_explain")) == 0
    fake_ai(responses=[json.dumps({"summary": "You pay ₹28,200.00 after EPF.", "steps": ["Gross ₹30,000.00."],
                                   "country_notes": [], "warnings": []})])
    r = await post_form(client, f"/owner/payroll/{st.id}/explain")
    assert r.status_code == 303 and r.headers["location"].endswith("#explanation")
    assert (await client.get(f"/owner/payroll/{st.id}/status")).json()["explaining"] is True
    await run_jobs()
    page = await client.get(f"/owner/payroll/{st.id}")
    assert "Explained by AI" in page.text and "You pay ₹28,200.00 after EPF." in page.text


# ============================================================================================ settlement + termination
async def test_settlement_and_termination_flow(client, db):
    owner, gym, emp = await staff(db, created=datetime(2026, 1, 1, tzinfo=timezone.utc))
    branch = await make_branch(db, gym)
    await make_shift(db, emp, branch=branch)
    st = await statement(db, emp)
    await login_as(client, owner)
    page = await client.get(f"/owner/staff/{emp.id}/settlement?termination_date={TODAY.isoformat()}&reason=retirement")
    assert page.status_code == 200 and "Preview" in page.text and "15/26" in page.text
    # an employment always ends with a reason (trust & safety): the old reason-less button hands over to /remove
    r = await post_form(client, f"/owner/staff/{emp.id}/terminate", {"confirm": "1"})
    assert r.status_code == 303 and r.headers["location"] == f"/owner/staff/{emp.id}/remove"
    remove = {"reason": "resigned", "note": "", "confirm": "1"}
    r = await post_form(client, f"/owner/staff/{emp.id}/remove", remove)
    assert r.status_code == 400 and "settled yet" in r.text  # a BENIGN reason waits for payroll
    bad = await post_form(client, f"/owner/staff/{emp.id}/settlement", {"termination_date": "", "reason": "resignation"})
    assert bad.status_code == 400 and "last working day" in bad.text
    bad = await post_form(client, f"/owner/staff/{emp.id}/settlement",
                          {"termination_date": TODAY.isoformat(), "reason": "boredom"})
    assert bad.status_code == 400 and "reason for leaving" in bad.text
    r = await post_form(client, f"/owner/staff/{emp.id}/settlement",
                        {"termination_date": TODAY.isoformat(), "reason": "retirement", "unpaid_leave_days": "0",
                         "note": "Thank you for 7 great years"})
    assert r.status_code == 303
    fs = await pr.final_settlement_of(db, emp.id)
    assert r.headers["location"] == f"/owner/payroll/{fs.id}" and fs.gratuity > 0
    dup = await post_form(client, f"/owner/staff/{emp.id}/settlement",
                          {"termination_date": TODAY.isoformat(), "reason": "retirement"})
    assert dup.status_code == 400
    r = await post_form(client, f"/owner/staff/{emp.id}/remove", remove)
    assert r.status_code == 400
    await db.refresh(emp)
    assert emp.status == EmploymentStatus.ACTIVE
    for s in (st, fs):
        s.status = StatementStatus.PAID
    await db.commit()
    page = await client.get(f"/owner/staff/{emp.id}/settlement")
    assert f'href="/owner/staff/{emp.id}/remove"' in page.text and f'action="/owner/staff/{emp.id}/terminate"' not in page.text
    r = await post_form(client, f"/owner/staff/{emp.id}/remove", {**remove, "confirm": ""})
    assert r.status_code == 400 and "Tick the box" in r.text
    r = await post_form(client, f"/owner/staff/{emp.id}/remove", remove)
    assert r.status_code == 303
    await db.refresh(emp)
    # the last working day is the one the final settlement paid up to; the reason is on the record
    assert emp.status == EmploymentStatus.TERMINATED and emp.termination_date == TODAY
    assert emp.removal_reason == "resigned" and emp.removal_category == "benign"
    assert await db.scalar(select(func.count(TrainerShift.id))) == 0
    page = await client.get(f"/owner/staff/{emp.id}/settlement")
    assert "Employment ended" in page.text
    again = await post_form(client, f"/owner/staff/{emp.id}/terminate", {"confirm": "1"})
    assert again.status_code == 303  # already terminated: friendly redirect
    again = await post_form(client, f"/owner/staff/{emp.id}/remove", remove)
    assert again.status_code == 303  # a double submit finds the employment already ended


# ============================================================================================ transactions + CSV
async def test_transactions_filters_search_and_pagination(client, db):
    owner, gym, emp = await staff(db)
    gym2 = await make_gym(db, owner, name="Second Gym")
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=1)
    for i in range(30):
        db.add(tx(gym, amount=str(100 + i), description=f"Plan payment {i}", reference=f"REF{i:04d}",
                  completed_at=base - timedelta(hours=i)))
    db.add(tx(gym2, direction=TxDirection.OUTGOING, category=TxCategory.SALARY, amount="5000",
              description="Salary - Ravi", reference="UTRSAL123", completed_at=base - timedelta(days=40)))
    db.add(tx(gym, status=TxStatus.FAILED, description="Declined card", amount="999"))
    await db.commit()
    await login_as(client, owner)
    r = await client.get("/owner/finance/transactions")
    assert r.status_code == 200 and "Showing 1-25 of 32" in r.text and "Page 1 of 2" in r.text
    assert "Showing 26-32 of 32" in (await client.get("/owner/finance/transactions?page=2")).text
    assert "Salary - Ravi" in (await client.get("/owner/finance/transactions?q=utrsal")).text
    r = await client.get("/owner/finance/transactions?q=REF0007")
    assert "Showing 1-1 of 1" in r.text and "Plan payment 7" in r.text
    assert "Showing 1-1 of 1" in (await client.get(f"/owner/finance/transactions?direction=outgoing&gym={gym2.id}")).text
    r = await client.get("/owner/finance/transactions?status=failed")
    assert "Declined card" in r.text and "Showing 1-1 of 1" in r.text
    since = (base - timedelta(days=2)).date().isoformat()
    assert "of 30" in (await client.get(f"/owner/finance/transactions?from={since}&category=subscription&status=success")).text
    r = await client.get("/owner/finance/transactions?from=2026-99-99")
    assert r.status_code == 200 and "valid start date" in r.text
    r = await client.get("/owner/finance/transactions?q=%25_%25")  # LIKE wildcards are escaped, not interpreted
    assert "Nothing matches these filters" in r.text
    r = await client.get("/owner/finance/transactions?direction=sideways&category=nope")
    assert r.status_code == 200 and "of 32" in r.text  # unknown enum values are ignored


async def test_csv_export_escapes_formulas_and_streams(client, db):
    owner, gym, emp = await staff(db)
    for desc in ("=HYPERLINK(\"http://evil\")", "+SUM(A1)", "-2+3", "@cmd", "Normal row"):
        db.add(tx(gym, description=desc, reference="=1+1" if desc == "Normal row" else None))
    await db.commit()
    await login_as(client, owner)
    r = await client.get("/owner/finance/transactions.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["cache-control"] == "no-store" and "attachment;" in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.text.lstrip("﻿"))))
    assert rows[0][:3] == ["id", "booked_at", "gym"] and len(rows) == 6
    assert {row[8] for row in rows[1:]} == {"'=HYPERLINK(\"http://evil\")", "'+SUM(A1)", "'-2+3", "'@cmd", "Normal row"}
    assert any(row[11] == "'=1+1" for row in rows[1:])
    only_out = await client.get("/owner/finance/transactions.csv?direction=outgoing")
    assert len(list(csv.reader(io.StringIO(only_out.text.lstrip("﻿"))))) == 1


async def test_finance_overview_numbers(client, db):
    owner, gym, emp = await staff(db)
    now = datetime.now(timezone.utc)
    db.add(tx(gym, amount="1000", completed_at=now))
    db.add(tx(gym, direction=TxDirection.OUTGOING, category=TxCategory.SALARY, amount="400", completed_at=now))
    db.add(tx(gym, amount="700", status=TxStatus.PENDING, completed_at=now))  # not completed -> not counted
    db.add(tx(gym, amount="50", currency="USD", completed_at=now))
    await db.commit()
    await statement(db, emp, due=TODAY - timedelta(days=1))
    await login_as(client, owner)
    r = await client.get("/owner/finance")
    assert r.status_code == 200
    assert "₹1,000.00" in r.text and "₹400.00" in r.text and "₹600.00" in r.text
    assert "1 overdue" in r.text and "currency=USD" in r.text  # second currency gets its own chart
    usd = await client.get("/owner/finance?currency=USD")
    assert "$50.00" in usd.text and "Completed payments in USD" in usd.text
    empty_owner = await make_owner(db)
    await login_as(client, empty_owner)
    r = await client.get("/owner/finance")
    assert r.status_code == 200 and "No gyms yet" in r.text


# ============================================================================================ N+1 guards
async def test_transactions_list_query_count_is_constant(client, db):
    owner, gym, emp = await staff(db)
    gym2 = await make_gym(db, owner)
    await login_as(client, owner)

    async def count_for(n):
        for i in range(n):
            db.add(tx(gym if i % 2 else gym2, description=f"Row {i}"))
        await db.commit()
        await client.get("/owner/finance/transactions")  # warm-up (session, CSRF cookie)
        with QueryCounter() as qc:
            r = await client.get("/owner/finance/transactions")
        assert r.status_code == 200
        return qc.count

    small = await count_for(3)
    big = await count_for(21)
    assert small == big and small <= 12, (small, big)


async def test_payroll_and_overview_query_counts_are_constant(client, db):
    owner, gym, emp = await staff(db)
    await login_as(client, owner)
    counts = []
    for n in (2, 12):
        for i in range(n):
            e = await make_employee(db, gym)
            await statement(db, e)
            db.add(tx(gym, description=f"Income {i}"))
        await db.commit()
        await client.get("/owner/payroll")
        with QueryCounter() as qc:
            assert (await client.get("/owner/payroll")).status_code == 200
        with QueryCounter() as qc2:
            assert (await client.get("/owner/finance")).status_code == 200
        counts.append((qc.count, qc2.count))
    assert counts[0] == counts[1], counts


# ============================================================================================ TEST MODE end to end
async def test_test_mode_receipt_round_trip_with_real_tesseract(client, db, monkeypatch):
    import asyncio

    import pytest

    from app.config import get_settings

    cmd = get_settings().resolved_tesseract_cmd()
    if not cmd:
        pytest.skip("Tesseract is not installed on this machine")
    import pytesseract
    from PIL import Image

    from app.services import ocr
    from app.services.ocr import MultiOcrResult, OcrEngineResult

    pytesseract.pytesseract.tesseract_cmd = cmd

    async def real_tesseract(data, content_type, **kwargs):
        text = await asyncio.to_thread(lambda: pytesseract.image_to_string(Image.open(io.BytesIO(data))))
        return MultiOcrResult([OcrEngineResult("tesseract", True, text)])

    monkeypatch.setattr(ocr, "ocr_document", real_tesseract)
    owner, gym, emp = await staff(db)
    st = await statement(db, emp)
    await login_as(client, owner)
    assert (await client.get(f"/owner/payroll/{st.id}/test-receipt.png")).status_code == 404  # nothing simulated yet
    assert (await post_form(client, f"/owner/payroll/{st.id}/pay")).status_code == 303
    page = await client.get(f"/owner/payroll/{st.id}")
    assert "Simulated receipt (PNG)" in page.text
    receipt = await client.get(f"/owner/payroll/{st.id}/test-receipt.png")
    assert receipt.status_code == 200 and receipt.headers["content-type"] == "image/png"
    assert receipt.headers["cache-control"] == "no-store"
    await db.refresh(st)
    payment = await db.get(Transaction, st.transaction_id)
    r = await post_form(client, f"/owner/payroll/{st.id}/proof",
                        {"reference": payment.provider_reference, "amount": f"{st.net_pay:.2f}", "paid_on": TODAY.isoformat()},
                        files={"proof": ("receipt.png", receipt.content, "image/png")})
    assert r.status_code == 303
    await run_jobs()
    await db.refresh(st)
    await db.refresh(payment)
    assert st.status == StatementStatus.PAID, payment.proof_check
    assert payment.proof_verified is True and payment.proof_check["engines"] == ["tesseract"]


# ============================================================================================ payroll-run (31-finance.js)
PAYROLL_RUN_HARNESS = r"""
var window = globalThis;
var reloads = 0, failures = 0, passed = 0;
window.location = { reload: function () { reloads += 1; } };
var timers = [];
window.setTimeout = function (fn, ms) { timers.push({ fn: fn, ms: ms }); return timers.length; };
var setTimeout = window.setTimeout;
var handlers = {}, registered = {}, apiCalls = [], apiAnswers = [];
window.IP = {
  register: function (name, fn) { registered[name] = fn; },
  on: function (type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
  api: function (url) {
    apiCalls.push(url);
    var a = apiAnswers.shift();
    return a && a.error ? Promise.reject(a.error) : Promise.resolve(a);
  },
  h: function () { return {}; },
};
function El(attrs) { this.attrs = attrs || {}; this.value = 0; this.textContent = ""; }
El.prototype.getAttribute = function (n) { return Object.prototype.hasOwnProperty.call(this.attrs, n) ? this.attrs[n] : null; };
function settle() { for (var i = 0; i < 25; i++) drainMicrotasks(); }
function check(ok, what) { if (ok) passed += 1; else { failures += 1; print("FAIL " + what); } }
function emit(evt) { (handlers["task.update"] || []).forEach(function (fn) { fn(evt); }); }

load(arguments[0]);
var bar = new El(), text = new El();
var root = new El({ "data-task": "42", "data-url": "/owner/payroll/runs/42" });
root.querySelector = function (sel) { return sel === "progress" ? bar : (sel === "[data-progress-text]" ? text : null); };
check(typeof registered["payroll-run"] === "function", "registered");
registered["payroll-run"](root);

emit({ type: "task.update", task_id: 7, status: "running", progress: 90, message: "9 of 10 gyms done" });
check(bar.value === 0 && text.textContent === "", "another task's progress is ignored");
emit({ type: "task.update", task_id: 42, status: "running", progress: 33, message: "1 of 3 gyms done" });
check(bar.value === 33, "progress bar follows the socket");
check(text.textContent.indexOf("1 of 3 gyms done") === 0, "progress text follows the socket");
emit({ type: "task.update", task_id: 42, status: "running", progress: 250, message: "" });
check(bar.value === 100 && text.textContent.indexOf("1 of 3") === 0, "progress clamped, empty message kept");

// the poll covers a quiet socket: running -> updates, then done -> one reload
check(timers.length === 1 && timers[0].ms === 4000, "first poll scheduled");
apiAnswers.push({ status: "running", progress: 67, message: "2 of 3 gyms done" });
timers[0].fn(); settle();
check(apiCalls[0] === "/owner/payroll/runs/42" && bar.value === 67, "poll updates the bar");
check(timers.length === 2, "next poll scheduled");
apiAnswers.push({ status: "done", progress: 100, message: "3 of 3 gyms done" });
timers[1].fn(); settle();
check(reloads === 1, "a finished run reloads the page once");
emit({ type: "task.update", task_id: 42, status: "done" });
check(reloads === 1, "the socket's done after the poll's does not reload twice");
check(timers.length === 2, "no poll after the end");

// a fresh card: the socket's done reloads; a 404 from the poll stops polling quietly
reloads = 0; timers = []; apiCalls = []; handlers = {};
var root2 = new El({ "data-task": "43", "data-url": "/owner/payroll/runs/43" });
root2.querySelector = root.querySelector;
registered["payroll-run"](root2);
emit({ type: "task.update", task_id: 43, status: "failed" });
check(reloads === 1, "a failed run reloads too (the notification explains)");
reloads = 0; timers = []; handlers = {};
var root3 = new El({ "data-task": "44", "data-url": "/owner/payroll/runs/44" });
root3.querySelector = root.querySelector;
registered["payroll-run"](root3);
apiAnswers.push({ error: { status: 404 } });
timers[0].fn(); settle();
check(reloads === 0 && timers.length === 1, "a 404 stops the poll without reloading");
print("SUMMARY passed=" + passed + " failed=" + failures);
"""


def test_the_payroll_run_card_follows_the_job_in_javascriptcore(tmp_path):
    """31-finance.js ``payroll-run``: the progress card of "run payroll" for all gyms follows task.update over the
    socket and a slow poll of /owner/payroll/runs/{id}, and reloads the page once when the run ends."""
    import shutil
    import subprocess
    from pathlib import Path

    import pytest

    jsc = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"
    jsc = jsc if Path(jsc).exists() else shutil.which("jsc")
    if not jsc:
        pytest.skip("JavaScriptCore (jsc) is not available on this machine")
    source = Path(__file__).resolve().parent.parent / "app" / "static" / "src" / "js" / "31-finance.js"
    harness = tmp_path / "payroll_run_harness.js"
    harness.write_text(PAYROLL_RUN_HARNESS, encoding="utf-8")
    proc = subprocess.run([jsc, str(harness), "--", str(source)], capture_output=True, text=True, timeout=60)
    out = proc.stdout + proc.stderr
    assert "SUMMARY passed=13 failed=0" in out, out
