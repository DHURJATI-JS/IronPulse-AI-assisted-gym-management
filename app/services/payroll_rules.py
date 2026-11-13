"""Country payroll rules - deterministic, documented ESTIMATES (never tax or legal advice).

Every country exposes two pure functions:

* ``taxes(gross=, monthly_salary=, fraction=, currency=, on=)`` -> :class:`MonthTaxes` for ONE calendar month:
  statutory employee deductions, employer contributions and an income-tax withholding estimate.
  ``gross`` is the prorated pay for that month, ``monthly_salary`` the full-month contractual salary and
  ``fraction`` = paid days / calendar days of the month (used to prorate annualised tax).
* ``gratuity(monthly_salary=, hire_date=, end_date=, employment_type=, reason=, currency=)`` ->
  :class:`GratuityResult` - end-of-service benefit (gratuity / indemnity / statutory severance) at termination.

Each result carries the amounts AND a human explanation of how they were obtained, so the payroll page can
show the working. Statutory thresholds are expressed in the country's own currency; when an employee is paid
in another currency the thresholds cannot be applied and the engine says so instead of guessing.

General simplifications (also shown to users as warnings):

* income tax is annualised from the monthly salary (single filer, no other income, no reliefs/allowances unless
  stated) and spread evenly over 12 months; prorated by the paid fraction of the month;
* contributions use the published rates of the stated year and ignore year-to-date caps unless stated;
* nationality-dependent schemes (GCC pensions, Singapore CPF) are flagged because nationality is unknown;
* gratuity uses the full contractual monthly salary as the "last wage" and the stated basic-pay share.

Money is ``Decimal``; every amount is rounded ROUND_HALF_UP to 0.01.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.core.utils import money

CENT = Decimal("0.01")
ZERO = Decimal("0")
ONE = Decimal("1")
TWELVE = Decimal("12")

# Termination reasons offered on the final-settlement form (code -> label).
TERMINATION_REASONS: dict[str, str] = {
    "resignation": "Resignation by the employee",
    "dismissal": "Dismissal by the employer",
    "redundancy": "Redundancy / position eliminated",
    "end_of_contract": "End of a fixed-term contract",
    "retirement": "Retirement",
    "mutual_agreement": "Mutual agreement",
    "misconduct": "Dismissal for misconduct",
    "death_or_disability": "Death or permanent disablement",
}

FIXED_TERM_TYPES = {"fixed_term", "contract"}


# ---------------------------------------------------------------------------------------------- helpers
def D(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def q(value: Any) -> Decimal:
    """Round to cents, half up (the only rounding used by payroll)."""
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def fmt(amount: Any, currency: str | None) -> str:
    return money(q(amount), currency)


def pct(rate: Decimal) -> str:
    text = f"{(rate * 100).normalize():f}"
    return f"{text}%"


def progressive(income: Decimal, brackets: list[tuple[Decimal | None, Decimal]]) -> Decimal:
    """Tax on ``income`` for marginal ``brackets`` = [(upper limit or None, rate), ...] (ascending)."""
    income = D(income)
    tax = ZERO
    lower = ZERO
    for upper, rate in brackets:
        if income <= lower:
            break
        top = income if upper is None else min(income, upper)
        tax += (top - lower) * rate
        if upper is None:
            break
        lower = upper
    return tax


def _bracket_text(brackets: list[tuple[Decimal | None, Decimal]], currency: str) -> str:
    parts = []
    lower = ZERO
    for upper, rate in brackets:
        if upper is None:
            parts.append(f"above {fmt(lower, currency)}: {pct(rate)}")
        else:
            parts.append(f"{fmt(lower, currency)}-{fmt(upper, currency)}: {pct(rate)}")
            lower = upper
    return "; ".join(parts)


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


@dataclass(frozen=True, slots=True)
class Service:
    """Length of service from ``start`` to ``end`` (both inclusive)."""

    years: int
    months: int
    days: int
    total_days: int

    @property
    def exact_years(self) -> Decimal:
        """Years incl. the fraction (months/12 + days/365) - used for pro-rata rules."""
        return D(self.years) + D(self.months) / TWELVE + D(self.days) / Decimal(365)

    @property
    def label(self) -> str:
        parts = []
        for n, unit in ((self.years, "year"), (self.months, "month"), (self.days, "day")):
            if n:
                parts.append(f"{n} {unit}{'s' if n != 1 else ''}")
        return ", ".join(parts) or "less than a day"

    def rounded_years(self) -> int:
        """Completed years; a final partial year of MORE than six months counts as a full year."""
        extra = 1 if (self.months > 6 or (self.months == 6 and self.days > 0)) else 0
        return self.years + extra


def service_length(start: date, end: date) -> Service:
    end_excl = end + timedelta(days=1)
    if end_excl <= start:
        return Service(0, 0, 0, 0)
    total_months = (end_excl.year - start.year) * 12 + (end_excl.month - start.month)
    if add_months(start, total_months) > end_excl:
        total_months -= 1
    anchor = add_months(start, total_months)
    return Service(total_months // 12, total_months % 12, (end_excl - anchor).days, (end_excl - start).days)


# ---------------------------------------------------------------------------------------------- results
@dataclass(slots=True)
class Line:
    code: str
    label: str
    amount: Decimal
    explanation: str

    def to_json(self) -> dict[str, str]:
        return {"code": self.code, "label": self.label, "amount": str(q(self.amount)), "explanation": self.explanation}


@dataclass(slots=True)
class MonthTaxes:
    employee: list[Line] = field(default_factory=list)  # statutory deductions from the employee's pay
    employer: list[Line] = field(default_factory=list)  # employer contributions on top of gross pay
    withholding: Line | None = None  # income-tax withholding estimate
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class GratuityResult:
    eligible: bool
    amount: Decimal
    years_counted: Decimal
    explanation: str
    steps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    label: str = "Gratuity"

    def to_json(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "amount": str(q(self.amount)),
            "years_counted": str(self.years_counted),
            "explanation": self.explanation,
            "steps": list(self.steps),
            "warnings": list(self.warnings),
            "label": self.label,
        }


def no_gratuity(text: str, label: str = "Gratuity") -> GratuityResult:
    return GratuityResult(False, ZERO, ZERO, text, [], [], label)


# ---------------------------------------------------------------------------------------------- base
class CountryRules:
    code = "DEFAULT"
    name = "Other countries"
    currency: str | None = None  # thresholds/caps are expressed in this currency
    rule_set = "No country-specific rules"
    gratuity_label = "Gratuity"
    notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = (
        "IronPulse has no payroll rules for this country: no withholding, social contributions or gratuity were "
        "calculated. Please consult a local accountant before paying.",
    )
    payout_rail = "a local bank transfer"

    # -- public API (with currency guard) ------------------------------------------------------------
    def taxes(self, *, gross: Decimal, monthly_salary: Decimal, fraction: Decimal, currency: str,
              on: date | None = None) -> MonthTaxes:
        gross, monthly_salary, fraction = D(gross), D(monthly_salary), D(fraction)
        if self.currency and currency.upper() != self.currency:
            return MonthTaxes(warnings=[
                f"Salary is paid in {currency.upper()} but {self.name}'s statutory thresholds are in {self.currency}: "
                "withholding and social contributions were NOT estimated. Calculate them with a local accountant."
            ])
        if gross <= 0:
            return MonthTaxes()
        return self._taxes(gross=gross, monthly_salary=monthly_salary, fraction=fraction, currency=currency.upper(),
                           on=on or date.today())

    def gratuity(self, *, monthly_salary: Decimal, hire_date: date, end_date: date, employment_type: str = "full_time",
                 reason: str | None = None, currency: str = "") -> GratuityResult:
        if end_date < hire_date:
            return no_gratuity("The termination date is before the hire date - no service to count.", self.gratuity_label)
        res = self._gratuity(monthly_salary=D(monthly_salary), hire_date=hire_date, end_date=end_date,
                             employment_type=(employment_type or "full_time").lower(), reason=(reason or "").lower(),
                             currency=(currency or self.currency or "").upper())
        res.amount = q(max(res.amount, ZERO))
        res.label = res.label or self.gratuity_label
        if reason == "misconduct" and res.amount > 0:
            res.warnings.append(
                "The reason is misconduct: local law may allow the end-of-service benefit to be reduced or forfeited. "
                "The full estimate is shown - check before paying less."
            )
        return res

    # -- overridable ---------------------------------------------------------------------------------
    def _taxes(self, *, gross: Decimal, monthly_salary: Decimal, fraction: Decimal, currency: str, on: date) -> MonthTaxes:
        return MonthTaxes()

    def _gratuity(self, *, monthly_salary: Decimal, hire_date: date, end_date: date, employment_type: str, reason: str,
                  currency: str) -> GratuityResult:
        return no_gratuity("No statutory gratuity is computed for this country. Check the employment contract.",
                           self.gratuity_label)

    def cap_applies(self, currency: str) -> bool:
        return not self.currency or currency.upper() == self.currency

    def describe(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name, "rule_set": self.rule_set, "notes": list(self.notes),
                "warnings": list(self.warnings)}


# ---------------------------------------------------------------------------------------------- India
class India(CountryRules):
    code = "IN"
    name = "India"
    currency = "INR"
    rule_set = "India - FY 2025-26 new tax regime, EPF/ESI, Code on Social Security 2020 (gratuity)"
    warnings = (
        "Income-tax TDS is an estimate under the new regime (no other income, exemptions or previous-employer "
        "salary). Surcharge above INR 50 lakh and state Professional Tax are not included.",
    )
    notes = (
        "Basic pay is assumed to be 50% of gross (Labour Codes wage definition) for EPF and gratuity.",
        "EPF: 12% employee + 12% employer on basic capped at INR 15,000 a month (employer share includes 8.33% EPS).",
        "ESI applies only while gross wages are INR 21,000 a month or less.",
    )
    payout_rail = "NEFT / RTGS / IMPS bank transfer or UPI"

    BASIC_SHARE = Decimal("0.50")
    EPF_RATE = Decimal("0.12")
    EPF_WAGE_CAP = Decimal("15000")
    ESI_LIMIT = Decimal("21000")
    ESI_EMPLOYEE = Decimal("0.0075")
    ESI_EMPLOYER = Decimal("0.0325")
    STANDARD_DEDUCTION = Decimal("75000")
    REBATE_LIMIT = Decimal("1200000")
    CESS = Decimal("0.04")
    SLABS: list[tuple[Decimal | None, Decimal]] = [
        (Decimal("400000"), Decimal("0")),
        (Decimal("800000"), Decimal("0.05")),
        (Decimal("1200000"), Decimal("0.10")),
        (Decimal("1600000"), Decimal("0.15")),
        (Decimal("2000000"), Decimal("0.20")),
        (Decimal("2400000"), Decimal("0.25")),
        (None, Decimal("0.30")),
    ]
    GRATUITY_CAP = Decimal("2000000")

    def annual_tax(self, annual_gross: Decimal) -> tuple[Decimal, list[str]]:
        cur = "INR"
        taxable = max(ZERO, annual_gross - self.STANDARD_DEDUCTION)
        slab_tax = progressive(taxable, self.SLABS)
        steps = [
            f"Annual salary {fmt(annual_gross, cur)} - standard deduction {fmt(self.STANDARD_DEDUCTION, cur)} = "
            f"taxable {fmt(taxable, cur)}",
            f"New-regime slab tax = {fmt(slab_tax, cur)}",
        ]
        if taxable <= self.REBATE_LIMIT:
            tax = ZERO
            if slab_tax > 0:
                steps.append(f"Section 87A rebate: taxable income is within {fmt(self.REBATE_LIMIT, cur)}, so tax is nil")
        else:
            relief_cap = taxable - self.REBATE_LIMIT
            tax = min(slab_tax, relief_cap)
            if tax < slab_tax:
                steps.append(f"Marginal relief: tax limited to the income above {fmt(self.REBATE_LIMIT, cur)} = {fmt(tax, cur)}")
        total = tax * (ONE + self.CESS)
        if tax > 0:
            steps.append(f"+ 4% health & education cess = {fmt(total, cur)} a year")
        return total, steps

    def _taxes(self, *, gross, monthly_salary, fraction, currency, on):
        cur = currency
        out = MonthTaxes()
        basic = gross * self.BASIC_SHARE
        epf_base = min(basic, self.EPF_WAGE_CAP)
        epf = q(epf_base * self.EPF_RATE)
        epf_text = (f"12% of basic {fmt(epf_base, cur)}" + (f" (basic {fmt(basic, cur)} capped at {fmt(self.EPF_WAGE_CAP, cur)})"
                                                              if basic > self.EPF_WAGE_CAP else " (50% of gross)"))
        out.employee.append(Line("epf_employee", "EPF - employee 12%", epf, epf_text))
        out.employer.append(Line("epf_employer", "EPF + EPS - employer 12%", epf, epf_text))
        if monthly_salary <= self.ESI_LIMIT:
            out.employee.append(Line("esi_employee", "ESI - employee 0.75%", q(gross * self.ESI_EMPLOYEE),
                                     f"0.75% of gross {fmt(gross, cur)} (wages within {fmt(self.ESI_LIMIT, cur)})"))
            out.employer.append(Line("esi_employer", "ESI - employer 3.25%", q(gross * self.ESI_EMPLOYER),
                                     f"3.25% of gross {fmt(gross, cur)}"))
        else:
            out.notes.append(f"ESI not applicable: monthly wages above {fmt(self.ESI_LIMIT, cur)}.")
        annual = monthly_salary * TWELVE
        annual_tax, steps = self.annual_tax(annual)
        monthly = q(annual_tax / TWELVE * fraction)
        text = "; ".join(steps) + f"; monthly TDS = annual / 12" + (f" x {fraction:.4f} of the month" if fraction < 1 else "")
        out.withholding = Line("tds", "Income tax (TDS) - estimate", monthly, text)
        if annual > Decimal("5000000"):
            out.warnings.append("Annual income above INR 50 lakh: surcharge applies and is NOT included in this estimate.")
        return out

    def _gratuity(self, *, monthly_salary, hire_date, end_date, employment_type, reason, currency):
        cur = currency or "INR"
        svc = service_length(hire_date, end_date)
        years = svc.rounded_years()
        fixed_term = employment_type in FIXED_TERM_TYPES
        steps = [f"Service: {svc.label} ({hire_date:%d %b %Y} - {end_date:%d %b %Y})"]
        if employment_type == "intern":
            return GratuityResult(False, ZERO, ZERO, "Interns and apprentices are not covered by gratuity.", steps)
        if reason == "death_or_disability":
            eligible = True
            steps.append("Death or disablement: the minimum-service condition does not apply")
        elif fixed_term:
            eligible = svc.years >= 1
            steps.append("Fixed-term employee: eligible after 1 year of service (Code on Social Security 2020, "
                         "in force from 21 Nov 2025)")
        else:
            eligible = years >= 5
            steps.append("Permanent employee: eligible after 5 years of continuous service")
        if svc.months > 6 or (svc.months == 6 and svc.days > 0):
            steps.append(f"The final partial year of more than 6 months counts as a full year: {years} years")
        else:
            steps.append(f"Completed years counted: {years}")
        if not eligible:
            return GratuityResult(False, ZERO, D(years),
                                  f"Not eligible yet: {svc.label} of service.", steps)
        basic = monthly_salary * self.BASIC_SHARE
        amount = basic * Decimal(15) / Decimal(26) * D(years)
        steps.append(f"Last monthly wage (basic + DA, assumed 50% of gross) = {fmt(basic, cur)}")
        steps.append(f"15/26 x {fmt(basic, cur)} x {years} years = {fmt(amount, cur)}")
        warnings: list[str] = []
        if self.cap_applies(cur):
            if amount > self.GRATUITY_CAP:
                steps.append(f"Capped at the statutory ceiling {fmt(self.GRATUITY_CAP, cur)}")
                amount = self.GRATUITY_CAP
        else:
            warnings.append("Gratuity ceiling (INR 20 lakh) not applied because the salary is not in INR.")
        return GratuityResult(True, q(amount), D(years),
                              f"15/26 of the last basic wage for each of {years} counted years"
                              + (" (capped)" if amount == self.GRATUITY_CAP else ""), steps, warnings)


# ---------------------------------------------------------------------------------------------- Gulf (GCC)
class _GulfRules(CountryRules):
    """GCC countries: no personal income tax; salary through the Wage Protection System (WPS)."""

    payout_rail = "a WPS (Wage Protection System) salary transfer"
    BASIC_SHARE = Decimal("0.60")

    def _no_tax(self) -> MonthTaxes:
        return MonthTaxes(notes=[f"{self.name} levies no personal income tax on salaries."])


class UAE(_GulfRules):
    code = "AE"
    name = "United Arab Emirates"
    currency = "AED"
    rule_set = "UAE - Federal Decree-Law 33/2021 (end-of-service gratuity)"
    gratuity_label = "End-of-service gratuity"
    warnings = ("GPSSA pension contributions apply only to UAE / GCC nationals and are not included.",)
    notes = (
        "Basic pay is assumed to be 60% of gross; daily basic = basic x 12 / 365.",
        "Gratuity: 21 days' basic per year for the first 5 years, 30 days per year after, pro-rata for part years, "
        "after at least 1 year of service; capped at 2 years' basic wage. Resignation no longer reduces it.",
    )

    def _taxes(self, **kwargs):
        return self._no_tax()

    def _gratuity(self, *, monthly_salary, hire_date, end_date, employment_type, reason, currency):
        cur = currency or "AED"
        svc = service_length(hire_date, end_date)
        steps = [f"Service: {svc.label}"]
        if svc.years < 1:
            return GratuityResult(False, ZERO, ZERO, "Not eligible: less than 1 year of continuous service.", steps)
        years = svc.exact_years
        basic = monthly_salary * self.BASIC_SHARE
        daily = basic * TWELVE / Decimal(365)
        first = min(years, Decimal(5)) * 21 * daily
        rest = max(ZERO, years - 5) * 30 * daily
        amount = first + rest
        steps += [
            f"Basic (60% of gross) = {fmt(basic, cur)}; daily basic = {fmt(basic, cur)} x 12 / 365 = {fmt(daily, cur)}",
            f"First 5 years: {min(years, Decimal(5)):.4f} x 21 days x {fmt(daily, cur)} = {fmt(first, cur)}",
        ]
        if rest:
            steps.append(f"After 5 years: {years - 5:.4f} x 30 days x {fmt(daily, cur)} = {fmt(rest, cur)}")
        cap = basic * 24
        if amount > cap:
            steps.append(f"Capped at 2 years' basic wage = {fmt(cap, cur)}")
            amount = cap
        return GratuityResult(True, q(amount), years.quantize(Decimal("0.0001")),
                              f"21/30 days' basic per year of service ({svc.label})", steps)


class SaudiArabia(_GulfRules):
    code = "SA"
    name = "Saudi Arabia"
    currency = "SAR"
    rule_set = "Saudi Labour Law arts. 84-85 (end-of-service award), GOSI occupational hazards"
    gratuity_label = "End-of-service award"
    warnings = ("GOSI pension / SANED contributions for Saudi nationals are not included (nationality unknown).",)
    notes = (
        "Award: half a month's wage per year for the first 5 years, a full month per year after, pro-rata; based on "
        "the full monthly salary (simplification: last wage incl. allowances).",
        "Resignation: nothing under 2 years, 1/3 for 2-5 years, 2/3 for 5-10 years, full award after 10 years.",
        "Employer pays 2% GOSI occupational hazards on wages up to SAR 45,000 for every employee.",
    )
    GOSI_HAZARD = Decimal("0.02")
    GOSI_CAP = Decimal("45000")

    def _taxes(self, *, gross, monthly_salary, fraction, currency, on):
        out = self._no_tax()
        base = min(gross, self.GOSI_CAP)
        out.employer.append(Line("gosi_hazards", "GOSI occupational hazards - employer 2%", q(base * self.GOSI_HAZARD),
                                 f"2% of contributory wage {fmt(base, currency)}"))
        return out

    def _gratuity(self, *, monthly_salary, hire_date, end_date, employment_type, reason, currency):
        cur = currency or "SAR"
        svc = service_length(hire_date, end_date)
        years = svc.exact_years
        steps = [f"Service: {svc.label} ({years:.4f} years)"]
        first = min(years, Decimal(5)) * Decimal("0.5") * monthly_salary
        rest = max(ZERO, years - 5) * monthly_salary
        full = first + rest
        steps.append(f"Half a month x {min(years, Decimal(5)):.4f} years = {fmt(first, cur)}")
        if rest:
            steps.append(f"One month x {years - 5:.4f} years = {fmt(rest, cur)}")
        factor = ONE
        if reason == "resignation":
            if years < 2:
                factor = ZERO
            elif years < 5:
                factor = ONE / 3
            elif years < 10:
                factor = Decimal(2) / 3
            steps.append(f"Resignation after {years:.2f} years: {'none' if factor == 0 else 'full' if factor == 1 else f'{factor:.4f} of the award'}")
        amount = full * factor
        eligible = amount > 0
        return GratuityResult(eligible, q(amount), years.quantize(Decimal("0.0001")),
                              "End-of-service award under arts. 84-85" if eligible else "No award for this resignation",
                              steps)


class Qatar(_GulfRules):
    code = "QA"
    name = "Qatar"
    currency = "QAR"
    rule_set = "Qatar Labour Law 14/2004 art. 54 (end-of-service gratuity)"
    gratuity_label = "End-of-service gratuity"
    warnings = ("GRSIA pension contributions for Qatari nationals are not included.",)
    notes = ("Gratuity: at least 3 weeks' (21 days') basic wage per year after 1 year of service, pro-rata; basic "
             "assumed 60% of gross; daily basic = basic x 12 / 365.",)

    def _taxes(self, **kwargs):
        return self._no_tax()

    def _gratuity(self, *, monthly_salary, hire_date, end_date, employment_type, reason, currency):
        cur = currency or "QAR"
        svc = service_length(hire_date, end_date)
        steps = [f"Service: {svc.label}"]
        if svc.years < 1:
            return GratuityResult(False, ZERO, ZERO, "Not eligible: less than 1 year of service.", steps)
        years = svc.exact_years
        basic = monthly_salary * self.BASIC_SHARE
        daily = basic * TWELVE / Decimal(365)
        amount = years * 21 * daily
        steps.append(f"{years:.4f} years x 21 days x daily basic {fmt(daily, cur)} = {fmt(amount, cur)}")
        return GratuityResult(True, q(amount), years.quantize(Decimal("0.0001")), "3 weeks' basic per year of service", steps)


class Kuwait(_GulfRules):
    code = "KW"
    name = "Kuwait"
    currency = "KWD"
    rule_set = "Kuwait Labour Law 6/2010 arts. 51-53 (end-of-service indemnity)"
    gratuity_label = "End-of-service indemnity"
    warnings = ("PIFSS social-security contributions for Kuwaiti nationals are not included.",)
    notes = (
        "Indemnity: 15 days' pay per year for the first 5 years and a month per year after, pro-rata, capped at "
        "18 months' pay; based on the full monthly salary.",
        "Resignation: nothing under 3 years, half for 3-5 years, two-thirds for 5-10 years, full after 10 years.",
    )

    def _taxes(self, **kwargs):
        return self._no_tax()

    def _gratuity(self, *, monthly_salary, hire_date, end_date, employment_type, reason, currency):
        cur = currency or "KWD"
        svc = service_length(hire_date, end_date)
        years = svc.exact_years
        steps = [f"Service: {svc.label} ({years:.4f} years)"]
        first = min(years, Decimal(5)) * Decimal("0.5") * monthly_salary
        rest = max(ZERO, years - 5) * monthly_salary
        full = min(first + rest, monthly_salary * 18)
        steps.append(f"15 days (half a month) x {min(years, Decimal(5)):.4f} years = {fmt(first, cur)}")
        if rest:
            steps.append(f"One month x {years - 5:.4f} years = {fmt(rest, cur)}")
        if first + rest > full:
            steps.append(f"Capped at 18 months' pay = {fmt(full, cur)}")
        factor = ONE
        if reason == "resignation":
            factor = ZERO if years < 3 else Decimal("0.5") if years < 5 else Decimal(2) / 3 if years < 10 else ONE
            steps.append(f"Resignation tier applied: x {factor:.4f}")
        amount = full * factor
        return GratuityResult(amount > 0, q(amount), years.quantize(Decimal("0.0001")),
                              "End-of-service indemnity (arts. 51-53)", steps)


class Oman(_GulfRules):
    code = "OM"
    name = "Oman"
    currency = "OMR"
    rule_set = "Oman Labour Law (Royal Decree 53/2023) - end-of-service gratuity for non-Omanis"
    gratuity_label = "End-of-service gratuity"
    warnings = (
        "Omani nationals are covered by the Social Protection Fund instead (not computed). Check whether your "
        "workers are already enrolled in the end-of-service savings scheme.",
    )
    notes = ("Gratuity: one month's basic wage per year of service after 1 year, pro-rata; basic assumed 60% of gross.",)

    def _taxes(self, **kwargs):
        return self._no_tax()

    def _gratuity(self, *, monthly_salary, hire_date, end_date, employment_type, reason, currency):
        cur = currency or "OMR"
        svc = service_length(hire_date, end_date)
        steps = [f"Service: {svc.label}"]
        if svc.years < 1:
            return GratuityResult(False, ZERO, ZERO, "Not eligible: less than 1 year of service.", steps)
        years = svc.exact_years
        basic = monthly_salary * self.BASIC_SHARE
        amount = years * basic
        steps.append(f"{years:.4f} years x one month's basic {fmt(basic, cur)} = {fmt(amount, cur)}")
        return GratuityResult(True, q(amount), years.quantize(Decimal("0.0001")), "One month's basic per year of service", steps)


class Bahrain(_GulfRules):
    code = "BH"
    name = "Bahrain"
    currency = "BHD"
    rule_set = "Bahrain Labour Law 36/2012 art. 116 (leaving indemnity)"
    gratuity_label = "Leaving indemnity"
    warnings = (
        "Since March 2024 the leaving indemnity for non-Bahrainis is funded through monthly SIO contributions - "
        "if you already pay them, the SIO pays the employee and this amount is not due from you.",
    )
    notes = ("Indemnity: half a month's wage per year for the first 3 years and one month per year after, pro-rata, "
             "after 1 year of service; based on the full monthly salary.",)

    def _taxes(self, **kwargs):
        return self._no_tax()

    def _gratuity(self, *, monthly_salary, hire_date, end_date, employment_type, reason, currency):
        cur = currency or "BHD"
        svc = service_length(hire_date, end_date)
        steps = [f"Service: {svc.label}"]
        if svc.years < 1:
            return GratuityResult(False, ZERO, ZERO, "Not eligible: less than 1 year of service.", steps)
        years = svc.exact_years
        first = min(years, Decimal(3)) * Decimal("0.5") * monthly_salary
        rest = max(ZERO, years - 3) * monthly_salary
        steps.append(f"Half a month x {min(years, Decimal(3)):.4f} years = {fmt(first, cur)}")
        if rest:
            steps.append(f"One month x {years - 3:.4f} years = {fmt(rest, cur)}")
        return GratuityResult(True, q(first + rest), years.quantize(Decimal("0.0001")), "Leaving indemnity (art. 116)", steps)


# ---------------------------------------------------------------------------------------------- United States
class UnitedStates(CountryRules):
    code = "US"
    name = "United States"
    currency = "USD"
    rule_set = "US federal - 2025 single-filer brackets, FICA"
    gratuity_label = "Severance"
    warnings = (
        "State and local income taxes are NOT included - add your state's withholding.",
        "Federal withholding is an annualised single-filer estimate (no W-4 adjustments); IRS Publication 15-T "
        "tables and later inflation adjustments differ slightly.",
    )
    notes = (
        "Standard deduction USD 15,000; 2025 brackets 10/12/22/24/32/35/37%.",
        "Social Security 6.2% (employee and employer) up to the USD 176,100 wage base; Medicare 1.45% each, plus "
        "0.9% Additional Medicare on wages above USD 200,000 (employee only).",
        "FUTA / state unemployment insurance (employer) are not included.",
    )
    payout_rail = "ACH direct deposit or a domestic wire"
    STANDARD_DEDUCTION = Decimal("15000")
    BRACKETS = [
        (Decimal("11925"), Decimal("0.10")),
        (Decimal("48475"), Decimal("0.12")),
        (Decimal("103350"), Decimal("0.22")),
        (Decimal("197300"), Decimal("0.24")),
        (Decimal("250525"), Decimal("0.32")),
        (Decimal("626350"), Decimal("0.35")),
        (None, Decimal("0.37")),
    ]
    SS_RATE = Decimal("0.062")
    SS_WAGE_BASE = Decimal("176100")
    MEDICARE = Decimal("0.0145")
    ADDL_MEDICARE = Decimal("0.009")
    ADDL_THRESHOLD = Decimal("200000")

    def _taxes(self, *, gross, monthly_salary, fraction, currency, on):
        cur = currency
        out = MonthTaxes()
        annual = monthly_salary * TWELVE
        taxable = max(ZERO, annual - self.STANDARD_DEDUCTION)
        annual_tax = progressive(taxable, self.BRACKETS)
        monthly = q(annual_tax / TWELVE * fraction)
        out.withholding = Line(
            "federal_withholding", "Federal income tax - estimate", monthly,
            f"Annual wages {fmt(annual, cur)} - standard deduction {fmt(self.STANDARD_DEDUCTION, cur)} = "
            f"{fmt(taxable, cur)}; 2025 brackets -> {fmt(annual_tax, cur)} a year; / 12"
            + (f" x {fraction:.4f} of the month" if fraction < 1 else ""),
        )
        ss_share = min(ONE, self.SS_WAGE_BASE / annual) if annual > 0 else ONE
        ss_base = gross * ss_share
        ss = q(ss_base * self.SS_RATE)
        ss_text = f"6.2% of {fmt(ss_base, cur)}" + (" (wage base USD 176,100 spread over the year)" if ss_share < 1 else "")
        out.employee.append(Line("social_security", "Social Security 6.2%", ss, ss_text))
        out.employer.append(Line("social_security_er", "Social Security - employer 6.2%", ss, ss_text))
        med = q(gross * self.MEDICARE)
        out.employee.append(Line("medicare", "Medicare 1.45%", med, f"1.45% of {fmt(gross, cur)}"))
        out.employer.append(Line("medicare_er", "Medicare - employer 1.45%", med, f"1.45% of {fmt(gross, cur)}"))
        if annual > self.ADDL_THRESHOLD:
            extra = q((annual - self.ADDL_THRESHOLD) * self.ADDL_MEDICARE / TWELVE * fraction)
            out.employee.append(Line("additional_medicare", "Additional Medicare 0.9%", extra,
                                     f"0.9% of annual wages above {fmt(self.ADDL_THRESHOLD, cur)}, spread monthly"))
        return out

    def _gratuity(self, **kwargs):
        return no_gratuity("No statutory severance or gratuity in the US - pay what the contract or company policy "
                           "promises. Final-paycheck deadlines are set by state law.", "Severance")


# ---------------------------------------------------------------------------------------------- United Kingdom
class UnitedKingdom(CountryRules):
    code = "GB"
    name = "United Kingdom"
    currency = "GBP"
    rule_set = "UK - PAYE 2025/26 (England, Wales & NI rates), Class 1 NI, auto-enrolment pension"
    gratuity_label = "Statutory redundancy"
    warnings = (
        "PAYE is an annualised estimate with the standard 1257L code; Scottish income-tax rates differ.",
        "Pension auto-enrolment is assumed (5% employee / 3% employer on qualifying earnings) - remove it if the "
        "employee opted out.",
    )
    notes = (
        "Personal allowance GBP 12,570 (tapered above GBP 100,000); 20% / 40% / 45% bands.",
        "Employee NI 8% between GBP 1,048 and GBP 4,189 a month, 2% above; employer NI 15% above GBP 417 a month.",
    )
    payout_rail = "Faster Payments / BACS"
    PERSONAL_ALLOWANCE = Decimal("12570")
    BANDS = [(Decimal("37700"), Decimal("0.20")), (Decimal("125140"), Decimal("0.40")), (None, Decimal("0.45"))]
    NI_PT_MONTH = Decimal("1048")
    NI_UEL_MONTH = Decimal("4189")
    NI_ST_MONTH = Decimal("417")
    QE_LOWER_MONTH = Decimal("520")
    QE_UPPER_MONTH = Decimal("4189")

    def annual_tax(self, annual: Decimal) -> tuple[Decimal, Decimal]:
        allowance = self.PERSONAL_ALLOWANCE
        if annual > 100000:
            allowance = max(ZERO, allowance - (annual - 100000) / 2)
        taxable = max(ZERO, annual - allowance)
        return progressive(taxable, self.BANDS), allowance

    def _taxes(self, *, gross, monthly_salary, fraction, currency, on):
        cur = currency
        out = MonthTaxes()
        annual = monthly_salary * TWELVE
        tax, allowance = self.annual_tax(annual)
        out.withholding = Line(
            "paye", "PAYE income tax - estimate", q(tax / TWELVE * fraction),
            f"Annual pay {fmt(annual, cur)} - personal allowance {fmt(allowance, cur)}; 20/40/45% bands -> "
            f"{fmt(tax, cur)} a year; / 12" + (f" x {fraction:.4f} of the month" if fraction < 1 else ""),
        )
        main = max(ZERO, min(gross, self.NI_UEL_MONTH) - self.NI_PT_MONTH) * Decimal("0.08")
        upper = max(ZERO, gross - self.NI_UEL_MONTH) * Decimal("0.02")
        out.employee.append(Line("ni_employee", "National Insurance - employee", q(main + upper),
                                 f"8% of pay between {fmt(self.NI_PT_MONTH, cur)} and {fmt(self.NI_UEL_MONTH, cur)}"
                                 f", 2% above (pay {fmt(gross, cur)})"))
        out.employer.append(Line("ni_employer", "National Insurance - employer 15%",
                                 q(max(ZERO, gross - self.NI_ST_MONTH) * Decimal("0.15")),
                                 f"15% of pay above {fmt(self.NI_ST_MONTH, cur)}"))
        qualifying = max(ZERO, min(gross, self.QE_UPPER_MONTH) - self.QE_LOWER_MONTH)
        if qualifying > 0:
            out.employee.append(Line("pension_employee", "Workplace pension - employee 5%", q(qualifying * Decimal("0.05")),
                                     f"5% of qualifying earnings {fmt(qualifying, cur)}"))
            out.employer.append(Line("pension_employer", "Workplace pension - employer 3%", q(qualifying * Decimal("0.03")),
                                     f"3% of qualifying earnings {fmt(qualifying, cur)}"))
        return out

    def _gratuity(self, *, monthly_salary, hire_date, end_date, employment_type, reason, currency):
        svc = service_length(hire_date, end_date)
        if reason == "redundancy" and svc.years >= 2:
            return GratuityResult(False, ZERO, D(svc.years),
                                  "Statutory redundancy pay is due (2+ years' service) but depends on age and the "
                                  "capped weekly pay - calculate it with the GOV.UK redundancy calculator.",
                                  [f"Service: {svc.label}"], ["Statutory redundancy pay not included in this statement."],
                                  "Statutory redundancy")
        return no_gratuity("No statutory gratuity in the UK (statutory redundancy pay applies only to redundancies "
                           "after 2 years' service).", "Statutory redundancy")


# ---------------------------------------------------------------------------------------------- Canada
class Canada(CountryRules):
    code = "CA"
    name = "Canada"
    currency = "CAD"
    rule_set = "Canada federal - 2025 brackets, CPP/CPP2, EI"
    gratuity_label = "Severance"
    warnings = ("Provincial / territorial income tax is NOT included.",
                "Federal tax uses the 2025 blended 14.5% lowest rate and the full basic personal amount.")
    notes = ("CPP 5.95% on earnings between CAD 3,500 and 71,300 (+ CPP2 4% up to 81,200); EI 1.64% up to CAD 65,700 "
             "insurable earnings; employer pays the same CPP and 1.4x the EI premium.",)
    payout_rail = "direct deposit (EFT) or Interac e-Transfer"
    BRACKETS = [
        (Decimal("57375"), Decimal("0.145")),
        (Decimal("114750"), Decimal("0.205")),
        (Decimal("177882"), Decimal("0.26")),
        (Decimal("253414"), Decimal("0.29")),
        (None, Decimal("0.33")),
    ]
    BPA = Decimal("16129")

    def _taxes(self, *, gross, monthly_salary, fraction, currency, on):
        cur = currency
        out = MonthTaxes()
        annual = monthly_salary * TWELVE
        cpp_annual = max(ZERO, min(annual, Decimal("71300")) - Decimal("3500")) * Decimal("0.0595")
        cpp2_annual = max(ZERO, min(annual, Decimal("81200")) - Decimal("71300")) * Decimal("0.04")
        ei_annual = min(annual, Decimal("65700")) * Decimal("0.0164")
        tax = max(ZERO, progressive(annual, self.BRACKETS) - self.BPA * Decimal("0.145"))
        out.withholding = Line("federal_tax", "Federal income tax - estimate", q(tax / TWELVE * fraction),
                               f"2025 federal brackets on {fmt(annual, cur)} minus the basic personal amount credit "
                               f"({fmt(self.BPA, cur)} x 14.5%) -> {fmt(tax, cur)} a year; / 12")
        cpp = q((cpp_annual + cpp2_annual) / TWELVE * fraction)
        ei = q(ei_annual / TWELVE * fraction)
        out.employee.append(Line("cpp", "CPP / CPP2", cpp, "5.95% between CAD 3,500 and 71,300 (+4% CPP2 to 81,200), annualised"))
        out.employee.append(Line("ei", "Employment Insurance 1.64%", ei, "1.64% of insurable earnings up to CAD 65,700, annualised"))
        out.employer.append(Line("cpp_er", "CPP / CPP2 - employer", cpp, "Matches the employee contribution"))
        out.employer.append(Line("ei_er", "EI - employer (1.4x)", q(ei * Decimal("1.4")), "1.4 x the employee EI premium"))
        return out

    def _gratuity(self, **kwargs):
        return no_gratuity("No statutory gratuity in Canada. Termination / severance pay under the provincial "
                           "Employment Standards Act is not computed.", "Severance")


# ---------------------------------------------------------------------------------------------- Australia
class Australia(CountryRules):
    code = "AU"
    name = "Australia"
    currency = "AUD"
    rule_set = "Australia - 2025-26 resident rates, Medicare levy, Superannuation Guarantee 12%"
    gratuity_label = "Redundancy pay"
    warnings = ("PAYG is an annualised resident estimate with the tax-free threshold and LITO; the ATO withholding "
                "schedules and Medicare levy low-income reductions differ slightly.",)
    notes = ("Brackets: 0-18,200 nil; 16% to 45,000; 30% to 135,000; 37% to 190,000; 45% above; Medicare levy 2%.",
             "Superannuation Guarantee 12% is paid by the employer on top of salary.")
    payout_rail = "an NPP / BECS bank transfer"
    BRACKETS = [
        (Decimal("18200"), Decimal("0")),
        (Decimal("45000"), Decimal("0.16")),
        (Decimal("135000"), Decimal("0.30")),
        (Decimal("190000"), Decimal("0.37")),
        (None, Decimal("0.45")),
    ]

    @staticmethod
    def lito(income: Decimal) -> Decimal:
        if income <= 37500:
            return Decimal("700")
        if income <= 45000:
            return Decimal("700") - (income - 37500) * Decimal("0.05")
        if income <= 66667:
            return max(ZERO, Decimal("325") - (income - 45000) * Decimal("0.015"))
        return ZERO

    def _taxes(self, *, gross, monthly_salary, fraction, currency, on):
        cur = currency
        out = MonthTaxes()
        annual = monthly_salary * TWELVE
        tax = max(ZERO, progressive(annual, self.BRACKETS) - self.lito(annual)) + annual * Decimal("0.02")
        out.withholding = Line("payg", "PAYG withholding - estimate", q(tax / TWELVE * fraction),
                               f"Resident rates on {fmt(annual, cur)} minus LITO plus 2% Medicare levy = {fmt(tax, cur)} "
                               "a year; / 12")
        out.employer.append(Line("super_guarantee", "Superannuation Guarantee 12%", q(gross * Decimal("0.12")),
                                 f"12% of ordinary-time earnings {fmt(gross, cur)}"))
        return out

    def _gratuity(self, **kwargs):
        return no_gratuity("No gratuity in Australia. NES redundancy pay and state long-service leave are not "
                           "computed.", "Redundancy pay")


# ---------------------------------------------------------------------------------------------- Singapore
class Singapore(CountryRules):
    code = "SG"
    name = "Singapore"
    currency = "SGD"
    rule_set = "Singapore - CPF 2025 (age 55 and below), Skills Development Levy"
    gratuity_label = "Retrenchment benefit"
    warnings = ("CPF applies only to Singapore citizens and permanent residents - remove it for work-pass holders.",)
    notes = ("No monthly income-tax withholding for residents: tax is assessed annually by IRAS.",
             "CPF: 20% employee + 17% employer on ordinary wages up to SGD 7,400 a month; SDL 0.25% (SGD 2-11.25).")
    payout_rail = "a FAST / GIRO transfer or PayNow"
    CPF_CEILING = Decimal("7400")

    def _taxes(self, *, gross, monthly_salary, fraction, currency, on):
        cur = currency
        out = MonthTaxes(notes=["Singapore residents pay income tax annually - nothing is withheld monthly."])
        wage = min(gross, self.CPF_CEILING)
        if gross > 750:
            out.employee.append(Line("cpf_employee", "CPF - employee 20%", q(wage * Decimal("0.20")),
                                     f"20% of ordinary wages {fmt(wage, cur)}"))
            out.employer.append(Line("cpf_employer", "CPF - employer 17%", q(wage * Decimal("0.17")),
                                     f"17% of ordinary wages {fmt(wage, cur)}"))
        sdl = min(max(gross * Decimal("0.0025"), Decimal("2")), Decimal("11.25"))
        out.employer.append(Line("sdl", "Skills Development Levy", q(sdl), "0.25% of pay, minimum SGD 2, maximum SGD 11.25"))
        return out

    def _gratuity(self, **kwargs):
        return no_gratuity("No statutory gratuity in Singapore; retrenchment benefits follow the contract / "
                           "tripartite guidelines.", "Retrenchment benefit")


# ---------------------------------------------------------------------------------------------- Germany
class Germany(CountryRules):
    code = "DE"
    name = "Germany"
    currency = "EUR"
    rule_set = "Germany - 2025 income-tax formula (class I), social insurance 2025"
    gratuity_label = "Severance (§1a KSchG)"
    warnings = ("Lohnsteuer is approximated with the 2025 §32a formula (tax class I, no children, no church tax); "
                "official payroll tables differ slightly.",)
    notes = ("Employee social insurance: pension 9.3%, unemployment 1.3% (ceiling EUR 8,050/month), health 8.55%, "
             "long-term care 1.8% (ceiling EUR 5,512.50/month); employer pays matching shares.",
             "Severance is only computed for redundancies (§1a KSchG: half a month per year).")
    payout_rail = "a SEPA credit transfer"
    PENSION_CEILING = Decimal("8050")
    HEALTH_CEILING = Decimal("5512.50")

    @staticmethod
    def income_tax(zve: Decimal) -> Decimal:
        zve = D(int(max(ZERO, zve)))
        if zve <= 12096:
            return ZERO
        if zve <= 17443:
            y = (zve - 12096) / 10000
            return (Decimal("932.30") * y + 1400) * y
        if zve <= 68480:
            z = (zve - 17443) / 10000
            return (Decimal("176.64") * z + 2397) * z + Decimal("1015.13")
        if zve <= 277825:
            return Decimal("0.42") * zve - Decimal("10911.92")
        return Decimal("0.45") * zve - Decimal("19256.67")

    def _taxes(self, *, gross, monthly_salary, fraction, currency, on):
        cur = currency
        out = MonthTaxes()
        pens_base = min(gross, self.PENSION_CEILING)
        health_base = min(gross, self.HEALTH_CEILING)
        items = [
            ("pension", "Pension insurance 9.3%", pens_base, Decimal("0.093")),
            ("unemployment", "Unemployment insurance 1.3%", pens_base, Decimal("0.013")),
            ("health", "Health insurance 8.55%", health_base, Decimal("0.0855")),
            ("care", "Long-term care 1.8%", health_base, Decimal("0.018")),
        ]
        social_month = ZERO
        for code, label, base, rate in items:
            amt = q(base * rate)
            social_month += amt
            out.employee.append(Line(code, label, amt, f"{pct(rate)} of {fmt(base, cur)}"))
            out.employer.append(Line(code + "_er", f"{label} - employer", amt, f"{pct(rate)} of {fmt(base, cur)}"))
        annual = monthly_salary * TWELVE
        annual_social = (min(monthly_salary, self.PENSION_CEILING) * Decimal("0.106")
                         + min(monthly_salary, self.HEALTH_CEILING) * Decimal("0.1035")) * TWELVE
        zve = max(ZERO, annual - Decimal("1230") - Decimal("36") - annual_social)
        tax = self.income_tax(zve)
        soli = ZERO
        if tax > Decimal("19950"):
            soli = min(tax * Decimal("0.055"), (tax - Decimal("19950")) * Decimal("0.119"))
        out.withholding = Line("lohnsteuer", "Income tax (Lohnsteuer) - estimate", q((tax + soli) / TWELVE * fraction),
                               f"Taxable income ~ {fmt(zve, cur)} (gross - EUR 1,266 allowances - social contributions); "
                               f"2025 tariff -> {fmt(tax, cur)}" + (f" + solidarity surcharge {fmt(soli, cur)}" if soli else "")
                               + " a year; / 12")
        return out

    def _gratuity(self, *, monthly_salary, hire_date, end_date, employment_type, reason, currency):
        cur = currency or "EUR"
        svc = service_length(hire_date, end_date)
        if reason != "redundancy":
            return no_gratuity("No statutory severance in Germany except for redundancies under §1a KSchG.",
                               self.gratuity_label)
        years = svc.rounded_years()
        amount = monthly_salary * Decimal("0.5") * years
        return GratuityResult(years > 0, q(amount), D(years), "Half a month's pay per year of service (§1a KSchG)",
                              [f"Service: {svc.label}; more than 6 months rounds up -> {years} years",
                               f"0.5 x {fmt(monthly_salary, cur)} x {years} = {fmt(amount, cur)}"],
                              ["§1a severance applies only if the employee does not challenge the dismissal in court."],
                              self.gratuity_label)


# ---------------------------------------------------------------------------------------------- France
class France(CountryRules):
    code = "FR"
    name = "France"
    currency = "EUR"
    rule_set = "France - 2025 income-tax scale (1 part), average social contribution rates"
    gratuity_label = "Indemnité légale de licenciement"
    warnings = ("Social contributions use average rates (employee ~22%, employer ~42% of gross) and ignore "
                "low-wage reductions; withholding at source uses the personalised rate in reality.",)
    notes = ("Income tax: 2025 scale for one tax share (0% to 11,497; 11% to 29,315; 30% to 83,823; 41% to 180,294; "
             "45% above) on ~78% of gross after the 10% professional-expense allowance.",
             "Legal severance on dismissal / mutual termination after 8 months: 1/4 month per year up to 10 years, "
             "1/3 month per year after.")
    payout_rail = "a SEPA credit transfer"
    SCALE = [
        (Decimal("11497"), Decimal("0")),
        (Decimal("29315"), Decimal("0.11")),
        (Decimal("83823"), Decimal("0.30")),
        (Decimal("180294"), Decimal("0.41")),
        (None, Decimal("0.45")),
    ]

    def _taxes(self, *, gross, monthly_salary, fraction, currency, on):
        cur = currency
        out = MonthTaxes()
        out.employee.append(Line("social_employee", "Social contributions - employee (~22%)", q(gross * Decimal("0.22")),
                                 f"Average employee rate 22% of {fmt(gross, cur)}"))
        out.employer.append(Line("social_employer", "Social contributions - employer (~42%)", q(gross * Decimal("0.42")),
                                 f"Average employer rate 42% of {fmt(gross, cur)}"))
        annual = monthly_salary * TWELVE
        taxable = annual * Decimal("0.78") * Decimal("0.90")
        tax = progressive(taxable, self.SCALE)
        out.withholding = Line("pas", "Income tax at source - estimate", q(tax / TWELVE * fraction),
                               f"Net taxable ~ {fmt(taxable, cur)} (78% of gross less 10% allowance); 2025 scale -> "
                               f"{fmt(tax, cur)} a year; / 12")
        return out

    def _gratuity(self, *, monthly_salary, hire_date, end_date, employment_type, reason, currency):
        cur = currency or "EUR"
        svc = service_length(hire_date, end_date)
        if reason not in ("dismissal", "redundancy", "mutual_agreement"):
            why = "serious misconduct (faute grave)" if reason == "misconduct" else "this reason"
            return no_gratuity(f"The legal severance indemnity is not due for {why}.", self.gratuity_label)
        if svc.years * 12 + svc.months < 8:
            return no_gratuity("Not eligible: less than 8 months of uninterrupted service.", self.gratuity_label)
        years = svc.exact_years
        first = min(years, Decimal(10)) * monthly_salary / 4
        rest = max(ZERO, years - 10) * monthly_salary / 3
        steps = [f"Service: {svc.label} ({years:.4f} years)",
                 f"1/4 month x {min(years, Decimal(10)):.4f} years = {fmt(first, cur)}"]
        if rest:
            steps.append(f"1/3 month x {years - 10:.4f} years = {fmt(rest, cur)}")
        return GratuityResult(True, q(first + rest), years.quantize(Decimal("0.0001")),
                              "Legal severance indemnity (Code du travail R1234-2)", steps, [], self.gratuity_label)


# ---------------------------------------------------------------------------------------------- New Zealand
class NewZealand(CountryRules):
    code = "NZ"
    name = "New Zealand"
    currency = "NZD"
    rule_set = "New Zealand - PAYE from 31 July 2024, ACC levy 2025-26, KiwiSaver"
    gratuity_label = "Redundancy compensation"
    warnings = ("KiwiSaver is assumed (default rate; employees may choose another rate or have opted out).",)
    notes = ("PAYE: 10.5% to 15,600; 17.5% to 53,500; 30% to 78,100; 33% to 180,000; 39% above; ACC earners' levy 1.67%.",
             "KiwiSaver default 3.5% employee + 3.5% compulsory employer contribution from 1 April 2026 (3% before).")
    payout_rail = "a domestic bank transfer"
    BRACKETS = [
        (Decimal("15600"), Decimal("0.105")),
        (Decimal("53500"), Decimal("0.175")),
        (Decimal("78100"), Decimal("0.30")),
        (Decimal("180000"), Decimal("0.33")),
        (None, Decimal("0.39")),
    ]

    def _taxes(self, *, gross, monthly_salary, fraction, currency, on):
        cur = currency
        out = MonthTaxes()
        annual = monthly_salary * TWELVE
        tax = progressive(annual, self.BRACKETS)
        out.withholding = Line("paye", "PAYE - estimate", q(tax / TWELVE * fraction),
                               f"PAYE brackets on {fmt(annual, cur)} = {fmt(tax, cur)} a year; / 12")
        acc_base = min(gross, Decimal("152790") / TWELVE)
        out.employee.append(Line("acc", "ACC earners' levy 1.67%", q(acc_base * Decimal("0.0167")),
                                 f"1.67% of {fmt(acc_base, cur)}"))
        rate = Decimal("0.035") if on >= date(2026, 4, 1) else Decimal("0.03")
        out.employee.append(Line("kiwisaver", f"KiwiSaver - employee {pct(rate)}", q(gross * rate), f"{pct(rate)} of {fmt(gross, cur)}"))
        out.employer.append(Line("kiwisaver_er", f"KiwiSaver - employer {pct(rate)}", q(gross * rate),
                                 f"{pct(rate)} of {fmt(gross, cur)} (employer superannuation contribution tax not included)"))
        return out

    def _gratuity(self, **kwargs):
        return no_gratuity("No statutory gratuity or redundancy pay in New Zealand - follow the employment agreement.",
                           self.gratuity_label)


# ---------------------------------------------------------------------------------------------- South Africa
class SouthAfrica(CountryRules):
    code = "ZA"
    name = "South Africa"
    currency = "ZAR"
    rule_set = "South Africa - PAYE 2025/26, UIF, SDL, BCEA s41 severance"
    gratuity_label = "Severance pay (BCEA s41)"
    warnings = ("PAYE is an annualised estimate for an employee under 65 (primary rebate only, no medical credits).",)
    notes = ("UIF: 1% employee + 1% employer on pay up to ZAR 17,712 a month; SDL 1% (employers with an annual payroll "
             "above ZAR 500,000).",
             "Severance only for dismissals based on operational requirements: one week's pay per completed year.")
    payout_rail = "an EFT bank transfer"
    BRACKETS = [
        (Decimal("237100"), Decimal("0.18")),
        (Decimal("370500"), Decimal("0.26")),
        (Decimal("512800"), Decimal("0.31")),
        (Decimal("673000"), Decimal("0.36")),
        (Decimal("857900"), Decimal("0.39")),
        (Decimal("1817000"), Decimal("0.41")),
        (None, Decimal("0.45")),
    ]
    PRIMARY_REBATE = Decimal("17235")
    UIF_CEILING = Decimal("17712")

    def _taxes(self, *, gross, monthly_salary, fraction, currency, on):
        cur = currency
        out = MonthTaxes()
        annual = monthly_salary * TWELVE
        tax = max(ZERO, progressive(annual, self.BRACKETS) - self.PRIMARY_REBATE)
        out.withholding = Line("paye", "PAYE - estimate", q(tax / TWELVE * fraction),
                               f"2025/26 tables on {fmt(annual, cur)} minus primary rebate {fmt(self.PRIMARY_REBATE, cur)} "
                               f"= {fmt(tax, cur)} a year; / 12")
        uif_base = min(gross, self.UIF_CEILING)
        out.employee.append(Line("uif", "UIF - employee 1%", q(uif_base / 100), f"1% of {fmt(uif_base, cur)}"))
        out.employer.append(Line("uif_er", "UIF - employer 1%", q(uif_base / 100), f"1% of {fmt(uif_base, cur)}"))
        out.employer.append(Line("sdl", "Skills Development Levy 1%", q(gross / 100), f"1% of {fmt(gross, cur)}"))
        return out

    def _gratuity(self, *, monthly_salary, hire_date, end_date, employment_type, reason, currency):
        cur = currency or "ZAR"
        if reason != "redundancy":
            return no_gratuity("Statutory severance (BCEA s41) applies only to dismissals for operational requirements.",
                               self.gratuity_label)
        svc = service_length(hire_date, end_date)
        week = monthly_salary * TWELVE / Decimal(52)
        amount = week * svc.years
        return GratuityResult(svc.years > 0, q(amount), D(svc.years), "One week's pay per completed year of service",
                              [f"Service: {svc.label} -> {svc.years} completed years",
                               f"Weekly pay {fmt(week, cur)} x {svc.years} = {fmt(amount, cur)}"], [], self.gratuity_label)


# ---------------------------------------------------------------------------------------------- registry
DEFAULT_RULES = CountryRules()

RULES: dict[str, CountryRules] = {
    r.code: r
    for r in (
        India(), UAE(), SaudiArabia(), Qatar(), Kuwait(), Oman(), Bahrain(), UnitedStates(), UnitedKingdom(),
        Canada(), Australia(), Singapore(), Germany(), France(), NewZealand(), SouthAfrica(),
    )
}

COUNTRY_NAMES: dict[str, str] = {code: r.name for code, r in RULES.items()}


def rules_for(country_code: str | None) -> CountryRules:
    """Rules for an ISO country code; unknown countries get the DEFAULT (nothing computed + accountant warning)."""
    return RULES.get((country_code or "").upper(), DEFAULT_RULES)


def supported_countries() -> list[tuple[str, str]]:
    return sorted(((c, r.name) for c, r in RULES.items()), key=lambda t: t[1])
