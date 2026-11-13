"""Payroll: salary statements by days worked, country estimates, payouts, proofs, final settlement, termination.

Public API (used by the finance router and other work packages)::

    compute_statement(...)                         pure -> StatementDraft (breakdown + rules + explanation)
    generate_statements(db, gym, year, month)      idempotent; DUE statements, due on the 7th of next month
    outstanding_dues(db, employee)                 -> list[SalaryStatement] (+ ``.accrued`` preview)  [used by WP-C1]
    accrued_preview(db, employee, as_of=None)      salary accrued since the last statement
    create_final_settlement(db, employee, termination_date, reason)
    initiate_payout / record_proof / verify_payment_proof / confirm_despite_mismatch
    termination_blockers(db, employee) / terminate_employee(db, employee, actor)

Salary is counted by DAYS: for every calendar month touched by a statement,
``pay = monthly_salary x paid_days / days_in_that_month`` where paid days are the days inside
[hire_date, termination_date] (minus unpaid leave). Every amount is Decimal rounded ROUND_HALF_UP to 0.01.
All tax / contribution / gratuity figures are labelled ESTIMATES (see ``payroll_rules``).
"""

from __future__ import annotations

import asyncio
import calendar
import inspect
import logging
import re
import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.ai import payroll as ai_payroll
from app.config import get_settings
from app.core.utils import mask_middle, money, utcnow
from app.db import session_scope
from app.models import Employee, Gym, Job, SalaryStatement, TrainerClient, TrainerShift, Transaction, User
from app.models.billing import proof_sha256
from app.models.enums import (
    EmploymentStatus,
    JobStatus,
    NotificationCategory,
    Severity,
    StatementStatus,
    TxCategory,
    TxDirection,
    TxStatus,
)
from app.services import storage
from app.services.audit import audit
from app.services.batching import Chunks, slices
from app.services.jobs import current_run, enqueue, exit_policy_for, job_handler, periodic, set_result_event
from app.services.notify import Note, email_address, notify, notify_bulk
from app.services.payroll_rules import (
    COUNTRY_NAMES,
    TERMINATION_REASONS,
    ZERO,
    GratuityResult,
    q,
    rules_for,
)

log = logging.getLogger("ironpulse.payroll")

UNPAID_STATUSES = (StatementStatus.DRAFT, StatementStatus.DUE, StatementStatus.AWAITING_PROOF, StatementStatus.PROOF_REVIEW)
PAYABLE_STATUSES = (StatementStatus.DUE, StatementStatus.AWAITING_PROOF)
REMINDER_LEAD_DAYS = 3
MAX_FUTURE_TERMINATION_DAYS = 90
AMOUNT_TOLERANCE = Decimal("0.01")  # +-1 %
PAYOUT_METHODS_ONLINE = {"bank_transfer", "bank", "upi", "imps", "neft"}
EXPLAIN_JOB = "payroll_explain"
GENERATE_JOB = "payroll_generate"  # "run payroll" for all of an owner's gyms, in the background (one gym at a time)
# batching (app/services/batching.py): bounded statements, one short transaction per gym
STATEMENT_INSERT_ROWS = 500  # salary statements per multi-row INSERT (~20 bind parameters each)
EXPLAIN_INSERT_ROWS = 500  # explanation jobs per multi-row INSERT
ID_LIST_CHUNK = 1000  # employee ids per IN list (asyncpg caps a statement at 32,767 bind parameters)
COMPUTE_YIELD_EVERY = 100  # statements computed between two event-loop yields (a 2,000-person gym never stalls it)
# AI explanations queued automatically per payroll run - per gym in the monthly run, per click of "run payroll" -
# the other statements show the deterministic explanation until the owner clicks Explain (approved load limit)
EXPLAIN_PER_RUN = 50
GEN_GYM_BATCH = 25  # gyms picked per chunk of the monthly generation (each gym then commits on its own)
GEN_BUDGET_S = 300.0  # per hourly run on day 1; the next run continues
SALARY_REMINDER_BATCH = 500  # due statements per chunk of the hourly reminders
SALARY_REMINDER_BUDGET_S = 300.0


class PayrollError(ValueError):
    """Validation / business-rule error; ``field`` names the form field (or None for the whole form)."""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class TerminationBlocked(PayrollError):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


# ============================================================================================ date helpers
def month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def previous_month(d: date) -> tuple[int, int]:
    first = d.replace(day=1) - timedelta(days=1)
    return first.year, first.month


def due_date_for(period_end: date) -> date:
    """Monthly salary is due on the 7th of the month after the period."""
    nxt = period_end.replace(day=1) + timedelta(days=32)
    return date(nxt.year, nxt.month, 7)


def period_label(start: date, end: date) -> str:
    if start.year == end.year and start.month == end.month:
        if start.day == 1 and end.day == calendar.monthrange(end.year, end.month)[1]:
            return f"{start:%B %Y}"
        return f"{start.day}-{end.day} {start:%b %Y}"
    return f"{start:%d %b %Y} - {end:%d %b %Y}"


def _merge(ranges: Iterable[tuple[date, date]]) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    for s, e in sorted(r for r in ranges if r[1] >= r[0]):
        if out and s <= out[-1][1] + timedelta(days=1):
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def uncovered_segments(start: date, end: date, covered: Iterable[tuple[date, date]]) -> list[tuple[date, date]]:
    """Parts of [start, end] not inside any ``covered`` range (all inclusive)."""
    if end < start:
        return []
    segs: list[tuple[date, date]] = []
    cursor = start
    for cs, ce in _merge(covered):
        if ce < cursor:
            continue
        if cs > end:
            break
        if cs > cursor:
            segs.append((cursor, min(end, cs - timedelta(days=1))))
        cursor = max(cursor, ce + timedelta(days=1))
        if cursor > end:
            break
    if cursor <= end:
        segs.append((cursor, end))
    return segs


def _overlap_days(a0: date, a1: date, b0: date, b1: date) -> int:
    s, e = max(a0, b0), min(a1, b1)
    return (e - s).days + 1 if e >= s else 0


# ============================================================================================ pure engine
@dataclass(slots=True)
class MonthLine:
    year: int
    month: int
    days_in_month: int
    employed_days: int
    leave_days: int
    paid_days: int
    amount: Decimal

    @property
    def label(self) -> str:
        return f"{calendar.month_abbr[self.month]} {self.year}"

    def to_json(self) -> dict[str, Any]:
        return {"month": f"{self.year:04d}-{self.month:02d}", "label": self.label, "days_in_month": self.days_in_month,
                "employed_days": self.employed_days, "unpaid_leave_days": self.leave_days, "paid_days": self.paid_days,
                "amount": str(self.amount)}


@dataclass(slots=True)
class StatementDraft:
    country_code: str
    currency: str
    monthly_salary: Decimal
    period_start: date
    period_end: date
    days_in_period: int
    days_worked: int
    unpaid_leave_days: int
    gross_pay: Decimal
    gratuity: Decimal
    tax_withholding: Decimal
    employee_deductions: dict[str, dict[str, str]]
    employer_contributions: dict[str, dict[str, str]]
    net_pay: Decimal
    rules: dict[str, Any]
    is_final_settlement: bool = False
    months: list[MonthLine] = field(default_factory=list)
    gratuity_result: GratuityResult | None = None
    status: StatementStatus = StatementStatus.DRAFT
    ai_explanation: str | None = None

    @property
    def total_employee_deductions(self) -> Decimal:
        return sum((Decimal(v["amount"]) for v in self.employee_deductions.values()), ZERO)

    @property
    def total_employer_contributions(self) -> Decimal:
        return sum((Decimal(v["amount"]) for v in self.employer_contributions.values()), ZERO)

    def model_kwargs(self) -> dict[str, Any]:
        return {
            "country_code": self.country_code, "currency": self.currency, "monthly_salary": self.monthly_salary,
            "period_start": self.period_start, "period_end": self.period_end, "days_in_period": self.days_in_period,
            "days_worked": self.days_worked, "unpaid_leave_days": self.unpaid_leave_days, "gross_pay": self.gross_pay,
            "gratuity": self.gratuity, "tax_withholding": self.tax_withholding,
            "employee_deductions": self.employee_deductions, "employer_contributions": self.employer_contributions,
            "net_pay": self.net_pay, "rules": self.rules, "is_final_settlement": self.is_final_settlement,
        }


def _acc(target: dict[str, dict[str, Any]], line: Any, months: int) -> None:
    item = target.get(line.code)
    if item is None:
        target[line.code] = {"label": line.label, "amount": q(line.amount), "explanation": line.explanation, "n": 1}
    else:
        item["amount"] = q(item["amount"] + line.amount)
        item["n"] += 1
        item["explanation"] = line.explanation
    if months > 1:
        target[line.code]["months"] = months


def _finish(target: dict[str, dict[str, Any]]) -> dict[str, dict[str, str]]:
    out = {}
    for code, item in target.items():
        expl = item["explanation"]
        if item.get("n", 1) > 1:
            expl = f"{expl} (summed over {item['n']} months)"
        out[code] = {"label": item["label"], "amount": str(q(item["amount"])), "explanation": expl}
    return out


def compute_statement(
    *,
    monthly_salary: Decimal | str | int,
    currency: str,
    country_code: str,
    hire_date: date,
    period_start: date,
    period_end: date,
    termination_date: date | None = None,
    unpaid_leave_days: int = 0,
    employment_type: str = "full_time",
    is_final_settlement: bool = False,
    termination_reason: str | None = None,
    gym_country: str | None = None,
    segments: list[tuple[date, date]] | None = None,
) -> StatementDraft:
    """Pure salary computation (no I/O). Raises :class:`PayrollError` for invalid input."""
    try:
        salary = q(monthly_salary)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PayrollError("Monthly salary is not a valid amount.", "monthly_salary") from exc
    if salary < 0:
        raise PayrollError("Monthly salary cannot be negative.", "monthly_salary")
    if period_end < period_start:
        raise PayrollError("The period ends before it starts.", "period")
    unpaid_leave_days = int(unpaid_leave_days or 0)
    if unpaid_leave_days < 0:
        raise PayrollError("Unpaid leave days cannot be negative.", "unpaid_leave_days")
    currency = (currency or "").upper()
    country_code = (country_code or "").upper()
    rules = rules_for(country_code)

    emp_end_cap = min(period_end, termination_date) if termination_date else period_end
    raw = segments if segments is not None else [(period_start, period_end)]
    segs = _merge((max(s, hire_date, period_start), min(e, emp_end_cap)) for s, e in raw)
    employed_total = sum((e - s).days + 1 for s, e in segs)
    if unpaid_leave_days > employed_total:
        raise PayrollError(f"Unpaid leave ({unpaid_leave_days} days) exceeds the {employed_total} days employed in this period.",
                           "unpaid_leave_days")

    # ---- per-month proration
    months: list[MonthLine] = []
    remaining_leave = unpaid_leave_days
    if segs:
        cur = segs[0][0].replace(day=1)
        last = segs[-1][1]
        while cur <= last:
            m_start, m_end = month_bounds(cur.year, cur.month)
            employed = sum(_overlap_days(s, e, m_start, m_end) for s, e in segs)
            if employed:
                leave = min(remaining_leave, employed)
                remaining_leave -= leave
                paid = employed - leave
                dim = (m_end - m_start).days + 1
                months.append(MonthLine(cur.year, cur.month, dim, employed, leave, paid, q(salary * paid / dim)))
            cur = m_end + timedelta(days=1)
    gross = q(sum((m.amount for m in months), ZERO))

    # ---- country estimates per month
    emp_lines: dict[str, dict[str, Any]] = {}
    er_lines: dict[str, dict[str, Any]] = {}
    withholding = ZERO
    withholding_expl: list[str] = []
    withholding_label = "Income tax withholding - estimate"
    notes: list[str] = list(rules.notes)
    warnings: list[str] = list(rules.warnings)
    paid_months = [m for m in months if m.amount > 0]
    for m in paid_months:
        fraction = Decimal(m.paid_days) / Decimal(m.days_in_month)
        mt = rules.taxes(gross=m.amount, monthly_salary=salary, fraction=fraction, currency=currency,
                         on=date(m.year, m.month, 1))
        for line in mt.employee:
            _acc(emp_lines, line, len(paid_months))
        for line in mt.employer:
            _acc(er_lines, line, len(paid_months))
        if mt.withholding is not None:
            withholding += mt.withholding.amount
            withholding_expl.append(f"{m.label}: {mt.withholding.explanation}")
            withholding_label = mt.withholding.label
        for n in mt.notes:
            if n not in notes:
                notes.append(n)
        for w in mt.warnings:
            if w not in warnings:
                warnings.append(w)
    withholding = q(withholding)
    employee_deductions = _finish(emp_lines)
    employer_contributions = _finish(er_lines)

    # ---- end-of-service benefit (final settlement only)
    grat: GratuityResult | None = None
    gratuity = ZERO
    end_for_service = termination_date or period_end
    if is_final_settlement:
        grat = rules.gratuity(monthly_salary=salary, hire_date=hire_date, end_date=end_for_service,
                              employment_type=employment_type, reason=termination_reason, currency=currency)
        gratuity = q(grat.amount)
        for w in grat.warnings:
            if w not in warnings:
                warnings.append(w)

    total_ded = sum((Decimal(v["amount"]) for v in employee_deductions.values()), ZERO)
    net = q(gross - total_ded - withholding + gratuity)
    if net < 0:
        warnings.append("Deductions exceed pay for this period; net pay was set to zero - review manually.")
        net = ZERO

    international = bool(gym_country) and country_code != (gym_country or "").upper()
    if international:
        warnings.append(
            f"The employee is based in {COUNTRY_NAMES.get(country_code, country_code)} while the gym is in "
            f"{COUNTRY_NAMES.get((gym_country or '').upper(), gym_country)}: estimates use the employee's country. "
            "Cross-border work can change tax residency, social security and payout obligations."
        )

    days_in_period = sum((e - s).days + 1 for s, e in segs) if segs else 0
    draft_period_start = segs[0][0] if segs else (termination_date or period_end)
    draft_period_end = segs[-1][1] if segs else (termination_date or period_end)
    rules_json: dict[str, Any] = {
        "version": 1,
        "estimate": True,
        "country": country_code,
        "country_name": rules.name if rules.code != "DEFAULT" else COUNTRY_NAMES.get(country_code, country_code or "Unknown"),
        "rule_set": rules.rule_set,
        "rules_found": rules.code != "DEFAULT",
        "currency": currency,
        "employment_type": employment_type,
        "hire_date": hire_date.isoformat(),
        "segments": [[s.isoformat(), e.isoformat()] for s, e in segs],
        "proration": [m.to_json() for m in months],
        "withholding": ({"label": withholding_label, "amount": str(withholding),
                         "explanation": " | ".join(withholding_expl)}
                        if withholding_expl else None),
        "gratuity": grat.to_json() if grat else None,
        "notes": notes,
        "warnings": warnings,
        "international": international,
        "gym_country": (gym_country or "").upper() or None,
    }
    draft = StatementDraft(
        country_code=country_code, currency=currency, monthly_salary=salary, period_start=draft_period_start,
        period_end=draft_period_end, days_in_period=days_in_period,
        days_worked=sum(m.paid_days for m in months), unpaid_leave_days=unpaid_leave_days, gross_pay=gross,
        gratuity=gratuity, tax_withholding=withholding, employee_deductions=employee_deductions,
        employer_contributions=employer_contributions, net_pay=net, rules=rules_json,
        is_final_settlement=is_final_settlement, months=months, gratuity_result=grat,
    )
    draft.rules["explanation"] = deterministic_explanation(draft).model_dump()
    return draft


def deterministic_explanation(st: Any) -> ai_payroll.PayrollExplanation:
    """Plain-language working built only from the statement's own numbers (used when AI is unavailable)."""
    cur = st.currency
    rules = st.rules or {}
    m = lambda v: money(Decimal(str(v)), cur)  # noqa: E731
    total_ded = sum((Decimal(v["amount"]) for v in (st.employee_deductions or {}).values()), ZERO)
    summary = (f"Net pay {m(st.net_pay)} for {period_label(st.period_start, st.period_end)}: gross {m(st.gross_pay)} for "
               f"{st.days_worked} paid day{'s' if st.days_worked != 1 else ''}")
    if total_ded or st.tax_withholding:
        summary += f", minus {m(total_ded)} statutory deductions and {m(st.tax_withholding)} estimated income tax"
    if st.gratuity:
        summary += f", plus {m(st.gratuity)} {((rules.get('gratuity') or {}).get('label') or 'gratuity').lower()}"
    summary += "."
    steps: list[str] = []
    for p in rules.get("proration", []):
        leave = f" ({p['unpaid_leave_days']} unpaid leave)" if p.get("unpaid_leave_days") else ""
        steps.append(f"{p['label']}: {m(st.monthly_salary)} x {p['paid_days']}/{p['days_in_month']} days{leave} = {m(p['amount'])}")
    if not rules.get("proration"):
        steps.append("No salary days in this period (already paid) - only the end-of-service benefit applies.")
    for v in (st.employee_deductions or {}).values():
        steps.append(f"{v['label']}: {m(v['amount'])} - {v['explanation']}")
    if rules.get("withholding"):
        steps.append(f"{rules['withholding']['label']}: {m(st.tax_withholding)} (annualised estimate)")
    grat = rules.get("gratuity")
    if grat:
        steps.append(f"{grat.get('label', 'Gratuity')}: {m(grat['amount'])} - {grat['explanation']}")
    steps.append(f"Net = gross - deductions - withholding + gratuity = {m(st.net_pay)}")
    if st.employer_contributions:
        er_total = sum((Decimal(v['amount']) for v in st.employer_contributions.values()), ZERO)
        steps.append(f"On top of this you pay {m(er_total)} in employer contributions (not deducted from the employee).")
    notes = list(rules.get("notes", []))[:6]
    warnings = ["All tax, contribution and gratuity figures are estimates - confirm with a qualified accountant."]
    warnings += [w for w in rules.get("warnings", []) if w not in warnings][:5]
    return ai_payroll.PayrollExplanation(summary=summary, steps=steps[:12], country_notes=notes, warnings=warnings)


def explanation_for(st: Any) -> tuple[ai_payroll.PayrollExplanation, str | None]:
    """(explanation, provider) - the stored AI text when present, else the deterministic one."""
    ai_text = ai_payroll.loads(getattr(st, "ai_explanation", None))
    if ai_text is not None and (ai_text.summary or ai_text.steps):
        return ai_text, getattr(st, "ai_provider", None)
    stored = (st.rules or {}).get("explanation")
    if stored:
        try:
            return ai_payroll.PayrollExplanation.model_validate(stored), None
        except ValueError:
            pass
    return deterministic_explanation(st), None


# ============================================================================================ coverage
async def _covered_ranges(db: AsyncSession, employee_ids: list[int], *, start: date | None = None,
                          end: date | None = None) -> dict[int, list[tuple[date, date]]]:
    out: dict[int, list[tuple[date, date]]] = {}
    for part in slices(employee_ids, ID_LIST_CHUNK):  # bounded IN lists, however many staff a gym has
        stmt = select(SalaryStatement.employee_id, SalaryStatement.period_start, SalaryStatement.period_end).where(
            SalaryStatement.employee_id.in_(part), SalaryStatement.status != StatementStatus.VOID
        )
        if start is not None:
            stmt = stmt.where(SalaryStatement.period_end >= start)
        if end is not None:
            stmt = stmt.where(SalaryStatement.period_start <= end)
        for emp_id, s, e in (await db.execute(stmt)).all():
            out.setdefault(emp_id, []).append((s, e))
    return out


async def _settlement_ends(db: AsyncSession, employee_ids: list[int]) -> dict[int, date]:
    """Termination date of each employee's (non-void) final settlement - salary never accrues after it."""
    out: dict[int, date] = {}
    for part in slices(employee_ids, ID_LIST_CHUNK):
        rows = await db.execute(
            select(SalaryStatement.employee_id, func.max(SalaryStatement.period_end))
            .where(SalaryStatement.employee_id.in_(part), SalaryStatement.is_final_settlement.is_(True),
                   SalaryStatement.status != StatementStatus.VOID)
            .group_by(SalaryStatement.employee_id)
        )
        out.update({emp_id: end for emp_id, end in rows.all()})
    return out


def _employment_end(emp: Employee, settlement_end: date | None) -> date | None:
    ends = [d for d in (emp.termination_date, settlement_end) if d is not None]
    return min(ends) if ends else None


def tracking_start(emp: Employee, covered: list[tuple[date, date]]) -> date:
    """First day IronPulse is responsible for this person's salary.

    Salaries before the staff member was added to IronPulse were settled outside the app, so accrual (dues
    preview, final settlement) starts at the earlier of the first statement and the first day of the month the
    record was created - never before the hire date. Gratuity still uses the full service from the hire date."""
    created = emp.created_at.date() if getattr(emp, "created_at", None) else emp.hire_date
    start = max(emp.hire_date, created.replace(day=1))
    if covered:
        start = min(start, min(s for s, _ in covered))
    return max(start, emp.hire_date)


# ============================================================================================ generation
@dataclass(slots=True)
class GenerationResult:
    year: int
    month: int
    created_ids: list[int] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{calendar.month_name[self.month]} {self.year}"


async def generate_statements(db: AsyncSession, gym: Gym, year: int, month: int, *,
                              queue_explanations: bool = True) -> GenerationResult:
    """Create DUE salary statements for ``gym`` and the given month (idempotent; the caller commits).

    Covers ACTIVE employees and those terminated inside the month; days already covered by another statement
    (e.g. a final settlement) are never paid twice; a unique constraint guards concurrent runs. Any staff size: id
    lists and INSERTs go in bounded chunks and the computation yields to the event loop as it goes.
    ``queue_explanations``: queue the AI explanation of the first EXPLAIN_PER_RUN new statements."""
    m_start, m_end = month_bounds(year, month)
    result = GenerationResult(year, month)
    emps = (
        await db.execute(
            select(Employee)
            .where(
                Employee.gym_id == gym.id,
                Employee.deleted_at.is_(None),
                Employee.hire_date <= m_end,
                or_(
                    Employee.status == EmploymentStatus.ACTIVE,
                    and_(Employee.status == EmploymentStatus.TERMINATED, Employee.termination_date >= m_start,
                         Employee.termination_date <= m_end),
                ),
            )
            .order_by(Employee.full_name, Employee.id)
        )
    ).scalars().all()
    ids = [e.id for e in emps]
    covered = await _covered_ranges(db, ids, start=m_start, end=m_end)
    settled = await _settlement_ends(db, ids)
    due = due_date_for(m_end)
    rows: list[dict[str, Any]] = []
    for n, emp in enumerate(emps, 1):
        if n % COMPUTE_YIELD_EVERY == 0:
            await asyncio.sleep(0)  # pure-Python Decimal work: let other requests run between batches
        end_cap = _employment_end(emp, settled.get(emp.id))
        start = max(m_start, emp.hire_date)
        end = min(m_end, end_cap) if end_cap else m_end
        segs = uncovered_segments(start, end, covered.get(emp.id, []))
        if not segs:
            result.skipped.append((emp.display_name, "already covered" if end >= start else "not employed this month"))
            continue
        draft = compute_statement(
            monthly_salary=emp.monthly_salary, currency=emp.currency, country_code=emp.country_code,
            hire_date=emp.hire_date, period_start=segs[0][0], period_end=segs[-1][1], termination_date=end_cap,
            employment_type=emp.employment_type.value, gym_country=gym.country_code, segments=segs,
        )
        if draft.gross_pay <= 0:
            result.skipped.append((emp.display_name, "no paid days"))
            continue
        rows.append({**draft.model_kwargs(), "employee_id": emp.id, "gym_id": gym.id, "status": StatementStatus.DUE,
                     "due_date": due})
    # multi-row INSERTs of STATEMENT_INSERT_ROWS (~20 bind parameters per row; asyncpg caps a statement at 32,767)
    for part in slices(rows, STATEMENT_INSERT_ROWS):
        stmt = (
            pg_insert(SalaryStatement)
            .values(list(part))
            .on_conflict_do_nothing(index_elements=["employee_id", "period_start", "period_end", "is_final_settlement"])
            .returning(SalaryStatement.id)
        )
        result.created_ids += list((await db.execute(stmt)).scalars().all())
    if queue_explanations and result.created_ids:
        await queue_explanations_for(db, result.created_ids[:EXPLAIN_PER_RUN])
    return result


async def queue_explanations_for(db: AsyncSession, statement_ids: Sequence[int]) -> None:
    """``enqueue(db, "payroll_explain", {"statement_id": id}, dedupe_key=f"payroll_explain:{id}", ...)`` for many NEW
    statements at once: one ``INSERT ... ON CONFLICT (dedupe_key) DO NOTHING`` per EXPLAIN_INSERT_ROWS jobs instead of
    three round trips per statement. A brand-new statement id has no job yet - the only case in which ``enqueue``
    inserts - so the rows are exactly what ``enqueue`` would write (system work: no requester). Callers pass at most
    EXPLAIN_PER_RUN ids of one run."""
    if not statement_ids:
        return
    run_after = utcnow() + timedelta(seconds=5)
    policy = exit_policy_for(EXPLAIN_JOB)
    for part in slices(list(statement_ids), EXPLAIN_INSERT_ROWS):
        await db.execute(
            pg_insert(Job)
            .values([{"kind": EXPLAIN_JOB, "payload": {"statement_id": sid}, "status": JobStatus.QUEUED, "attempts": 0,
                      "max_attempts": 2, "run_after": run_after, "dedupe_key": f"{EXPLAIN_JOB}:{sid}", "user_id": None,
                      "exit_policy": policy} for sid in part])
            .on_conflict_do_nothing(index_elements=[Job.dedupe_key])
        )


# ============================================================================================ dues / accrual
@dataclass(slots=True)
class AccruedPreview:
    period_start: date
    period_end: date
    days: int
    gross: Decimal
    net: Decimal
    currency: str
    draft: StatementDraft


class OutstandingDues(list):
    """``list[SalaryStatement]`` of unpaid statements with an ``accrued`` preview (salary not yet on a statement)."""

    accrued: AccruedPreview | None = None

    def totals(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for st in self:
            out[st.currency] = q(out.get(st.currency, ZERO) + st.net_pay)
        return out

    @property
    def is_clear(self) -> bool:
        return not self and self.accrued is None


async def accrued_preview(db: AsyncSession, employee: Employee, *, as_of: date | None = None,
                          gym_country: str | None = None) -> AccruedPreview | None:
    as_of = as_of or date.today()
    if employee.status in (EmploymentStatus.INVITED, EmploymentStatus.PENDING_VERIFICATION):
        return None
    covered = (await _covered_ranges(db, [employee.id])).get(employee.id, [])
    settled = (await _settlement_ends(db, [employee.id])).get(employee.id)
    end_cap = _employment_end(employee, settled)
    end = min(as_of, end_cap) if end_cap else as_of
    segs = uncovered_segments(tracking_start(employee, covered), end, covered)
    if not segs:
        return None
    draft = compute_statement(
        monthly_salary=employee.monthly_salary, currency=employee.currency, country_code=employee.country_code,
        hire_date=employee.hire_date, period_start=segs[0][0], period_end=segs[-1][1], termination_date=end_cap,
        employment_type=employee.employment_type.value, gym_country=gym_country, segments=segs,
    )
    if draft.gross_pay <= 0:
        return None
    return AccruedPreview(draft.period_start, draft.period_end, draft.days_worked, draft.gross_pay, draft.net_pay,
                          draft.currency, draft)


async def accrued_previews(db: AsyncSession, employees: Sequence[Employee], *, as_of: date | None = None,
                           gym_countries: dict[int, str | None] | None = None) -> dict[int, AccruedPreview]:
    """``accrued_preview`` of many employees at once - salary earned but not yet on a statement, per employee id (those
    with none are left out) - in a constant number of queries (two per ID_LIST_CHUNK employees), never two per person:
    the governance impact previews (app/services/governance.py) price whole gyms. ``gym_countries``: gym id ->
    country code (the employer's country rules)."""
    as_of = as_of or date.today()
    working = [e for e in employees
               if e.status not in (EmploymentStatus.INVITED, EmploymentStatus.PENDING_VERIFICATION)]
    if not working:
        return {}
    ids = [e.id for e in working]
    covered_all = await _covered_ranges(db, ids)
    settled_all = await _settlement_ends(db, ids)
    out: dict[int, AccruedPreview] = {}
    for n, employee in enumerate(working, 1):
        if n % COMPUTE_YIELD_EVERY == 0:
            await asyncio.sleep(0)  # pure-Python Decimal work: let other requests run between batches
        covered = covered_all.get(employee.id, [])
        end_cap = _employment_end(employee, settled_all.get(employee.id))
        end = min(as_of, end_cap) if end_cap else as_of
        segs = uncovered_segments(tracking_start(employee, covered), end, covered)
        if not segs:
            continue
        draft = compute_statement(
            monthly_salary=employee.monthly_salary, currency=employee.currency, country_code=employee.country_code,
            hire_date=employee.hire_date, period_start=segs[0][0], period_end=segs[-1][1], termination_date=end_cap,
            employment_type=employee.employment_type.value,
            gym_country=(gym_countries or {}).get(employee.gym_id), segments=segs,
        )
        if draft.gross_pay > 0:
            out[employee.id] = AccruedPreview(draft.period_start, draft.period_end, draft.days_worked, draft.gross_pay,
                                              draft.net_pay, draft.currency, draft)
    return out


async def outstanding_dues(db: AsyncSession, employee: Employee, *, as_of: date | None = None) -> OutstandingDues:
    """Unpaid statements of ``employee`` (oldest first) + ``.accrued`` = salary earned since the last statement."""
    rows = (
        await db.execute(
            select(SalaryStatement)
            .where(SalaryStatement.employee_id == employee.id, SalaryStatement.status.in_(UNPAID_STATUSES))
            .order_by(SalaryStatement.period_start, SalaryStatement.id)
        )
    ).scalars().all()
    dues = OutstandingDues(rows)
    dues.accrued = await accrued_preview(db, employee, as_of=as_of)
    return dues


# ============================================================================================ final settlement
def _validate_settlement_input(employee: Employee, termination_date: date | None, reason: str | None,
                               today: date) -> tuple[date, str]:
    if termination_date is None:
        raise PayrollError("Choose the last working day.", "termination_date")
    if termination_date < employee.hire_date:
        raise PayrollError(f"The last working day cannot be before the hire date ({employee.hire_date:%d %b %Y}).",
                           "termination_date")
    if termination_date > today + timedelta(days=MAX_FUTURE_TERMINATION_DAYS):
        raise PayrollError(f"The last working day can be at most {MAX_FUTURE_TERMINATION_DAYS} days ahead.", "termination_date")
    if reason not in TERMINATION_REASONS:
        raise PayrollError("Choose a reason for leaving.", "reason")
    return termination_date, reason


async def settlement_preview(db: AsyncSession, employee: Employee, *, termination_date: date | None, reason: str | None,
                             unpaid_leave_days: int = 0, gym_country: str | None = None,
                             today: date | None = None) -> StatementDraft:
    """Final-settlement draft: salary for every day not yet on a statement up to the last working day + gratuity."""
    today = today or date.today()
    termination_date, reason = _validate_settlement_input(employee, termination_date, reason, today)
    covered = (await _covered_ranges(db, [employee.id])).get(employee.id, [])
    paid_through = max((e for _, e in covered), default=None)
    if paid_through and termination_date < paid_through:
        raise PayrollError(f"Salary is already on a statement through {paid_through:%d %b %Y}; the last working day must "
                           "be on or after that date.", "termination_date")
    segs = uncovered_segments(tracking_start(employee, covered), termination_date, covered)
    draft = compute_statement(
        monthly_salary=employee.monthly_salary, currency=employee.currency, country_code=employee.country_code,
        hire_date=employee.hire_date, period_start=segs[0][0] if segs else termination_date,
        period_end=termination_date, termination_date=termination_date, unpaid_leave_days=unpaid_leave_days,
        employment_type=employee.employment_type.value, is_final_settlement=True, termination_reason=reason,
        gym_country=gym_country, segments=segs,
    )
    draft.rules["settlement"] = {"termination_date": termination_date.isoformat(), "reason": reason,
                                 "reason_label": TERMINATION_REASONS[reason]}
    if not segs:
        draft.rules["notes"].insert(0, f"Salary is already on statements through {termination_date:%d %b %Y}; only the "
                                       "end-of-service benefit is settled here.")
    draft.rules["explanation"] = deterministic_explanation(draft).model_dump()
    return draft


async def final_settlement_of(db: AsyncSession, employee_id: int) -> SalaryStatement | None:
    return (
        await db.execute(
            select(SalaryStatement)
            .where(SalaryStatement.employee_id == employee_id, SalaryStatement.is_final_settlement.is_(True),
                   SalaryStatement.status != StatementStatus.VOID)
            .order_by(SalaryStatement.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def create_final_settlement(db: AsyncSession, employee: Employee, termination_date: date | None,
                                  reason: str | None, *, note: str | None = None, unpaid_leave_days: int = 0,
                                  actor: User | None = None, gym: Gym | None = None, request: Any = None,
                                  today: date | None = None) -> SalaryStatement:
    """Create the final-settlement statement (prorated salary up to the last working day + gratuity if eligible).

    The caller commits. A zero-amount settlement is marked PAID immediately (nothing to transfer)."""
    today = today or date.today()
    if employee.status == EmploymentStatus.TERMINATED:
        raise PayrollError("This staff member has already been terminated.")
    existing = await final_settlement_of(db, employee.id)
    if existing is not None:
        raise PayrollError("A final settlement already exists for this staff member"
                           + (" - pay it first." if existing.status != StatementStatus.PAID else "."))
    if gym is None:
        gym = await db.get(Gym, employee.gym_id)
    draft = await settlement_preview(db, employee, termination_date=termination_date, reason=reason,
                                     unpaid_leave_days=unpaid_leave_days, gym_country=gym.country_code if gym else None,
                                     today=today)
    note = (note or "").strip()[:200] or None
    draft.rules["settlement"]["note"] = note
    st = SalaryStatement(**draft.model_kwargs(), employee_id=employee.id, gym_id=employee.gym_id,
                         status=StatementStatus.DUE, due_date=max(today, draft.period_end))
    if draft.net_pay <= 0:
        st.status = StatementStatus.PAID
        st.paid_at = utcnow()
        st.rules = {**st.rules, "settled_without_payment": True}
    db.add(st)
    await db.flush()
    await audit(db, actor, "payroll.final_settlement_created", "salary_statement", st.id, gym_id=employee.gym_id,
                detail={"employee_id": employee.id, "termination_date": draft.rules["settlement"]["termination_date"],
                        "reason": reason, "net_pay": str(draft.net_pay), "gratuity": str(draft.gratuity),
                        "currency": draft.currency}, request=request)
    if st.status != StatementStatus.PAID:
        await enqueue(db, "payroll_explain", {"statement_id": st.id}, dedupe_key=f"payroll_explain:{st.id}", max_attempts=2,
                      user_id=actor.id if actor is not None else None)
    return st


# ============================================================================================ payout details
_DETAIL_KEYS = {
    "account_holder": ("account_holder", "holder", "name", "beneficiary_name"),
    "bank_name": ("bank_name", "bank"),
    "account_number": ("account_number", "bank_account", "account", "acct"),
    "ifsc": ("ifsc", "ifsc_code"),
    "upi": ("upi", "vpa", "upi_id"),
    "iban": ("iban",),
    "swift": ("swift", "bic", "swift_bic", "swift_code"),
    "routing_number": ("routing_number", "aba", "sort_code", "bsb"),
}


def payout_details(employee: Employee) -> dict[str, str]:
    """Decrypted payout details (normalised keys) - empty dict when missing or unreadable."""
    if not employee.payout_details_enc:
        return {}
    try:
        from app.security.crypto import decrypt_json

        raw = decrypt_json(employee.payout_details_enc)
    except Exception:  # wrong key / corrupted - never break the page
        log.warning("payout details of employee %s could not be decrypted", employee.id)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    lowered = {str(k).lower(): v for k, v in raw.items()}
    for key, aliases in _DETAIL_KEYS.items():
        for a in aliases:
            v = lowered.get(a)
            if v not in (None, ""):
                out[key] = str(v).strip()[:80]
                break
    return out


def masked_payout_details(details: dict[str, str]) -> list[tuple[str, str]]:
    labels = {"account_holder": "Account holder", "bank_name": "Bank", "account_number": "Account number", "ifsc": "IFSC",
              "upi": "UPI ID", "iban": "IBAN", "swift": "SWIFT / BIC", "routing_number": "Routing / sort code"}
    out = []
    for key, label in labels.items():
        v = details.get(key)
        if not v:
            continue
        if key in ("account_number", "iban"):
            v = mask_middle(v, keep_end=4)
        elif key == "upi" and "@" in v:
            user, _, host = v.partition("@")
            v = (user[:2] + "•" * max(1, len(user) - 2)) + "@" + host
        out.append((label, v))
    return out


def payout_mode(st: SalaryStatement, employee: Employee) -> tuple[str | None, str]:
    """('cashfree' | 'test' | None, explanation) for paying this statement online."""
    settings = get_settings()
    if st.status != StatementStatus.DUE:
        return None, "Online payout is only possible while the statement is due."
    if st.currency.upper() != "INR":
        return None, "Cashfree Payouts support INR bank / UPI transfers only - pay by bank transfer and upload the proof."
    if (employee.payout_method or "bank_transfer").lower() not in PAYOUT_METHODS_ONLINE:
        return None, "The employee's payout method is not a bank account or UPI - pay manually and upload the proof."
    if settings.cashfree_payouts_enabled:
        d = payout_details(employee)
        if not (d.get("upi") or (d.get("account_number") and d.get("ifsc"))):
            return None, "Add the employee's bank account + IFSC or UPI ID before paying through Cashfree."
        return "cashfree", "Pay instantly through Cashfree Payouts (IMPS / UPI)."
    if settings.payments_test_mode:
        return "test", "TEST MODE: simulates a Cashfree payout - no money moves. You still upload a proof afterwards."
    return None, "Cashfree Payouts are not configured - pay by bank transfer and upload the proof."


def payout_instructions(employee: Employee, gym: Gym, st: SalaryStatement) -> dict[str, Any]:
    rules = rules_for(employee.country_code)
    international = (employee.country_code or "").upper() != (gym.country_code or "").upper() or \
        st.currency.upper() != (gym.currency or st.currency).upper()
    details = payout_details(employee)
    amount = money(st.net_pay, st.currency)
    steps: list[str] = []
    paperwork: list[str] = []
    if international:
        steps += [
            f"Send an international (SWIFT) transfer of {amount} in {st.currency} to the employee's "
            + ("IBAN" if details.get("iban") else "bank account") + " with the SWIFT/BIC code.",
            f"FX: if you pay from a {gym.currency} account your bank converts at its own rate - make sure the employee "
            f"receives the full {amount} (choose charges 'OUR' so fees are not deducted from the salary).",
            "Use the bank's SWIFT confirmation (MT103 / transfer advice) as the proof; its reference is the UTR.",
        ]
        paperwork.append("Your bank will ask for a purpose-of-payment code (salary / compensation of employees) and may "
                         "ask for the employment contract.")
        if (gym.country_code or "").upper() == "IN":
            paperwork.append("Outward remittance from India: submit Form A2 with the purpose code; Form 15CA (and 15CB "
                             "from a chartered accountant when required) must be filed before remitting.")
        paperwork.append(f"Withholding / social security in {rules.name} may apply even though you pay from abroad - "
                         "check the tax treaty and local registration requirements.")
    else:
        steps += [
            f"Pay {amount} by {rules.payout_rail if rules.code != 'DEFAULT' else 'a local bank transfer'}.",
            "Keep the bank's confirmation (screenshot or PDF) showing the amount, the reference (UTR) and the date.",
        ]
        if rules.code in ("AE", "SA", "QA", "KW", "OM", "BH"):
            paperwork.append("Gulf states require salaries to go through the Wage Protection System (WPS) - pay through "
                             "your WPS agent or bank so the payment is reported.")
        if st.tax_withholding or st.employee_deductions:
            paperwork.append("Remit the withheld tax and statutory contributions to the authorities separately - they "
                             "are estimates on this statement.")
    return {
        "international": international,
        "rail": rules.payout_rail,
        "method": (employee.payout_method or "bank_transfer").replace("_", " "),
        "details": masked_payout_details(details),
        "has_details": bool(details),
        "steps": steps,
        "paperwork": paperwork,
        "country": rules.name if rules.code != "DEFAULT" else employee.country_code,
    }


# ============================================================================================ payouts
def _normalize_payout(res: Any) -> dict[str, Any]:
    def pick(*names: str) -> Any:
        for n in names:
            v = res.get(n) if isinstance(res, dict) else getattr(res, n, None)
            if v not in (None, ""):
                return v
        return None

    status = str(pick("status", "transfer_status", "state") or "").upper()
    error = pick("error", "message_error")
    ok = bool(res) and not error and status not in {"FAILED", "REJECTED", "ERROR", "REVERSED", "CANCELLED"}
    if isinstance(res, dict) and res.get("ok") is False:
        ok = False
    return {
        "ok": ok,
        "status": status or ("SUCCESS" if ok else "FAILED"),
        "reference": str(pick("utr", "reference", "bank_reference", "cf_transfer_id", "transfer_id") or "")[:80] or None,
        "provider_id": str(pick("cf_transfer_id", "transfer_id", "id") or "")[:64] or None,
        "message": str(pick("message", "status_description", "error") or "")[:300],
    }


async def _cashfree_payout(transfer_id: str, amount: Decimal, currency: str, beneficiary: dict[str, Any]) -> dict[str, Any]:
    """Adapter around WP-D's ``app.services.cashfree.create_payout`` (lazy import: absent -> unavailable)."""
    try:
        import importlib

        create = getattr(importlib.import_module("app.services.cashfree"), "create_payout")
    except (ImportError, AttributeError):
        return {"ok": False, "message": "Cashfree Payouts are unavailable right now - pay by bank transfer and upload the proof."}
    try:
        res = create(transfer_id, amount, currency, beneficiary)
        if inspect.isawaitable(res):
            res = await res
    except Exception as exc:
        log.warning("cashfree payout %s failed: %s", transfer_id, exc)
        return {"ok": False, "message": f"Cashfree payout failed: {str(exc)[:200]}"}
    norm = _normalize_payout(res)
    if not norm["ok"] and not norm["message"]:
        norm["message"] = "Cashfree rejected the payout."
    return norm


async def _payment_transaction(db: AsyncSession, st: SalaryStatement) -> Transaction | None:
    if st.transaction_id is None:
        return None
    return await db.get(Transaction, st.transaction_id)


def _tx_description(st: SalaryStatement, employee: Employee) -> str:
    kind = "Final settlement" if st.is_final_settlement else "Salary"
    return f"{kind} {period_label(st.period_start, st.period_end)} - {employee.display_name}"[:255]


async def initiate_payout(db: AsyncSession, st: SalaryStatement, employee: Employee, gym: Gym, actor: User, *,
                          request: Any = None) -> Transaction:
    """Pay a DUE statement through Cashfree Payouts (INR) or the TEST-MODE simulation. Proof is still required."""
    mode, why = payout_mode(st, employee)
    if mode is None:
        raise PayrollError(why)
    existing = await _payment_transaction(db, st)
    if existing is not None and existing.status in (TxStatus.PENDING, TxStatus.AWAITING_PROOF, TxStatus.PROOF_REVIEW):
        raise PayrollError("A payment for this statement is already in progress.")
    transfer_id = f"ipsal{st.id}x{secrets.token_hex(4)}"
    meta: dict[str, Any] = {"transfer_id": transfer_id, "initiated_by": actor.id, "initiated_at": utcnow().isoformat()}
    if mode == "cashfree":
        d = payout_details(employee)
        beneficiary = {"name": employee.full_name, "email": employee.invite_email, "phone": employee.phone,
                       "bank_account": d.get("account_number"), "ifsc": d.get("ifsc"), "vpa": d.get("upi"),
                       "method": "upi" if d.get("upi") and not d.get("account_number") else "bank_transfer"}
        res = await _cashfree_payout(transfer_id, st.net_pay, st.currency, beneficiary)
        meta["payout"] = {k: res.get(k) for k in ("status", "reference", "provider_id", "message")}
        if not res["ok"]:
            db.add(Transaction(
                gym_id=st.gym_id, direction=TxDirection.OUTGOING, category=TxCategory.SALARY, status=TxStatus.FAILED,
                amount=st.net_pay, currency=st.currency, description=_tx_description(st, employee),
                party_label=employee.display_name[:160], provider="cashfree", provider_order_id=transfer_id,
                employee_id=employee.id, salary_statement_id=st.id, created_by_id=actor.id, meta=meta,
            ))
            await audit(db, actor, "payroll.payout_failed", "salary_statement", st.id, gym_id=st.gym_id,
                        detail={"message": res["message"]}, request=request)
            await db.flush()
            raise PayrollError(res["message"] or "The payout failed.")
        reference = res.get("reference")
        provider_id = res.get("provider_id")
    else:
        reference = "TESTUTR" + "".join(secrets.choice("0123456789") for _ in range(10))
        provider_id = f"test_{secrets.token_hex(6)}"
        meta["test_mode"] = True
    tx = Transaction(
        gym_id=st.gym_id, direction=TxDirection.OUTGOING, category=TxCategory.SALARY, status=TxStatus.AWAITING_PROOF,
        amount=st.net_pay, currency=st.currency, description=_tx_description(st, employee),
        party_label=employee.display_name[:160], provider="cashfree" if mode == "cashfree" else "test",
        provider_order_id=transfer_id, provider_payment_id=provider_id, provider_reference=reference,
        employee_id=employee.id, salary_statement_id=st.id, created_by_id=actor.id, meta=meta,
    )
    db.add(tx)
    await db.flush()
    st.transaction_id = tx.id
    st.status = StatementStatus.AWAITING_PROOF
    await audit(db, actor, "payroll.payout_initiated", "salary_statement", st.id, gym_id=st.gym_id,
                detail={"mode": mode, "amount": str(st.net_pay), "currency": st.currency, "transaction_id": tx.id,
                        "reference": reference}, request=request)
    return tx


# ============================================================================================ proofs
def render_test_receipt(*, amount: Decimal, currency: str, reference: str, paid_on: date, beneficiary: str,
                        payer: str, statement_id: int) -> bytes:
    """TEST MODE only: a clearly watermarked receipt for a SIMULATED payout, so the proof -> OCR -> verification
    workflow can be exercised end to end without a real bank transfer (blocking: call via asyncio.to_thread)."""
    import io

    from PIL import Image, ImageDraw, ImageFont

    width, height = 1100, 720
    im = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(im)
    title = ImageFont.load_default(size=40)
    value = ImageFont.load_default(size=36)
    label = ImageFont.load_default(size=26)
    draw.rectangle([0, 0, width, 120], fill=(10, 132, 255))
    draw.text((48, 38), "IronPulse TEST MODE payout receipt", font=title, fill="white")
    rows = [
        ("Status", "SUCCESS (simulated)"),
        ("Amount", f"{currency} {amount:,.2f}"),
        ("Reference / UTR", reference),
        ("Date", paid_on.strftime("%d %b %Y")),
        ("Beneficiary", beneficiary[:40]),
        ("Paid by", payer[:40]),
        ("Statement", f"#{statement_id}"),
    ]
    y = 165
    for name, text_value in rows:
        draw.text((48, y + 6), name, font=label, fill=(110, 110, 115))
        draw.text((400, y), text_value, font=value, fill=(29, 29, 31))
        y += 70
    draw.line([(48, height - 92), (width - 48, height - 92)], fill=(229, 229, 234), width=2)
    draw.text((48, height - 70), "SIMULATED - NO MONEY MOVED. Generated for testing the proof workflow only.",
              font=label, fill=(215, 40, 30))
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-/ ]{5,39}$")


def normalize_reference(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


_REFERENCE_KEEP = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"  # spelled out: a regex range could follow the collation


def _normalized_reference_sql(column: Any) -> Any:
    """``normalize_reference`` in SQL (references are validated ASCII - REFERENCE_RE - so both agree)."""
    return func.regexp_replace(func.upper(column), f"[^{_REFERENCE_KEEP}]", "", "g")


_CONFUSABLE = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2", "G": "6"})


def _fuzzy(value: str) -> str:
    return normalize_reference(value).translate(_CONFUSABLE)


_AMOUNT_TOKEN = re.compile(r"(?<!\d)\d[\d.,'\u00a0\u202f]*\d(?!\d)|(?<![\d.,])\d(?!\d)")


def amounts_in(text: str) -> list[Decimal]:
    """Every number in ``text`` that could be an amount (1,23,456.78 / 123,456.78 / 123.456,78 / 123456)."""
    out: list[Decimal] = []
    for tok in _AMOUNT_TOKEN.findall(text or ""):
        t = tok.replace("\u00a0", "").replace("\u202f", "").replace("'", "")
        if "," in t and "." in t:
            if t.rfind(",") > t.rfind("."):
                t = t.replace(".", "").replace(",", ".")
            else:
                t = t.replace(",", "")
        elif "," in t:
            parts = t.split(",")
            t = t.replace(",", ".") if (len(parts) == 2 and len(parts[1]) == 2) else t.replace(",", "")
        elif t.count(".") > 1:
            head, _, tail = t.rpartition(".")
            t = head.replace(".", "") + ("." + tail if len(tail) <= 2 else tail)
        try:
            out.append(Decimal(t))
        except InvalidOperation:
            continue
    return out


_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE_PATTERNS = (
    re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b"),
    re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})\b"),
    re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-/.,]*" + _MON + r"[\s\-/.,]*(\d{2,4})\b", re.I),
    re.compile(r"\b" + _MON + r"[\s\-/.]*(\d{1,2})(?:st|nd|rd|th)?[,\s\-/.]+(\d{2,4})\b", re.I),
)


def _mk(y: int, m: int, d: int) -> date | None:
    if y < 100:
        y += 2000
    try:
        return date(y, m, d)
    except ValueError:
        return None


def dates_in(text: str) -> set[date]:
    found: set[date] = set()
    text = text or ""
    for m in _DATE_PATTERNS[0].finditer(text):
        if d := _mk(int(m.group(1)), int(m.group(2)), int(m.group(3))):
            found.add(d)
    for m in _DATE_PATTERNS[1].finditer(text):
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        for d in (_mk(y, b, a), _mk(y, a, b)):  # day-first and month-first
            if d:
                found.add(d)
    for m in _DATE_PATTERNS[2].finditer(text):
        mon = _MONTHS.get(m.group(2).lower()[:3]) or _MONTHS.get(m.group(2).lower())
        if mon and (d := _mk(int(m.group(3)), mon, int(m.group(1)))):
            found.add(d)
    for m in _DATE_PATTERNS[3].finditer(text):
        mon = _MONTHS.get(m.group(1).lower()[:3]) or _MONTHS.get(m.group(1).lower())
        if mon and (d := _mk(int(m.group(3)), mon, int(m.group(2)))):
            found.add(d)
    return found


@dataclass(slots=True)
class ProofCheck:
    passed: bool
    checks: list[dict[str, Any]]
    engines: list[str] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        return [c["detail"] for c in self.checks if not c["passed"]]

    def to_json(self) -> dict[str, Any]:
        return {"passed": self.passed, "checks": self.checks, "engines": self.engines, "reasons": self.reasons,
                "checked_at": utcnow().isoformat()}


def check_proof_text(text: str, *, expected_amount: Decimal, amount_paid: Decimal, reference: str, currency: str,
                     earliest: date, latest: date) -> ProofCheck:
    """Deterministic comparison of OCR text with what the owner declared (amount +-1 %, reference, date)."""
    checks: list[dict[str, Any]] = []
    tol = max(Decimal("0.01"), expected_amount * AMOUNT_TOLERANCE)
    declared_ok = abs(amount_paid - expected_amount) <= tol
    checks.append({"check": "declared_amount", "passed": declared_ok,
                   "detail": (f"Declared amount {money(amount_paid, currency)} matches the net pay"
                              if declared_ok else
                              f"Declared amount {money(amount_paid, currency)} differs from the net pay "
                              f"{money(expected_amount, currency)} by more than 1%")})
    amounts = amounts_in(text)
    hit = next((a for a in amounts if abs(a - amount_paid) <= max(Decimal("0.01"), amount_paid * AMOUNT_TOLERANCE)), None)
    checks.append({"check": "amount", "passed": hit is not None,
                   "detail": (f"Amount {money(hit, currency)} found on the proof" if hit is not None else
                              f"The amount {money(amount_paid, currency)} (+-1%) was not found on the proof")})
    ref = normalize_reference(reference)
    norm_text = normalize_reference(text)
    ref_ok = len(ref) >= 4 and (ref in norm_text or _fuzzy(ref) in _fuzzy(text))
    checks.append({"check": "reference", "passed": ref_ok,
                   "detail": ("Reference / UTR found on the proof" if ref_ok else
                              f"Reference {reference} was not found on the proof")})
    found_dates = sorted(dates_in(text))
    plausible = [d for d in found_dates if earliest <= d <= latest]
    checks.append({"check": "date", "passed": bool(plausible),
                   "detail": (f"Payment date {plausible[-1]:%d %b %Y} is plausible" if plausible else
                              ("No date found on the proof" if not found_dates else
                               f"Dates on the proof ({', '.join(f'{d:%d %b %Y}' for d in found_dates[:3])}) are outside "
                               f"{earliest:%d %b %Y} - {latest:%d %b %Y}"))})
    return ProofCheck(all(c["passed"] for c in checks), checks)


async def record_proof(db: AsyncSession, st: SalaryStatement, employee: Employee, actor: User, *, upload: Any,
                       reference: str, amount_paid: Decimal, paid_on: date | None, request: Any = None) -> Transaction:
    """Store the proof (encrypted), move the statement to PROOF_REVIEW and queue OCR verification.

    Raises ``storage.UploadError`` for bad files and :class:`PayrollError` for business-rule problems."""
    if st.status not in PAYABLE_STATUSES:
        raise PayrollError("A proof can only be uploaded while the salary is due or awaiting proof.")
    stored, _plain = await storage.save_private_file(upload, kind="payment_proofs")
    tx = await _payment_transaction(db, st)
    proof_meta = {"filename": stored.original_filename, "content_type": stored.content_type, "sha256": stored.sha256,
                  "size": stored.size, "uploaded_at": utcnow().isoformat(), "uploaded_by": actor.id,
                  "amount_paid": str(amount_paid), "paid_on": paid_on.isoformat() if paid_on else None,
                  "reference": reference}
    if tx is None or tx.status in (TxStatus.SUCCESS, TxStatus.FAILED, TxStatus.CANCELLED):
        tx = Transaction(
            gym_id=st.gym_id, direction=TxDirection.OUTGOING, category=TxCategory.SALARY, status=TxStatus.PROOF_REVIEW,
            amount=amount_paid, currency=st.currency, description=_tx_description(st, employee),
            party_label=employee.display_name[:160], provider="manual", employee_id=employee.id,
            salary_statement_id=st.id, created_by_id=actor.id, meta={},
        )
        db.add(tx)
    old_key = tx.proof_key
    history = list((tx.meta or {}).get("proof_history", []))
    if old_key:
        history.append({"key": old_key, "check": tx.proof_check, "replaced_at": utcnow().isoformat()})
    tx.meta = {**(tx.meta or {}), "proof": proof_meta, "proof_history": history[-5:]}
    if tx.provider_reference and normalize_reference(tx.provider_reference) != normalize_reference(reference):
        tx.meta["payout_reference"] = tx.provider_reference
    tx.provider_reference = reference[:80]
    tx.proof_key = stored.key
    tx.proof_check = None
    tx.proof_verified = None
    tx.amount = amount_paid
    tx.status = TxStatus.PROOF_REVIEW
    await db.flush()
    st.transaction_id = tx.id
    st.status = StatementStatus.PROOF_REVIEW
    await enqueue(db, "verify_payment_proof", {"statement_id": st.id}, dedupe_key=f"verify_payment_proof:{st.id}",
                  user_id=actor.id)
    await audit(db, actor, "payroll.proof_uploaded", "salary_statement", st.id, gym_id=st.gym_id,
                detail={"transaction_id": tx.id, "amount_paid": str(amount_paid), "reference": reference,
                        "sha256": stored.sha256}, request=request)
    return tx


async def _ocr_text(data: bytes) -> tuple[str, list[str], str | None]:
    ctype = "application/pdf" if data.startswith(b"%PDF-") else "image/jpeg"
    try:
        from app.services.ocr import ocr_document

        res = await ocr_document(data, ctype)
        text = res.combined_text or res.best_text
        return text, list(res.engines_used), None if text.strip() else "No text could be read from the proof."
    except NotImplementedError:
        return "", [], "Automatic reading (OCR) is not available on this server yet."
    except Exception as exc:  # engines must never break verification
        log.warning("proof OCR failed: %s", exc)
        return "", [], "Automatic reading (OCR) failed for this file."


PROOF_ROUNDS = 3  # proofs read per job run: one replaced while it was read is read in turn (its upload's job is this one)


async def _proof_in_review(db: AsyncSession, statement_id: int, *,
                           lock: bool) -> tuple[SalaryStatement, Transaction | None, Gym | None] | None:
    """The statement (+ employee) if it is still in PROOF_REVIEW, its payment transaction and gym - fresh rows, not the
    session's cached ones; ``lock``: FOR UPDATE of the statement (the write phase)."""
    stmt = (select(SalaryStatement).options(joinedload(SalaryStatement.employee))
            .where(SalaryStatement.id == statement_id).execution_options(populate_existing=True))
    if lock:
        stmt = stmt.with_for_update(of=SalaryStatement)
    st = (await db.execute(stmt)).unique().scalar_one_or_none()
    if st is None or st.status != StatementStatus.PROOF_REVIEW:
        return None
    tx = await db.get(Transaction, st.transaction_id, populate_existing=True) if st.transaction_id else None
    return st, tx, await db.get(Gym, st.gym_id)


async def _read_proof(key: str) -> tuple[str, list[str], str | None]:
    """(text, engines, error) of a stored proof: decrypt + OCR, no database work. Never raises."""
    try:
        return await _ocr_text(await storage.load_private(key))
    except Exception as exc:
        log.warning("could not load proof %s: %s", key, exc)
        return "", [], "The proof file could not be opened."


async def verify_payment_proof(db: AsyncSession, statement_id: int, *, today: date | None = None) -> ProofCheck | None:
    """Job body: OCR the proof and compare it with the declared payment -> PAID or back to AWAITING_PROOF.

    No transaction - and no row lock - is held while the proof is decrypted and read by the OCR engines (seconds per
    page; a pooled connection held idle that long starves the pool under load): the statement is read and that
    transaction committed (the caller's session commits here), the file is read, then the outcome is written in a new
    transaction under the statement's row lock - only for the proof that was read. A proof replaced meanwhile is read
    in turn (at most PROOF_ROUNDS; the last round reads under the lock)."""
    today = today or date.today()
    for attempt in range(PROOF_ROUNDS):
        found = await _proof_in_review(db, statement_id, lock=False)
        if found is None:
            return None
        st, tx, gym = found
        key = tx.proof_key if tx is not None and gym is not None else None
        read: tuple[str, list[str], str | None] | None = None
        if key:
            tx_id = tx.id
            await db.commit()  # end the read transaction: nothing is held while the proof is read
            read = await _read_proof(key)
        found = await _proof_in_review(db, statement_id, lock=True)
        if found is None:
            return None  # paid, confirmed or re-reviewed by someone else meanwhile
        st, tx, gym = found
        if tx is None or not tx.proof_key or gym is None:
            st.status = StatementStatus.AWAITING_PROOF
            return None
        if read is None or tx.id != tx_id or tx.proof_key != key:  # another proof was uploaded while this one was read
            if attempt + 1 < PROOF_ROUNDS:
                await db.commit()  # release the row lock, then read the new proof
                continue
            read = await _read_proof(tx.proof_key)
        return await _settle_proof(db, st, tx, gym, read, today=today)
    return None  # pragma: no cover - every round returns or continues; the last one always settles


async def _settle_proof(db: AsyncSession, st: SalaryStatement, tx: Transaction, gym: Gym,
                        read: tuple[str, list[str], str | None], *, today: date) -> ProofCheck:
    """Compare what was read with the declared payment and write the outcome (the statement row is locked)."""
    text, engines, ocr_error = read
    proof = (tx.meta or {}).get("proof", {})
    amount_paid = Decimal(str(proof.get("amount_paid") or tx.amount))
    reference = proof.get("reference") or tx.provider_reference or ""
    earliest = st.period_start - timedelta(days=31)
    latest = today + timedelta(days=1)
    check = check_proof_text(text, expected_amount=st.net_pay, amount_paid=amount_paid, reference=reference,
                             currency=st.currency, earliest=earliest, latest=latest)
    check.engines = engines
    if ocr_error:
        check.checks.insert(0, {"check": "ocr", "passed": False, "detail": ocr_error})
        check.passed = False
    # anti-fraud: the same file or the same reference must not settle two payments. Both are EXISTS probes that stop
    # at the first match - never a count over the ledger or the gym's whole list of references loaded into Python.
    sha = proof.get("sha256")
    if sha:  # served by ix_transactions_proof_sha256 (the expression with literal keys: proof_sha256())
        dup = await db.scalar(select(exists().where(
            Transaction.id != tx.id, proof_sha256() == sha, Transaction.salary_statement_id.is_distinct_from(st.id))))
        if dup:
            check.checks.append({"check": "duplicate_file", "passed": False,
                                 "detail": "This exact file was already used as proof for another payment"})
            check.passed = False
    norm_ref = normalize_reference(reference)
    if norm_ref:
        reused = await db.scalar(select(exists().where(
            Transaction.id != tx.id, Transaction.gym_id == st.gym_id, Transaction.status == TxStatus.SUCCESS,
            Transaction.direction == TxDirection.OUTGOING, Transaction.salary_statement_id != st.id,
            Transaction.provider_reference.is_not(None), _normalized_reference_sql(Transaction.provider_reference) == norm_ref)))
        if reused:
            check.checks.append({"check": "duplicate_reference", "passed": False,
                                 "detail": "This reference / UTR already settled another payment"})
            check.passed = False
    employee = st.employee
    owner_id = gym.owner_id
    if check.passed:
        await mark_paid(db, st, tx, employee=employee, verified=True, check=check.to_json())
        await notify(db, owner_id, "Salary payment verified",
                     f"The proof for {money(tx.amount, st.currency)} to {employee.display_name} matched - the salary "
                     f"for {period_label(st.period_start, st.period_end)} is marked paid.",
                     category=NotificationCategory.PAYMENT, severity=Severity.SUCCESS, link=f"/owner/payroll/{st.id}")
    else:
        st.status = StatementStatus.AWAITING_PROOF
        tx.status = TxStatus.AWAITING_PROOF
        tx.proof_verified = False
        tx.proof_check = check.to_json()
        await notify(db, owner_id, "Payment proof needs attention",
                     f"We could not verify the proof for {employee.display_name}: " + "; ".join(check.reasons[:3])
                     + ". Upload a clearer proof or confirm the payment with a justification.",
                     category=NotificationCategory.PAYMENT, severity=Severity.WARNING, link=f"/owner/payroll/{st.id}")
    return check


async def mark_paid(db: AsyncSession, st: SalaryStatement, tx: Transaction, *, employee: Employee, verified: bool,
                    check: dict[str, Any] | None) -> list[Transaction]:
    """Complete the ledger: SUCCESS transaction(s) - split into SALARY + GRATUITY when both are paid."""
    now = utcnow()
    tx.status = TxStatus.SUCCESS
    tx.completed_at = now
    tx.proof_verified = verified
    if check is not None:
        tx.proof_check = check
    out = [tx]
    salary_part = q(tx.amount - st.gratuity)
    if st.gratuity > 0 and salary_part > 0:
        tx.amount = salary_part
        tx.category = TxCategory.SALARY
        grat = Transaction(
            gym_id=st.gym_id, direction=TxDirection.OUTGOING, category=TxCategory.GRATUITY, status=TxStatus.SUCCESS,
            amount=st.gratuity, currency=st.currency,
            description=f"{(st.rules.get('gratuity') or {}).get('label') or 'Gratuity'} - {employee.display_name}"[:255],
            party_label=employee.display_name[:160], provider=tx.provider, provider_reference=tx.provider_reference,
            employee_id=employee.id, salary_statement_id=st.id, created_by_id=tx.created_by_id, proof_key=tx.proof_key,
            proof_check=tx.proof_check, proof_verified=verified, meta={"split_from": tx.id}, completed_at=now,
        )
        db.add(grat)
        tx.meta = {**(tx.meta or {}), "split": {"salary": str(salary_part), "gratuity": str(st.gratuity)}}
        out.append(grat)
    elif st.gratuity > 0:
        tx.category = TxCategory.GRATUITY
    st.status = StatementStatus.PAID
    st.paid_at = now
    await db.flush()
    if employee.user_id:
        await notify(db, employee.user_id, "Salary paid",
                     f"Your {'final settlement' if st.is_final_settlement else 'salary'} of {money(st.net_pay, st.currency)} "
                     f"for {period_label(st.period_start, st.period_end)} has been paid.",
                     category=NotificationCategory.PAYMENT, severity=Severity.SUCCESS, link="/staff")
    return out


async def confirm_despite_mismatch(db: AsyncSession, st: SalaryStatement, employee: Employee, actor: User,
                                   justification: str, *, request: Any = None) -> Transaction:
    """Owner override after a failed automatic check: PAID with ``proof_verified=False`` (audited)."""
    if st.status != StatementStatus.AWAITING_PROOF:
        raise PayrollError("Only a payment whose proof could not be verified can be confirmed manually.")
    tx = await _payment_transaction(db, st)
    if tx is None or not tx.proof_key or tx.proof_verified is not False:
        raise PayrollError("Upload a proof first - a manual confirmation needs a stored proof that failed verification.")
    justification = (justification or "").strip()
    if len(justification) < 15:
        raise PayrollError("Explain in at least 15 characters why the payment is correct.", "justification")
    check = dict(tx.proof_check or {})
    check["override"] = {"by": actor.id, "at": utcnow().isoformat(), "justification": justification[:500]}
    await mark_paid(db, st, tx, employee=employee, verified=False, check=check)
    await audit(db, actor, "payroll.confirm_mismatch", "salary_statement", st.id, gym_id=st.gym_id,
                detail={"transaction_id": tx.id, "justification": justification[:500],
                        "reasons": (tx.proof_check or {}).get("reasons", [])}, request=request)
    return tx


async def adjust_unpaid_leave(db: AsyncSession, st: SalaryStatement, employee: Employee, actor: User,
                              unpaid_leave_days: int, *, gym_country: str | None = None, request: Any = None) -> None:
    """Recompute a DUE statement (before any payment) with a new number of unpaid leave days."""
    if st.status != StatementStatus.DUE or st.transaction_id is not None:
        raise PayrollError("Only a due statement without a payment can be adjusted.")
    rules = st.rules or {}
    segs = [(date.fromisoformat(a), date.fromisoformat(b)) for a, b in rules.get("segments", [])] or \
        [(st.period_start, st.period_end)]
    settlement = rules.get("settlement")
    term = date.fromisoformat(settlement["termination_date"]) if settlement else None
    draft = compute_statement(
        monthly_salary=st.monthly_salary, currency=st.currency, country_code=st.country_code,
        hire_date=date.fromisoformat(rules["hire_date"]) if rules.get("hire_date") else employee.hire_date,
        period_start=segs[0][0], period_end=segs[-1][1], termination_date=term or employee.termination_date,
        unpaid_leave_days=unpaid_leave_days, employment_type=rules.get("employment_type") or employee.employment_type.value,
        is_final_settlement=st.is_final_settlement, termination_reason=(settlement or {}).get("reason"),
        gym_country=gym_country, segments=segs,
    )
    if settlement:
        draft.rules["settlement"] = settlement
    draft.rules["adjustments"] = list(rules.get("adjustments", [])) + [
        {"at": utcnow().isoformat(), "by": actor.id, "unpaid_leave_days": unpaid_leave_days}]
    draft.rules["explanation"] = deterministic_explanation(draft).model_dump()
    before = {"net_pay": str(st.net_pay), "unpaid_leave_days": st.unpaid_leave_days}
    for k, v in draft.model_kwargs().items():
        if k not in ("period_start", "period_end", "is_final_settlement"):
            setattr(st, k, v)
    st.ai_explanation = None
    st.ai_provider = None
    await audit(db, actor, "payroll.statement_adjusted", "salary_statement", st.id, gym_id=st.gym_id,
                detail={"before": before, "after": {"net_pay": str(st.net_pay), "unpaid_leave_days": unpaid_leave_days}},
                request=request)
    await enqueue(db, "payroll_explain", {"statement_id": st.id}, dedupe_key=f"payroll_explain:{st.id}", max_attempts=2,
                  user_id=actor.id)


# ============================================================================================ AI explanation
EXPLAIN_ROUNDS = 2  # the numbers changed while the AI wrote (unpaid leave adjusted): explained once more, then given up


async def explain_statement(db: AsyncSession, statement_id: int) -> bool:
    """Ask the AI to explain a statement (explanation only - numbers are not touched). False when AI is unavailable.

    No transaction is held while the AI writes (a pooled connection held idle for the whole call starves the pool under
    load): the statement is read and that transaction committed (the caller's session commits here), the AI asked, then
    the explanation is stored in a new transaction under the statement's row lock - only if the statement still has
    the numbers the AI explained."""
    for _round in range(EXPLAIN_ROUNDS):
        st = await db.get(SalaryStatement, statement_id, populate_existing=True)
        if st is None:
            return False
        facts = ai_payroll.statement_facts(st)
        fallback = deterministic_explanation(st)
        await db.commit()  # end the read transaction: nothing is held while the AI answers
        expl, provider = await ai_payroll.explain(facts, fallback)
        if provider is None:
            return False
        st = (await db.execute(select(SalaryStatement).where(SalaryStatement.id == statement_id).with_for_update()
                               .execution_options(populate_existing=True))).scalar_one_or_none()
        if st is None:
            return False
        if ai_payroll.statement_facts(st) != facts:  # adjusted meanwhile: that explanation is for other numbers
            await db.commit()
            continue
        st.ai_explanation = ai_payroll.dumps(expl)
        st.ai_provider = provider[:40]
        return True
    return False


@job_handler("payroll_explain", on_exit="keep")
async def _explain_job(db: AsyncSession, payload: dict[str, Any]) -> None:
    await explain_statement(db, int(payload["statement_id"]))


@job_handler("verify_payment_proof", on_exit="keep")
async def _verify_job(db: AsyncSession, payload: dict[str, Any]) -> None:
    await verify_payment_proof(db, int(payload["statement_id"]))


# ============================================================================================ periodic tasks
def payroll_gym_clause() -> Any:
    """Gyms whose staff get monthly statements: active ones - and suspended ones (an owner ban, a deletion in its
    cooling-off, app/services/governance.py): their staff's earned pay keeps being recorded until it is settled. An
    archived (deleted) gym settled everyone before it was archived."""
    return or_(Gym.is_active.is_(True), and_(Gym.suspended_at.is_not(None), Gym.archived_at.is_(None)))


async def _generate_gym_month(gym_id: int, year: int, month: int, due_on: date) -> int:
    """One gym's statements for the month in ONE short transaction of its own: the statements, the AI explanations of
    the first EXPLAIN_PER_RUN and the owner's notification commit together, or not at all (a failure is logged and
    leaves the gym to the next run). Returns the statements created."""
    try:
        async with session_scope() as s:
            gym = await s.get(Gym, gym_id)
            if gym is None or not (gym.is_active or (gym.suspended_at is not None and gym.archived_at is None)):
                return 0
            res = await generate_statements(s, gym, year, month)
            n = len(res.created_ids)
            if n:
                await notify_bulk(s, [Note(gym.owner_id, f"Salaries for {res.label} are ready",
                                           f"{n} salary statement{'s' if n != 1 else ''} for {gym.name} "
                                           f"{'are' if n != 1 else 'is'} due on {due_on:%d %b %Y}.",
                                           link="/owner/payroll?status=due", category=NotificationCategory.PAYMENT,
                                           severity=Severity.INFO)])
            return n
    except Exception:
        log.exception("payroll generation failed for gym %s", gym_id)
        return 0


async def run_monthly_generation(db: AsyncSession, *, today: date | None = None) -> int:
    """On day 1, generate the previous month's statements for every active gym (idempotent).

    Gyms are picked in keyset chunks of GEN_GYM_BATCH ids (on ``db``, the periodic's lock-holding session) and each
    gym is generated in ONE short transaction of its own (``_generate_gym_month``): one gym's work - up to thousands of
    statements - is never held open, rolled back or retried together with another's. Only gyms that still have
    someone to pay for the month are picked (``_needs_statements``), so the later hourly runs of day 1 cost one query,
    and a run that spends GEN_BUDGET_S leaves the rest to the next hour."""
    today = today or utcnow().date()
    if today.day != 1:
        return 0
    year, month = previous_month(today)
    m_start, m_end = month_bounds(year, month)
    due_on = due_date_for(m_end)
    run = Chunks(holder=db, budget_s=GEN_BUDGET_S)
    total = last_id = 0
    while await run.next():
        gym_ids = list((await db.execute(
            select(Gym.id).where(payroll_gym_clause(), Gym.id > last_id, _needs_statements(m_start, m_end))
            .order_by(Gym.id).limit(GEN_GYM_BATCH)
        )).scalars().all())
        for k, gym_id in enumerate(gym_ids):
            if k and run.out_of_time:  # budget spent mid-chunk: the next hourly run picks the rest up
                return total
            total += await _generate_gym_month(gym_id, year, month, due_on)
            last_id = gym_id
        run.done(len(gym_ids), GEN_GYM_BATCH)
    return total


# ============================================================================================ "run payroll", all gyms
GENERATE_ATTEMPTS = 3
IN_FLIGHT = (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED)


def generation_key(owner_id: int, year: int, month: int) -> str:
    return f"{GENERATE_JOB}:{owner_id}:{year:04d}-{month:02d}"


async def queue_owner_generation(db: AsyncSession, owner: User, year: int, month: int, *,
                                 gym_count: int) -> tuple[Job | None, bool]:
    """Queue "run payroll" for every gym of ``owner`` (the caller commits). Returns (job, queued): an unfinished run of
    the same owner and month is returned as it is (``queued`` False) - a second click queues nothing."""
    key = generation_key(owner.id, year, month)
    existing = await db.scalar(select(Job).where(Job.dedupe_key == key))
    if existing is not None and existing.status in IN_FLIGHT:
        return existing, False
    job = await enqueue(db, GENERATE_JOB, {"owner_id": owner.id, "year": year, "month": month, "gyms": gym_count},
                        dedupe_key=key, user_id=owner.id, max_attempts=GENERATE_ATTEMPTS)
    if job is not None:
        job.progress = 0.0
    return job, True


async def generation_in_flight(db: AsyncSession, owner_id: int) -> Job | None:
    """The owner's newest unfinished "run payroll" job (the payroll page shows its progress) - ix_jobs_user_status."""
    return await db.scalar(select(Job).where(Job.user_id == owner_id, Job.status.in_(IN_FLIGHT),
                                             Job.kind == GENERATE_JOB).order_by(Job.id.desc()).limit(1))


def _generation_progress(job_id: int | None, user_id: int | None, done: int, total: int) -> None:
    """``task.update`` 'running' with the share of gyms done (the payroll page's progress bar). Never raises."""
    if job_id is None or user_id is None:
        return
    evt = {"type": "task.update", "task_id": job_id, "kind": GENERATE_JOB, "status": JobStatus.RUNNING.value,
           "progress": round(done * 100 / total) if total else 100, "message": f"{done} of {total} gyms done",
           "link": "/owner/payroll", "notify": False}
    try:
        from app.services.realtime import hub

        hub.publish_nowait([user_id], evt)
    except Exception:  # noqa: BLE001 - a lost progress note must never affect the run
        log.debug("task.update for payroll run %s not delivered", job_id, exc_info=True)


async def run_owner_generation(owner_id: int, year: int, month: int, *,
                               state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Generate ``year``-``month`` for every gym ``owner_id`` owns now, ONE short transaction per gym: the statements,
    the AI explanations (at most EXPLAIN_PER_RUN for the whole run), the gym's audit row and the job's progress commit
    together. ``state`` (the job payload) carries the tally across attempts: a retried run skips the gyms already
    done. Returns the tally; the owner is notified at the end."""
    state = dict(state or {})
    done: list[int] = [int(g) for g in state.get("done", [])]
    tally = {k: int(state.get(k, 0)) for k in ("created", "skipped", "explained")}
    failed: list[str] = []
    run = current_run()
    job_id = run.job_id if run is not None else None
    async with session_scope() as s:
        owner = await s.get(User, owner_id)
        gyms = list((await s.execute(select(Gym.id, Gym.name).where(Gym.owner_id == owner_id)
                                     .order_by(Gym.name, Gym.id))).all())
    if owner is None or owner.deleted_at is not None:
        return {**state, "gyms": 0}
    label = GenerationResult(year, month).label
    total = len(gyms)
    for gym_id, name in gyms:
        if gym_id in done:
            continue
        now_done, now_tally = [*done, gym_id], dict(tally)
        try:
            async with session_scope() as s:
                gym = await s.get(Gym, gym_id)
                if gym is not None and gym.owner_id == owner_id:
                    res = await generate_statements(s, gym, year, month, queue_explanations=False)
                    explain = res.created_ids[:max(0, EXPLAIN_PER_RUN - tally["explained"])]
                    await queue_explanations_for(s, explain)
                    await audit(s, owner, "payroll.generate", "gym", gym_id, gym_id=gym_id, detail={
                        "month": f"{year:04d}-{month:02d}", "created": len(res.created_ids), "skipped": len(res.skipped),
                        "background": True})
                    now_tally = {"created": tally["created"] + len(res.created_ids),
                                 "skipped": tally["skipped"] + len(res.skipped),
                                 "explained": tally["explained"] + len(explain)}
                if job_id is not None:  # progress + tally commit with the gym's statements (a retry resumes here)
                    await s.execute(update(Job).where(Job.id == job_id).values(
                        progress=round(len(now_done) * 100 / total, 1), payload={**state, "done": now_done, **now_tally}))
        except Exception:  # this gym rolled back alone and the others carry on; the owner is told to run it again
            log.exception("payroll generation failed for gym %s (%s-%02d)", gym_id, year, month)
            failed.append(name)
            continue
        done, tally = now_done, now_tally
        _generation_progress(job_id, owner_id, len(done), total)
    title, body = _generation_notice(label, tally, total, failed)
    async with session_scope() as s:
        await notify(s, owner_id, title, body, category=NotificationCategory.PAYMENT,
                     severity=Severity.WARNING if failed else Severity.SUCCESS, link="/owner/payroll?status=unpaid")
    set_result_event(title=title, message=body, link="/owner/payroll?status=unpaid")
    return {**state, "done": done, **tally, "failed": failed, "gyms": total}


def _generation_notice(label: str, tally: dict[str, int], gyms: int, failed: list[str]) -> tuple[str, str]:
    created = tally["created"]
    if created:
        title = f"Payroll for {label} is ready"
        body = (f"{created:,} salary statement{'s' if created != 1 else ''} created across {gyms} gym"
                f"{'s' if gyms != 1 else ''}.")
        if tally["skipped"]:
            body += f" {tally['skipped']:,} staff member{'s were' if tally['skipped'] != 1 else ' was'} already " \
                    "covered or not employed that month."
        if created > tally["explained"]:
            body += (f" AI explanations were queued for the first {tally['explained']}; open any other statement to ask "
                     "for one." if tally["explained"] else " Open a statement to ask the AI to explain it.")
    else:
        title = f"Nothing new for {label}"
        body = "Every active staff member is already covered."
    if failed:
        title = f"Payroll for {label}: some gyms need another try"
        body += (f" Payroll could not be generated for {', '.join(failed[:3])}"
                 + (f" and {len(failed) - 3} more" if len(failed) > 3 else "") + " - please run it again.")
    return title, body


@job_handler(GENERATE_JOB, on_exit="keep")
async def _generate_job(db: AsyncSession, payload: dict[str, Any]) -> None:
    try:
        owner_id, year, month = int(payload["owner_id"]), int(payload["year"]), int(payload["month"])
    except (KeyError, TypeError, ValueError):
        log.warning("payroll_generate: bad payload %r", payload)
        return
    await run_owner_generation(owner_id, year, month, state=payload)


def _needs_statements(m_start: date, m_end: date) -> Any:
    """EXISTS (correlated to ``Gym``): someone ``generate_statements`` pays for the month (active, or terminated in it,
    hired by its end) has no non-void statement touching the month yet. Gyms generated earlier - by a previous hourly
    run or by the owner - drop out in SQL."""
    covered = exists(select(SalaryStatement.id).where(
        SalaryStatement.employee_id == Employee.id, SalaryStatement.status != StatementStatus.VOID,
        SalaryStatement.period_end >= m_start, SalaryStatement.period_start <= m_end,
    ))
    return exists(select(Employee.id).where(
        Employee.gym_id == Gym.id, Employee.deleted_at.is_(None), Employee.hire_date <= m_end,
        or_(Employee.status == EmploymentStatus.ACTIVE,
            and_(Employee.status == EmploymentStatus.TERMINATED, Employee.termination_date >= m_start,
                 Employee.termination_date <= m_end)),
        ~covered,
    ))


async def send_salary_reminders(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Remind owners of salaries due within 3 days or overdue - at most once per statement per day.

    In chunks of SALARY_REMINDER_BATCH statements, one short transaction each: claim (``FOR UPDATE SKIP LOCKED``), load
    them with their employees, one bulk notification insert (owners loaded once), stamp ``last_reminder_at`` - so a
    stamped statement leaves the due set and a backlog is drained within SALARY_REMINDER_BUDGET_S. Returns the
    notifications sent."""
    now = now or utcnow()
    today = now.date()
    due = (
        SalaryStatement.status.in_(PAYABLE_STATUSES),
        SalaryStatement.due_date <= today + timedelta(days=REMINDER_LEAD_DAYS),
        or_(SalaryStatement.last_reminder_at.is_(None), SalaryStatement.last_reminder_at <= now - timedelta(hours=24)),
    )
    run = Chunks(holder=db, budget_s=SALARY_REMINDER_BUDGET_S)
    sent = 0
    while await run.next():
        async with session_scope() as s:
            ids = list((await s.execute(
                select(SalaryStatement.id).where(*due).order_by(SalaryStatement.due_date, SalaryStatement.id)
                .limit(SALARY_REMINDER_BATCH).with_for_update(skip_locked=True)
            )).scalars().all())
            if ids:
                rows = (await s.execute(
                    select(SalaryStatement, Gym.owner_id).join(Gym, Gym.id == SalaryStatement.gym_id)
                    .options(joinedload(SalaryStatement.employee))
                    .where(SalaryStatement.id.in_(ids)).order_by(SalaryStatement.due_date, SalaryStatement.id)
                )).unique().all()
                notes = []
                for st, owner_id in rows:
                    overdue = st.due_date < today
                    amount = money(st.net_pay, st.currency)
                    name = st.employee.display_name
                    body = (f"Pay {amount} to {name} - upload the proof of transaction. "
                            f"{'Final settlement' if st.is_final_settlement else 'Salary'} for "
                            f"{period_label(st.period_start, st.period_end)}, {'was due' if overdue else 'due'} "
                            f"{st.due_date:%d %b %Y}.")
                    notes.append(Note(owner_id, f"{'Overdue' if overdue else 'Salary due'}: pay {amount} to {name}", body,
                                      link=f"/owner/payroll/{st.id}", category=NotificationCategory.PAYMENT,
                                      severity=Severity.WARNING if overdue else Severity.INFO))
                sent += len(await notify_bulk(s, notes))
                await s.execute(update(SalaryStatement).where(SalaryStatement.id.in_(ids)).values(last_reminder_at=now)
                                .execution_options(synchronize_session=False))
        run.done(len(ids), SALARY_REMINDER_BATCH)
    return sent


@periodic("payroll_monthly_generation", every_seconds=3600, initial_delay=90)
async def _monthly_generation(db: AsyncSession) -> None:
    await run_monthly_generation(db)


@periodic("payroll_salary_reminders", every_seconds=3600, initial_delay=120)
async def _salary_reminders(db: AsyncSession) -> None:
    await send_salary_reminders(db)


# ============================================================================================ termination
async def termination_blockers(db: AsyncSession, employee: Employee) -> list[str]:
    """Why ``employee`` cannot be terminated yet (empty list = allowed)."""
    blockers: list[str] = []
    if employee.status == EmploymentStatus.TERMINATED:
        return ["This staff member has already been terminated."]
    unpaid = (
        await db.execute(
            select(func.count(SalaryStatement.id), func.coalesce(func.sum(SalaryStatement.net_pay), 0))
            .where(SalaryStatement.employee_id == employee.id, SalaryStatement.status.in_(UNPAID_STATUSES))
        )
    ).one()
    if unpaid[0]:
        blockers.append(f"{unpaid[0]} salary statement{'s are' if unpaid[0] != 1 else ' is'} still unpaid "
                        f"({money(unpaid[1], employee.currency)}). Pay them and upload the proofs first.")
    if employee.status in (EmploymentStatus.ACTIVE, EmploymentStatus.SUSPENDED):
        fs = await final_settlement_of(db, employee.id)
        if fs is None:
            blockers.append("Create the final settlement (salary up to the last working day + gratuity) first.")
    return blockers


async def terminate_employee(db: AsyncSession, employee: Employee, actor: User, *, gym: Gym | None = None,
                             request: Any = None) -> SalaryStatement | None:
    """Terminate once everything is paid: status TERMINATED, shifts deleted, client links deactivated, notified."""
    blockers = await termination_blockers(db, employee)
    if blockers:
        raise TerminationBlocked(blockers)
    gym = gym or await db.get(Gym, employee.gym_id)
    fs = await final_settlement_of(db, employee.id)
    settlement = (fs.rules or {}).get("settlement") if fs else None
    term_date = date.fromisoformat(settlement["termination_date"]) if settlement else date.today()
    reason = settlement.get("reason_label") if settlement else "Invitation withdrawn before starting"
    if settlement and settlement.get("note"):
        reason = f"{reason}: {settlement['note']}"
    employee.status = EmploymentStatus.TERMINATED
    employee.termination_date = term_date
    employee.termination_reason = reason[:255]
    shifts = (await db.execute(delete(TrainerShift).where(TrainerShift.employee_id == employee.id))).rowcount or 0
    clients = (await db.execute(update(TrainerClient).where(TrainerClient.employee_id == employee.id,
                                                            TrainerClient.active.is_(True)).values(active=False))).rowcount or 0
    title = f"Your employment at {gym.name if gym else 'the gym'} has ended"
    body = f"Your last working day is {term_date:%d %b %Y}."
    if fs is not None:
        body += f" Your final settlement of {money(fs.net_pay, fs.currency)} has been paid."
    if employee.user_id:
        await notify(db, employee.user_id, title, body, category=NotificationCategory.STAFF, severity=Severity.WARNING,
                     link="/staff")
    elif employee.invite_email:
        await email_address(db, employee.invite_email, title, body, recipient=employee.full_name)
    await audit(db, actor, "staff.terminated", "employee", employee.id, gym_id=employee.gym_id,
                detail={"termination_date": term_date.isoformat(), "reason": reason[:255], "shifts_deleted": shifts,
                        "clients_deactivated": clients, "final_settlement_id": fs.id if fs else None}, request=request)
    return fs
