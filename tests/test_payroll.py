"""WP-C2 payroll engine + services: proration by days, country rules, generation, dues, settlement,
proof verification, reminders, AI explanation and termination."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import AuditLog, Notification, SalaryStatement, TrainerClient, TrainerShift, Transaction
from app.models.enums import (
    EmploymentStatus,
    EmploymentType,
    ShiftMode,
    StatementStatus,
    TxCategory,
    TxStatus,
)
from app.services import payroll as pr
from app.services.payroll_rules import rules_for, service_length
from tests.factories import make_branch, make_customer, make_employee, make_gym, make_owner, make_shift, png_bytes

D = Decimal
TODAY = date.today()


# ============================================================================================ helpers
def compute(**kw):
    base = dict(monthly_salary=D("30000"), currency="INR", country_code="XX", hire_date=date(2020, 1, 1),
                period_start=date(2026, 11, 1), period_end=date(2026, 11, 30))
    base.update(kw)
    return pr.compute_statement(**base)


async def setup_staff(db, *, salary="30000", hire=date(2019, 1, 15), country="IN", currency="INR", created=None, **kw):
    owner = await make_owner(db)
    gym = await make_gym(db, owner, country="IN" if country == "IN" else country, currency=currency)
    emp = await make_employee(db, gym, salary=salary, hire_date=hire, country=country, currency=currency, **kw)
    if created is not None:
        emp.created_at = created
        await db.commit()
    return owner, gym, emp


async def add_statement(db, emp, *, start, end, status=StatementStatus.DUE, due=None, final=False, reason=None,
                        termination=None):
    draft = pr.compute_statement(monthly_salary=emp.monthly_salary, currency=emp.currency, country_code=emp.country_code,
                                 hire_date=emp.hire_date, period_start=start, period_end=end, termination_date=termination,
                                 is_final_settlement=final, termination_reason=reason)
    if final:
        draft.rules["settlement"] = {"termination_date": (termination or end).isoformat(), "reason": reason or "resignation",
                                     "reason_label": "Resignation by the employee"}
    st = SalaryStatement(**draft.model_kwargs(), employee_id=emp.id, gym_id=emp.gym_id, status=status,
                         due_date=due or pr.due_date_for(end))
    db.add(st)
    await db.commit()
    return st


def fake_ocr(monkeypatch, text: str | None = None, *, fail: Exception | None = None):
    from app.services import ocr
    from app.services.ocr import MultiOcrResult, OcrEngineResult

    calls = []

    async def _ocr(data, content_type, **kwargs):
        calls.append(content_type)
        if fail is not None:
            raise fail
        return MultiOcrResult([OcrEngineResult("tesseract", True, text or "", 0.9),
                               OcrEngineResult("apple_vision", True, text or "", 0.95)])

    monkeypatch.setattr(ocr, "ocr_document", _ocr)
    return calls


# ============================================================================================ proration by days
def test_full_month():
    d = compute()
    assert d.gross_pay == D("30000.00") and d.days_worked == 30 and d.days_in_period == 30
    assert d.net_pay == D("30000.00") and d.rules["proration"][0]["paid_days"] == 30


def test_mid_month_hire_and_termination():
    hire = compute(hire_date=date(2026, 11, 16))
    assert hire.period_start == date(2026, 11, 16) and hire.days_worked == 15 and hire.gross_pay == D("15000.00")
    term = compute(termination_date=date(2026, 11, 10))
    assert term.period_end == date(2026, 11, 10) and term.days_worked == 10 and term.gross_pay == D("10000.00")
    both = compute(hire_date=date(2026, 11, 6), termination_date=date(2026, 11, 20))
    assert both.days_worked == 15 and both.gross_pay == D("15000.00")


def test_february_leap_and_common_year():
    leap = compute(monthly_salary=D("29000"), hire_date=date(2024, 2, 15), period_start=date(2024, 2, 1),
                   period_end=date(2024, 2, 29))
    assert leap.rules["proration"][0]["days_in_month"] == 29 and leap.days_worked == 15
    assert leap.gross_pay == D("15000.00")
    full_leap = compute(period_start=date(2024, 2, 1), period_end=date(2024, 2, 29))
    common = compute(period_start=date(2025, 2, 1), period_end=date(2025, 2, 28))
    assert full_leap.gross_pay == common.gross_pay == D("30000.00")
    assert common.rules["proration"][0]["days_in_month"] == 28


def test_unpaid_leave_and_validation():
    d = compute(unpaid_leave_days=2)
    assert d.days_worked == 28 and d.unpaid_leave_days == 2 and d.gross_pay == D("28000.00")
    with pytest.raises(pr.PayrollError) as exc:
        compute(unpaid_leave_days=31)
    assert exc.value.field == "unpaid_leave_days"
    with pytest.raises(pr.PayrollError):
        compute(unpaid_leave_days=-1)
    with pytest.raises(pr.PayrollError):
        compute(monthly_salary=D("-1"))
    with pytest.raises(pr.PayrollError):
        compute(period_start=date(2026, 11, 30), period_end=date(2026, 11, 1))


def test_multi_month_period_prorates_each_month():
    d = compute(monthly_salary=D("31000"), hire_date=date(2024, 12, 1), period_start=date(2025, 1, 20),
                period_end=date(2025, 3, 10))
    assert [(p["label"], p["paid_days"], p["days_in_month"], p["amount"]) for p in d.rules["proration"]] == [
        ("Jan 2025", 12, 31, "12000.00"), ("Feb 2025", 28, 28, "31000.00"), ("Mar 2025", 10, 31, "10000.00")]
    assert d.gross_pay == D("53000.00") and d.days_worked == 50
    leave = compute(monthly_salary=D("31000"), hire_date=date(2024, 12, 1), period_start=date(2025, 1, 20),
                    period_end=date(2025, 3, 10), unpaid_leave_days=15)
    assert [p["paid_days"] for p in leave.rules["proration"]] == [0, 25, 10]  # earliest month first
    assert leave.gross_pay == D("37678.57")


def test_round_half_up_to_cents():
    d = compute(monthly_salary=D("1.01"), period_start=date(2026, 11, 1), period_end=date(2026, 11, 15))
    assert d.gross_pay == D("0.51")  # 1.01 x 15/30 = 0.505 -> half up


def test_no_employment_in_period_yields_zero():
    d = compute(hire_date=date(2027, 1, 1))
    assert d.gross_pay == 0 and d.days_worked == 0 and d.rules["proration"] == []


# ============================================================================================ India
def test_india_gratuity_partial_year_rounds_up_and_eligibility():
    r = rules_for("IN")
    g = r.gratuity(monthly_salary=D("40000"), hire_date=date(2020, 1, 15), end_date=date(2024, 8, 20), currency="INR")
    assert service_length(date(2020, 1, 15), date(2024, 8, 20)).label == "4 years, 7 months, 6 days"
    assert g.eligible and g.years_counted == 5 and g.amount == D("57692.31")  # 15/26 x 20,000 x 5
    short = r.gratuity(monthly_salary=D("40000"), hire_date=date(2020, 1, 15), end_date=date(2024, 6, 10), currency="INR")
    assert not short.eligible and short.amount == 0
    exactly_six = r.gratuity(monthly_salary=D("40000"), hire_date=date(2020, 1, 1), end_date=date(2024, 6, 30), currency="INR")
    assert not exactly_six.eligible  # 4 y 6 m is NOT more than six months


def test_india_fixed_term_one_year_and_cap_and_death():
    r = rules_for("IN")
    ft = r.gratuity(monthly_salary=D("30000"), hire_date=date(2025, 1, 1), end_date=date(2025, 12, 31),
                    employment_type="fixed_term", currency="INR")
    assert ft.eligible and ft.years_counted == 1 and ft.amount == D("8653.85")
    ft_short = r.gratuity(monthly_salary=D("30000"), hire_date=date(2025, 1, 1), end_date=date(2025, 11, 30),
                          employment_type="fixed_term", currency="INR")
    assert not ft_short.eligible
    capped = r.gratuity(monthly_salary=D("1000000"), hire_date=date(1990, 1, 1), end_date=date(2019, 12, 31), currency="INR")
    assert capped.amount == D("2000000.00") and any("ceiling" in s for s in capped.steps)
    death = r.gratuity(monthly_salary=D("30000"), hire_date=date(2024, 1, 1), end_date=date(2025, 12, 31),
                       reason="death_or_disability", currency="INR")
    assert death.eligible and death.amount == D("17307.69")
    intern = r.gratuity(monthly_salary=D("30000"), hire_date=date(2015, 1, 1), end_date=date(2025, 1, 1),
                        employment_type="intern", currency="INR")
    assert not intern.eligible


def test_india_epf_esi_and_tds():
    r = rules_for("IN")
    hi = r.taxes(gross=D("50000"), monthly_salary=D("50000"), fraction=D(1), currency="INR")
    assert {l.code: l.amount for l in hi.employee}["epf_employee"] == D("1800.00")  # basic capped at 15,000
    assert "esi_employee" not in {l.code for l in hi.employee}
    lo = r.taxes(gross=D("20000"), monthly_salary=D("20000"), fraction=D(1), currency="INR")
    emp = {l.code: l.amount for l in lo.employee}
    er = {l.code: l.amount for l in lo.employer}
    assert emp == {"epf_employee": D("1200.00"), "esi_employee": D("150.00")}
    assert er["esi_employer"] == D("650.00")
    rebate = r.taxes(gross=D("100000"), monthly_salary=D("100000"), fraction=D(1), currency="INR")
    assert rebate.withholding.amount == 0 and "87A" in rebate.withholding.explanation
    taxed = r.taxes(gross=D("150000"), monthly_salary=D("150000"), fraction=D(1), currency="INR")
    assert taxed.withholding.amount == D("12566.67")  # (20k+40k+60k+25k) x 1.04 / 12
    relief_annual, steps = r.annual_tax(D("1285000"))
    assert relief_annual == D("10400.00") and any("Marginal relief" in s for s in steps)
    half = r.taxes(gross=D("75000"), monthly_salary=D("150000"), fraction=D("0.5"), currency="INR")
    assert half.withholding.amount == D("6283.33")


def test_currency_mismatch_skips_thresholds():
    d = compute(country_code="IN", currency="USD")
    assert d.employee_deductions == {} and d.tax_withholding == 0
    assert any("NOT estimated" in w for w in d.rules["warnings"])


# ============================================================================================ UAE / GCC
def test_uae_tiers_cap_and_minimum_service():
    r = rules_for("AE")
    three = r.gratuity(monthly_salary=D("10000"), hire_date=date(2021, 1, 1), end_date=date(2023, 12, 31), currency="AED")
    assert three.eligible and three.amount == D("12427.40")  # 3 x 21 x (6000 x 12 / 365)
    seven = r.gratuity(monthly_salary=D("10000"), hire_date=date(2016, 1, 1), end_date=date(2022, 12, 31), currency="AED")
    assert seven.amount == D("32547.95")  # (5 x 21 + 2 x 30) days of basic
    capped = r.gratuity(monthly_salary=D("10000"), hire_date=date(1990, 1, 1), end_date=date(2019, 12, 31), currency="AED")
    assert capped.amount == D("144000.00")  # 2 years' basic
    short = r.gratuity(monthly_salary=D("10000"), hire_date=date(2025, 3, 1), end_date=date(2025, 12, 31), currency="AED")
    assert not short.eligible and short.amount == 0
    assert r.taxes(gross=D("10000"), monthly_salary=D("10000"), fraction=D(1), currency="AED").withholding is None


def test_gcc_resignation_tiers():
    sa = rules_for("SA")
    assert sa.gratuity(monthly_salary=D("9000"), hire_date=date(2024, 6, 1), end_date=date(2025, 12, 31),
                       reason="resignation", currency="SAR").amount == 0  # < 2 years
    third = sa.gratuity(monthly_salary=D("9000"), hire_date=date(2021, 1, 1), end_date=date(2023, 12, 31),
                        reason="resignation", currency="SAR")
    assert third.amount == D("4500.00")  # 3 x half month = 13,500 x 1/3
    full = sa.gratuity(monthly_salary=D("9000"), hire_date=date(2021, 1, 1), end_date=date(2023, 12, 31),
                       reason="dismissal", currency="SAR")
    assert full.amount == D("13500.00")
    kw = rules_for("KW")
    assert kw.gratuity(monthly_salary=D("1000"), hire_date=date(2023, 6, 1), end_date=date(2025, 12, 31),
                       reason="resignation", currency="KWD").amount == 0  # < 3 years
    assert kw.gratuity(monthly_salary=D("1000"), hire_date=date(2023, 1, 1), end_date=date(2025, 12, 31),
                       reason="resignation", currency="KWD").amount == D("750.00")  # 3 x half month x 50%
    for code in ("QA", "OM", "BH"):
        g = rules_for(code).gratuity(monthly_salary=D("5000"), hire_date=date(2020, 1, 1), end_date=date(2024, 12, 31),
                                     currency=rules_for(code).currency)
        assert g.eligible and g.amount > 0 and g.steps


# ============================================================================================ US / GB / others
def test_us_withholding_and_fica():
    r = rules_for("US")
    t = r.taxes(gross=D("5000"), monthly_salary=D("5000"), fraction=D(1), currency="USD")
    assert t.withholding.amount == D("430.13")
    emp = {l.code: l.amount for l in t.employee}
    assert emp == {"social_security": D("310.00"), "medicare": D("72.50")}
    rich = r.taxes(gross=D("20000"), monthly_salary=D("20000"), fraction=D(1), currency="USD")
    rich_emp = {l.code: l.amount for l in rich.employee}
    assert rich_emp["social_security"] == D("909.85")  # wage base 176,100 spread over the year
    assert rich_emp["additional_medicare"] == D("30.00")
    assert not r.gratuity(monthly_salary=D("5000"), hire_date=date(2010, 1, 1), end_date=date(2025, 1, 1)).eligible
    assert any("State" in w for w in r.warnings)


def test_gb_paye_ni_and_allowance_taper():
    r = rules_for("GB")
    t = r.taxes(gross=D("5000"), monthly_salary=D("5000"), fraction=D(1), currency="GBP")
    assert t.withholding.amount == D("952.67")
    emp = {l.code: l.amount for l in t.employee}
    er = {l.code: l.amount for l in t.employer}
    assert emp["ni_employee"] == D("267.50") and emp["pension_employee"] == D("183.45")
    assert er["ni_employer"] == D("687.45")
    taper = r.taxes(gross=D("12500"), monthly_salary=D("12500"), fraction=D(1), currency="GBP")
    assert taper.withholding.amount == D("4475.25")  # personal allowance fully withdrawn at 150k


@pytest.mark.parametrize("code", ["IN", "AE", "SA", "QA", "KW", "OM", "BH", "US", "GB", "CA", "AU", "SG", "DE", "FR",
                                  "NZ", "ZA"])
def test_every_country_produces_explained_non_negative_estimates(code):
    r = rules_for(code)
    assert r.code == code
    cur = r.currency
    d = compute(country_code=code, currency=cur, monthly_salary=D("6000"), hire_date=date(2015, 3, 1),
                is_final_settlement=True, termination_reason="redundancy", termination_date=date(2026, 11, 30))
    assert d.net_pay >= 0 and d.gross_pay == D("6000.00")
    for v in list(d.employee_deductions.values()) + list(d.employer_contributions.values()):
        assert D(v["amount"]) >= 0 and v["explanation"]
    assert d.rules["gratuity"]["explanation"] and d.rules["rules_found"] is True
    expl = pr.deterministic_explanation(d)
    assert expl.summary and expl.steps and expl.warnings


def test_default_country_computes_nothing_but_warns():
    d = compute(country_code="XX", is_final_settlement=True, termination_date=date(2026, 11, 30))
    assert d.gratuity == 0 and d.tax_withholding == 0 and d.employee_deductions == {}
    assert d.rules["rules_found"] is False and any("accountant" in w for w in d.rules["warnings"])


def test_reason_specific_severance():
    fr = rules_for("FR")
    assert fr.gratuity(monthly_salary=D("3000"), hire_date=date(2020, 1, 1), end_date=date(2024, 12, 31),
                       reason="misconduct", currency="EUR").amount == 0
    assert fr.gratuity(monthly_salary=D("3000"), hire_date=date(2020, 1, 1), end_date=date(2024, 12, 31),
                       reason="dismissal", currency="EUR").amount == D("3750.00")  # 5 x 1/4 month
    de = rules_for("DE")
    assert de.gratuity(monthly_salary=D("4000"), hire_date=date(2020, 1, 1), end_date=date(2024, 7, 15),
                       reason="redundancy", currency="EUR").amount == D("10000.00")  # 4y 6.5m -> 5 x 0.5 month
    assert de.gratuity(monthly_salary=D("4000"), hire_date=date(2020, 1, 1), end_date=date(2024, 7, 15),
                       reason="resignation", currency="EUR").amount == 0
    za = rules_for("ZA")
    assert za.gratuity(monthly_salary=D("26000"), hire_date=date(2020, 1, 1), end_date=date(2024, 12, 31),
                       reason="redundancy", currency="ZAR").amount == D("30000.00")  # 5 weeks x 6000
    mis = rules_for("IN").gratuity(monthly_salary=D("30000"), hire_date=date(2015, 1, 1), end_date=date(2025, 1, 1),
                                   reason="misconduct", currency="INR")
    assert mis.amount > 0 and any("forfeited" in w for w in mis.warnings)


def test_final_settlement_net_includes_gratuity():
    d = compute(country_code="IN", hire_date=date(2015, 1, 1), period_start=date(2026, 11, 1),
                period_end=date(2026, 11, 10), termination_date=date(2026, 11, 10), is_final_settlement=True,
                termination_reason="resignation")
    ded = sum(D(v["amount"]) for v in d.employee_deductions.values())
    assert d.gross_pay == D("10000.00") and d.gratuity == D("103846.15")  # 15/26 x 15,000 x 12 years
    assert d.net_pay == d.gross_pay - ded - d.tax_withholding + d.gratuity


# ============================================================================================ parsing helpers
def test_amount_and_date_parsing():
    assert D("28200.00") in pr.amounts_in("Paid INR28,200.00 via IMPS")
    assert D("123456.50") in pr.amounts_in("Rs.1,23,456.50/-")
    assert D("28200.50") in pr.amounts_in("Betrag 28.200,50 EUR")
    found = pr.dates_in("Txn on 03/11/2026 at 10:15, value date 2026-11-04, 5th Nov 2026, Nov 6, 2026")
    assert {date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6)} <= found
    check = pr.check_proof_text("UTR 4123 4567 8901 amount 28,200.00 on " + TODAY.strftime("%d-%m-%Y"),
                                expected_amount=D("28200"), amount_paid=D("28200"), reference="412345678901",
                                currency="INR", earliest=TODAY - timedelta(days=40), latest=TODAY + timedelta(days=1))
    assert check.passed, check.checks
    bad = pr.check_proof_text("amount 27,000.00 on 01-01-2020", expected_amount=D("28200"), amount_paid=D("28200"),
                              reference="412345678901", currency="INR", earliest=TODAY - timedelta(days=40),
                              latest=TODAY + timedelta(days=1))
    assert not bad.passed and {c["check"] for c in bad.checks if not c["passed"]} == {"amount", "reference", "date"}
    assert pr.uncovered_segments(date(2026, 1, 1), date(2026, 1, 31), [(date(2026, 1, 10), date(2026, 1, 20))]) == [
        (date(2026, 1, 1), date(2026, 1, 9)), (date(2026, 1, 21), date(2026, 1, 31))]


# ============================================================================================ generation
async def test_generate_is_idempotent_and_due_on_the_7th(db):
    owner, gym, emp = await setup_staff(db)
    res = await pr.generate_statements(db, gym, 2026, 10)
    await db.commit()
    assert len(res.created_ids) == 1 and res.label == "October 2026"
    again = await pr.generate_statements(db, gym, 2026, 10)
    await db.commit()
    assert again.created_ids == [] and again.skipped == [(emp.display_name, "already covered")]
    st = (await db.execute(select(SalaryStatement))).scalar_one()
    assert st.status == StatementStatus.DUE and st.due_date == date(2026, 11, 7)
    assert st.gross_pay == D("30000.00") and st.net_pay == D("28200.00") and st.country_code == "IN"
    assert st.rules["estimate"] is True and st.rules["explanation"]["summary"]
    assert await db.scalar(select(func.count(SalaryStatement.id))) == 1


async def test_generate_selects_the_right_people(db):
    owner, gym, emp = await setup_staff(db)
    late = await make_employee(db, gym, hire_date=date(2026, 10, 16))
    left_in_month = await make_employee(db, gym, hire_date=date(2020, 1, 1), status=EmploymentStatus.TERMINATED)
    left_in_month.termination_date = date(2026, 10, 10)
    left_before = await make_employee(db, gym, hire_date=date(2020, 1, 1), status=EmploymentStatus.TERMINATED)
    left_before.termination_date = date(2026, 9, 30)
    invited = await make_employee(db, gym, status=EmploymentStatus.INVITED)
    future = await make_employee(db, gym, hire_date=date(2026, 11, 2))
    await db.commit()
    res = await pr.generate_statements(db, gym, 2026, 10)
    await db.commit()
    rows = {s.employee_id: s for s in (await db.execute(select(SalaryStatement))).scalars()}
    assert set(rows) == {emp.id, late.id, left_in_month.id}
    assert rows[late.id].days_worked == 16 and rows[late.id].period_start == date(2026, 10, 16)
    assert rows[left_in_month.id].days_worked == 10 and rows[left_in_month.id].period_end == date(2026, 10, 10)
    assert invited.id not in rows and future.id not in rows and left_before.id not in rows
    assert len(res.created_ids) == 3


async def test_generation_never_pays_days_covered_by_a_final_settlement(db):
    owner, gym, emp = await setup_staff(db)
    await add_statement(db, emp, start=date(2026, 10, 1), end=date(2026, 10, 15), final=True,
                        termination=date(2026, 10, 15), reason="resignation", status=StatementStatus.PAID)
    res = await pr.generate_statements(db, gym, 2026, 10)
    await db.commit()
    assert res.created_ids == []  # employment ends with the settlement
    nov = await pr.generate_statements(db, gym, 2026, 11)
    assert nov.created_ids == []


async def test_monthly_periodic_runs_on_day_one_only(db):
    owner, gym, emp = await setup_staff(db)
    assert await pr.run_monthly_generation(db, today=date(2026, 11, 2)) == 0
    assert await pr.run_monthly_generation(db, today=date(2026, 11, 1)) == 1
    await db.commit()
    assert await pr.run_monthly_generation(db, today=date(2026, 11, 1)) == 0  # idempotent
    await db.commit()
    st = (await db.execute(select(SalaryStatement))).scalar_one()
    assert st.period_start == date(2026, 10, 1) and st.period_end == date(2026, 10, 31)
    note = (await db.execute(select(Notification).where(Notification.user_id == owner.id))).scalar_one()
    assert "October 2026" in note.title


# ============================================================================================ dues / accrual
async def test_outstanding_dues_lists_unpaid_and_accrual_since_last_statement(db):
    owner, gym, emp = await setup_staff(db, created=datetime(2026, 9, 20, tzinfo=timezone.utc))
    oct_st = await add_statement(db, emp, start=date(2026, 10, 1), end=date(2026, 10, 31))
    await add_statement(db, emp, start=date(2026, 9, 1), end=date(2026, 9, 30), status=StatementStatus.PAID)
    dues = await pr.outstanding_dues(db, emp, as_of=date(2026, 11, 10))
    assert isinstance(dues, list) and [s.id for s in dues] == [oct_st.id]
    assert dues.totals() == {"INR": D("28200.00")}
    acc = dues.accrued
    assert acc.period_start == date(2026, 11, 1) and acc.period_end == date(2026, 11, 10)
    assert acc.days == 10 and acc.gross == D("10000.00")


async def test_accrual_starts_when_the_person_was_added_to_ironpulse(db):
    owner, gym, emp = await setup_staff(db, hire=date(2019, 1, 15), created=datetime(2026, 11, 3, tzinfo=timezone.utc))
    acc = await pr.accrued_preview(db, emp, as_of=date(2026, 11, 10))
    assert acc.period_start == date(2026, 11, 1) and acc.days == 10  # salaries before Nov were paid outside the app
    gap = await setup_staff(db, hire=date(2026, 8, 1), created=datetime(2026, 8, 5, tzinfo=timezone.utc))
    await add_statement(db, gap[2], start=date(2026, 10, 1), end=date(2026, 10, 31), status=StatementStatus.PAID)
    acc2 = await pr.accrued_preview(db, gap[2], as_of=date(2026, 11, 10))
    assert acc2.days == 31 + 30 + 10  # unpaid Aug + Sep (gap) + Nov
    invited = await make_employee(db, gym, status=EmploymentStatus.INVITED)
    assert await pr.accrued_preview(db, invited, as_of=date(2026, 11, 10)) is None


# ============================================================================================ reminders
async def test_salary_reminders_are_throttled_daily(db):
    owner, gym, emp = await setup_staff(db)
    overdue = await add_statement(db, emp, start=date(2026, 9, 1), end=date(2026, 9, 30), due=TODAY - timedelta(days=2))
    await add_statement(db, emp, start=date(2026, 10, 1), end=date(2026, 10, 31), due=TODAY + timedelta(days=20))
    await add_statement(db, emp, start=date(2026, 8, 1), end=date(2026, 8, 31), due=TODAY - timedelta(days=40),
                        status=StatementStatus.PAID)
    now = datetime.now(timezone.utc)
    assert await pr.send_salary_reminders(db, now=now) == 1
    await db.commit()
    notes = (await db.execute(select(Notification).where(Notification.user_id == owner.id))).scalars().all()
    assert len(notes) == 1 and "Overdue" in notes[0].title
    assert notes[0].body.startswith(f"Pay ₹28,200.00 to {emp.display_name} - upload the proof of transaction")
    assert notes[0].link == f"/owner/payroll/{overdue.id}"
    assert await pr.send_salary_reminders(db, now=now + timedelta(hours=3)) == 0  # max once a day
    assert await pr.send_salary_reminders(db, now=now + timedelta(hours=25)) == 1


# ============================================================================================ final settlement
async def test_final_settlement_validation_and_creation(db):
    owner, gym, emp = await setup_staff(db, created=datetime(2026, 10, 1, tzinfo=timezone.utc))
    await add_statement(db, emp, start=date(2026, 10, 1), end=date(2026, 10, 31), status=StatementStatus.PAID)
    with pytest.raises(pr.PayrollError) as exc:
        await pr.create_final_settlement(db, emp, date(2026, 10, 20), "resignation", today=date(2026, 11, 10))
    assert exc.value.field == "termination_date" and "31 Oct 2026" in str(exc.value)
    with pytest.raises(pr.PayrollError):
        await pr.create_final_settlement(db, emp, date(2027, 6, 1), "resignation", today=date(2026, 11, 10))
    with pytest.raises(pr.PayrollError):
        await pr.create_final_settlement(db, emp, date(2026, 11, 10), "bored", today=date(2026, 11, 10))
    st = await pr.create_final_settlement(db, emp, date(2026, 11, 10), "resignation", note="Moving city", actor=owner,
                                          today=date(2026, 11, 10))
    await db.commit()
    assert st.is_final_settlement and st.status == StatementStatus.DUE and st.period_start == date(2026, 11, 1)
    assert st.days_worked == 10 and st.gratuity == D("69230.77")  # 7y 9m -> 8 years x 15/26 x 15,000
    assert st.rules["settlement"]["note"] == "Moving city" and st.due_date == date(2026, 11, 10)
    with pytest.raises(pr.PayrollError):
        await pr.create_final_settlement(db, emp, date(2026, 11, 10), "resignation", today=date(2026, 11, 10))
    assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "payroll.final_settlement_created")) == 1


async def test_zero_settlement_is_paid_immediately(db):
    owner, gym, emp = await setup_staff(db, hire=date(2026, 6, 1), country="US", currency="USD",
                                        created=datetime(2026, 6, 1, tzinfo=timezone.utc))
    for m in range(6, 11):
        end = (date(2026, m + 1, 1) - timedelta(days=1))
        await add_statement(db, emp, start=date(2026, m, 1), end=end, status=StatementStatus.PAID)
    st = await pr.create_final_settlement(db, emp, date(2026, 10, 31), "resignation", today=date(2026, 11, 10))
    await db.commit()
    assert st.net_pay == 0 and st.status == StatementStatus.PAID and st.rules.get("settled_without_payment")


# ============================================================================================ payouts + proofs
async def test_test_mode_payout_then_verified_proof_splits_salary_and_gratuity(db, monkeypatch):
    owner, gym, emp = await setup_staff(db, hire=date(2015, 1, 1), created=datetime(2026, 11, 1, tzinfo=timezone.utc))
    st = await pr.create_final_settlement(db, emp, TODAY, "retirement", actor=owner)
    await db.commit()
    assert st.gratuity > 0
    mode, _ = pr.payout_mode(st, emp)
    assert mode == "test"
    tx = await pr.initiate_payout(db, st, emp, gym, owner)
    await db.commit()
    assert st.status == StatementStatus.AWAITING_PROOF and tx.provider == "test" and tx.status == TxStatus.AWAITING_PROOF
    assert tx.provider_reference.startswith("TESTUTR")
    with pytest.raises(pr.PayrollError):
        await pr.initiate_payout(db, st, emp, gym, owner)  # already in progress
    ref = tx.provider_reference
    await pr.record_proof(db, st, emp, owner, upload=png_bytes(text="proof"), reference=ref, amount_paid=st.net_pay,
                          paid_on=TODAY)
    await db.commit()
    assert st.status == StatementStatus.PROOF_REVIEW
    calls = fake_ocr(monkeypatch, f"Transfer successful\nRef: {ref}\nAmount: Rs. {st.net_pay:,.2f}\nDate: {TODAY:%d %b %Y}")
    check = await pr.verify_payment_proof(db, st.id)
    await db.commit()
    assert calls and check.passed, check.checks
    await db.refresh(st)
    assert st.status == StatementStatus.PAID and st.paid_at is not None
    txs = (await db.execute(select(Transaction).where(Transaction.salary_statement_id == st.id)
                            .order_by(Transaction.id))).scalars().all()
    assert [t.category for t in txs] == [TxCategory.SALARY, TxCategory.GRATUITY]
    assert all(t.status == TxStatus.SUCCESS and t.proof_verified for t in txs)
    assert sum(t.amount for t in txs) == st.net_pay and txs[1].amount == st.gratuity
    staff_note = (await db.execute(select(Notification).where(Notification.user_id == emp.user_id))).scalar_one()
    assert staff_note.title == "Salary paid"


async def test_proof_mismatch_then_confirm_with_justification(db, monkeypatch):
    owner, gym, emp = await setup_staff(db)
    st = await add_statement(db, emp, start=date(2026, 10, 1), end=date(2026, 10, 31))
    await pr.record_proof(db, st, emp, owner, upload=png_bytes(), reference="UTR998877665544", amount_paid=st.net_pay,
                          paid_on=TODAY)
    await db.commit()
    fake_ocr(monkeypatch, "Payment of 12,000.00 to someone else on 01/01/2019")
    check = await pr.verify_payment_proof(db, st.id)
    await db.commit()
    assert not check.passed and len(check.reasons) == 3
    await db.refresh(st)
    tx = await db.get(Transaction, st.transaction_id)
    assert st.status == StatementStatus.AWAITING_PROOF and tx.status == TxStatus.AWAITING_PROOF
    assert tx.proof_verified is False and tx.proof_check["reasons"]
    warn = (await db.execute(select(Notification).where(Notification.user_id == owner.id))).scalar_one()
    assert "attention" in warn.title
    with pytest.raises(pr.PayrollError) as exc:
        await pr.confirm_despite_mismatch(db, st, emp, owner, "too short")
    assert exc.value.field == "justification"
    await pr.confirm_despite_mismatch(db, st, emp, owner, "Bank statement shows the transfer on 3 Nov, screenshot cropped")
    await db.commit()
    await db.refresh(tx)
    assert st.status == StatementStatus.PAID and tx.status == TxStatus.SUCCESS and tx.proof_verified is False
    assert tx.proof_check["override"]["by"] == owner.id
    audit_row = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.confirm_mismatch"))).scalar_one()
    assert "cropped" in audit_row.detail["justification"]


async def test_ocr_unavailable_and_reused_reference_fail_verification(db, monkeypatch):
    owner, gym, emp = await setup_staff(db)
    first = await add_statement(db, emp, start=date(2026, 9, 1), end=date(2026, 9, 30))
    await pr.record_proof(db, first, emp, owner, upload=png_bytes(color="red"), reference="UTR123456789",
                          amount_paid=first.net_pay, paid_on=TODAY)
    await db.commit()
    fake_ocr(monkeypatch, fail=NotImplementedError())
    check = await pr.verify_payment_proof(db, first.id)
    await db.commit()
    assert not check.passed and "not available" in check.reasons[0]
    fake_ocr(monkeypatch, f"UTR123456789 paid {first.net_pay:,.2f} on {TODAY:%Y-%m-%d}")
    await pr.record_proof(db, first, emp, owner, upload=png_bytes(color="blue"), reference="UTR123456789",
                          amount_paid=first.net_pay, paid_on=TODAY)
    await db.commit()
    assert (await pr.verify_payment_proof(db, first.id)).passed
    await db.commit()
    second = await add_statement(db, emp, start=date(2026, 10, 1), end=date(2026, 10, 31))
    await pr.record_proof(db, second, emp, owner, upload=png_bytes(color="green"), reference="UTR 1234-56789",
                          amount_paid=second.net_pay, paid_on=TODAY)
    await db.commit()
    again = await pr.verify_payment_proof(db, second.id)
    assert not again.passed and any(c["check"] == "duplicate_reference" for c in again.checks)


async def test_non_inr_and_non_bank_payouts_are_manual_only(db):
    owner, gym, emp = await setup_staff(db, country="AE", currency="AED")
    st = await add_statement(db, emp, start=date(2026, 10, 1), end=date(2026, 10, 31))
    mode, why = pr.payout_mode(st, emp)
    assert mode is None and "INR" in why
    with pytest.raises(pr.PayrollError):
        await pr.initiate_payout(db, st, emp, gym, owner)
    owner2, gym2, emp2 = await setup_staff(db)
    emp2.payout_method = "cash"
    st2 = await add_statement(db, emp2, start=date(2026, 10, 1), end=date(2026, 10, 31))
    assert pr.payout_mode(st2, emp2)[0] is None


async def test_cashfree_adapter_used_when_payouts_configured(db, monkeypatch):
    import sys
    import types

    from app.config import get_settings
    from app.security.crypto import encrypt_json

    owner, gym, emp = await setup_staff(db)
    emp.payout_details_enc = encrypt_json({"account_number": "123456789012", "ifsc": "HDFC0001234", "name": "A"})
    await db.commit()
    st = await add_statement(db, emp, start=date(2026, 10, 1), end=date(2026, 10, 31))
    s = get_settings()
    monkeypatch.setattr(type(s), "cashfree_payouts_enabled", property(lambda self: True))
    seen = {}

    async def create_payout(transfer_id, amount, currency, beneficiary):
        seen.update(transfer_id=transfer_id, amount=amount, currency=currency, beneficiary=beneficiary)
        return {"status": "SUCCESS", "utr": "CFUTR778899", "cf_transfer_id": "cf_1"}

    monkeypatch.setitem(sys.modules, "app.services.cashfree", types.SimpleNamespace(create_payout=create_payout))
    tx = await pr.initiate_payout(db, st, emp, gym, owner)
    await db.commit()
    assert tx.provider == "cashfree" and tx.provider_reference == "CFUTR778899" and seen["amount"] == st.net_pay
    assert seen["beneficiary"]["ifsc"] == "HDFC0001234" and st.status == StatementStatus.AWAITING_PROOF

    st2 = await add_statement(db, emp, start=date(2026, 9, 1), end=date(2026, 9, 30))

    async def failing(*args):
        return {"status": "FAILED", "message": "Beneficiary bank offline"}

    monkeypatch.setitem(sys.modules, "app.services.cashfree", types.SimpleNamespace(create_payout=failing))
    with pytest.raises(pr.PayrollError, match="offline"):
        await pr.initiate_payout(db, st2, emp, gym, owner)
    await db.commit()
    failed = (await db.execute(select(Transaction).where(Transaction.status == TxStatus.FAILED))).scalar_one()
    assert failed.salary_statement_id == st2.id and st2.status == StatementStatus.DUE

    monkeypatch.setitem(sys.modules, "app.services.cashfree", None)  # module missing -> ImportError
    res = await pr._cashfree_payout("x", D("1"), "INR", {})
    assert res["ok"] is False and "unavailable" in res["message"]


async def test_adjust_unpaid_leave_recomputes(db):
    owner, gym, emp = await setup_staff(db)
    st = await add_statement(db, emp, start=date(2026, 11, 1), end=date(2026, 11, 30))
    await pr.adjust_unpaid_leave(db, st, emp, owner, 3, gym_country="IN")
    await db.commit()
    assert st.unpaid_leave_days == 3 and st.days_worked == 27 and st.gross_pay == D("27000.00")
    with pytest.raises(pr.PayrollError):
        await pr.adjust_unpaid_leave(db, st, emp, owner, 31)
    st.status = StatementStatus.AWAITING_PROOF
    with pytest.raises(pr.PayrollError):
        await pr.adjust_unpaid_leave(db, st, emp, owner, 1)


# ============================================================================================ AI explanation
async def test_ai_explanation_is_stored_and_sanitised(db, fake_ai):
    owner, gym, emp = await setup_staff(db)
    st = await add_statement(db, emp, start=date(2026, 10, 1), end=date(2026, 10, 31))
    fake_ai(responses=[json.dumps({
        "summary": "Net pay is ₹28,200.00 after EPF.",
        "steps": ["Gross pay ₹30,000.00 for 31 days.", "A bonus of ₹99,999.00 was added."],
        "country_notes": ["EPF is 12% of basic."], "warnings": ["Estimates only."],
    })])
    assert await pr.explain_statement(db, st.id) is True
    await db.commit()
    expl, provider = pr.explanation_for(st)
    assert provider == "gateway" and expl.summary.startswith("Net pay")
    assert expl.steps == ["Gross pay ₹30,000.00 for 31 days."]  # invented amount dropped
    assert any("authoritative" in w for w in expl.warnings)
    assert st.net_pay == D("28200.00")  # numbers never change


async def test_ai_unavailable_keeps_deterministic_explanation(db, ai_down):
    owner, gym, emp = await setup_staff(db)
    st = await add_statement(db, emp, start=date(2026, 10, 1), end=date(2026, 10, 31))
    assert await pr.explain_statement(db, st.id) is False
    expl, provider = pr.explanation_for(st)
    assert provider is None and "₹28,200.00" in expl.summary and expl.steps


# ============================================================================================ termination
async def test_termination_blocked_until_everything_is_paid(db):
    owner, gym, emp = await setup_staff(db, created=datetime(2026, 10, 1, tzinfo=timezone.utc))
    branch = await make_branch(db, gym)
    await make_shift(db, emp, branch=branch, weekday=1)
    customer = await make_customer(db)
    db.add(TrainerClient(employee_id=emp.id, customer_id=customer.id, mode=ShiftMode.OFFLINE))
    await db.commit()
    oct_st = await add_statement(db, emp, start=date(2026, 10, 1), end=date(2026, 10, 31))
    blockers = await pr.termination_blockers(db, emp)
    assert len(blockers) == 2 and "unpaid" in blockers[0] and "final settlement" in blockers[1]
    with pytest.raises(pr.TerminationBlocked):
        await pr.terminate_employee(db, emp, owner)
    oct_st.status = StatementStatus.PAID
    await db.commit()
    fs = await pr.create_final_settlement(db, emp, TODAY, "dismissal", actor=owner)
    await db.commit()
    assert any("unpaid" in b for b in await pr.termination_blockers(db, emp))
    fs.status = StatementStatus.PAID
    await db.commit()
    assert await pr.termination_blockers(db, emp) == []
    await pr.terminate_employee(db, emp, owner)
    await db.commit()
    assert emp.status == EmploymentStatus.TERMINATED and emp.termination_date == TODAY
    assert "Dismissal" in emp.termination_reason
    assert await db.scalar(select(func.count(TrainerShift.id)).where(TrainerShift.employee_id == emp.id)) == 0
    assert (await db.execute(select(TrainerClient.active))).scalar_one() is False
    note = (await db.execute(select(Notification).where(Notification.user_id == emp.user_id))).scalar_one()
    assert "has ended" in note.title
    row = (await db.execute(select(AuditLog).where(AuditLog.action == "staff.terminated"))).scalar_one()
    assert row.detail["shifts_deleted"] == 1 and row.detail["clients_deactivated"] == 1
    assert await pr.termination_blockers(db, emp) == ["This staff member has already been terminated."]


async def test_never_started_staff_can_be_removed_without_settlement(db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    invited = await make_employee(db, gym, status=EmploymentStatus.INVITED)
    assert await pr.termination_blockers(db, invited) == []
    await pr.terminate_employee(db, invited, owner)
    await db.commit()
    assert invited.status == EmploymentStatus.TERMINATED


async def test_fixed_term_employee_gets_gratuity_after_one_year_in_settlement(db):
    owner, gym, emp = await setup_staff(db, hire=TODAY - timedelta(days=400),
                                        created=datetime.now(timezone.utc))
    emp.employment_type = EmploymentType.FIXED_TERM
    await db.commit()
    draft = await pr.settlement_preview(db, emp, termination_date=TODAY, reason="end_of_contract", gym_country="IN")
    assert draft.gratuity_result.eligible and draft.gratuity == D("8653.85")


async def test_payout_instructions_domestic_vs_international(db):
    from app.security.crypto import encrypt_json

    owner = await make_owner(db)
    gym = await make_gym(db, owner)  # India, INR
    local = await make_employee(db, gym)
    abroad = await make_employee(db, gym, country="AE", currency="AED")
    abroad.payout_details_enc = encrypt_json({"iban": "AE070331234567890123456", "swift": "EBILAEAD", "name": "Omar"})
    await db.commit()
    st_local = await add_statement(db, local, start=date(2026, 10, 1), end=date(2026, 10, 31))
    st_abroad = await add_statement(db, abroad, start=date(2026, 10, 1), end=date(2026, 10, 31))
    dom = pr.payout_instructions(local, gym, st_local)
    assert dom["international"] is False and "NEFT" in dom["rail"] and dom["has_details"] is False
    intl = pr.payout_instructions(abroad, gym, st_abroad)
    assert intl["international"] is True and any("SWIFT" in s for s in intl["steps"])
    assert any("FX" in s for s in intl["steps"]) and any("15CA" in p for p in intl["paperwork"])
    details = dict(intl["details"])
    assert details["IBAN"].endswith("3456") and "•" in details["IBAN"] and details["SWIFT / BIC"] == "EBILAEAD"
    assert pr.payout_mode(st_abroad, abroad)[0] is None
    d = compute(country_code="AE", currency="AED", gym_country="IN")
    assert d.rules["international"] and any("while the gym is in India" in w for w in d.rules["warnings"])
