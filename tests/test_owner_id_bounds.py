"""F42: integer ids beyond PostgreSQL's BIGINT range in the owner and staff consoles.

Routes declared their ids as plain ``int``, so ``9223372036854775808`` (or ``-9223372036854775809``) reached asyncpg,
which raised "value out of int64 range": a 500, an 'unhandled error' traceback in the log for every request (cheap log
flooding) and a keep-alive connection closed by the server. Every integer path id of these routers is now bounded
(``IdParam``: 1..2**63-1, like the super-admin's), and the integer ids read from forms and filters are parsed the same
way - an out-of-range or odd value ('²', thousands of digits) is "not found" / a form error, never a server error."""

from __future__ import annotations

import re
from typing import Any

from fastapi.routing import APIRoute

from app.main import app as fastapi_app
from app.models.enums import EmployeeRole, Role
from tests.conftest import login_as, make_client, post_form
from tests.factories import (
    make_branch,
    make_customer,
    make_employee,
    make_gym,
    make_owner,
    make_plans,
    make_subscription,
    make_user,
)

MODULES = ("app.routers.owner_customers", "app.routers.owner_finance", "app.routers.owner_gyms",
           "app.routers.owner_plans", "app.routers.owner_staff", "app.routers.staff_portal")
TOO_BIG = str(2**63)
TOO_SMALL = str(-(2**63) - 1)
PARAM = re.compile(r"{(\w+)(:\w+)?}")


def _api_routes(routes: Any) -> Any:
    for route in routes:
        inner = getattr(route, "original_router", None)  # an included router (main.py includes them unprefixed)
        if inner is not None:
            yield from _api_routes(inner.routes)
        elif isinstance(route, APIRoute):
            yield route


def _routes() -> list[tuple[str, str]]:
    out = []
    for route in _api_routes(fastapi_app.routes):
        if route.endpoint.__module__ in MODULES and PARAM.search(route.path):
            out += [(method, route.path) for method in sorted(route.methods - {"HEAD"})]
    return out


async def _call(client: Any, method: str, url: str, data: dict[str, Any] | None = None) -> tuple[int | None, str]:
    try:
        r = await (client.get(url) if method == "GET" else post_form(client, url, data or {}))
    except Exception as exc:  # noqa: BLE001 - the in-process transport re-raises what the app failed on
        return None, f"{type(exc).__name__}: {str(exc)[:120]}"
    return r.status_code, ""


def test_the_scan_sees_the_routes():
    routes = _routes()
    assert len(routes) >= 60 and ("GET", "/owner/staff/{employee_id}") in routes
    assert ("GET", "/owner/payroll/{statement_id:int}") in routes and ("GET", "/staff/clients/{customer_id}") in routes


async def test_out_of_range_path_ids_are_never_a_server_error(db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    staff = await make_user(db, role=Role.STAFF)
    await make_employee(db, gym, user=staff)
    failures: list[str] = []
    async with make_client() as oc, make_client() as sc:
        await login_as(oc, owner)
        await login_as(sc, staff)
        for method, path in _routes():
            client = sc if path.startswith("/staff/") else oc
            for value in (TOO_BIG, TOO_SMALL):
                url = PARAM.sub(value, path)
                status, error = await _call(client, method, url)
                if status is None or status >= 500:
                    failures.append(f"{method} {url} -> {status or error}")
    assert not failures, "\n".join(failures)


async def test_out_of_range_ids_in_forms_and_filters_are_never_a_server_error(db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    branch = await make_branch(db, gym)
    trainer = await make_employee(db, gym, role=EmployeeRole.TRAINER)
    plans = {p.code: p for p in await make_plans(db, gym)}
    member = await make_customer(db)
    await make_subscription(db, member, plans["offline_trainer"], branch=branch)
    huge = "9" * 5000  # beyond Python's int() digit limit as well as BIGINT
    failures: list[str] = []
    async with make_client() as oc:
        await login_as(oc, owner)
        for value in (TOO_BIG, "²", huge, "-1"):
            for method, url, data in (
                    ("GET", f"/owner/staff?gym={value}", None),
                    ("GET", f"/owner/jobs?gym={value}", None),
                    ("POST", f"/owner/staff/{trainer.id}/clients", {"subscription_id": value}),
                    ("POST", f"/owner/staff/{trainer.id}/shifts", {"weekdays": ["0"], "start_time": "06:00",
                                                                   "end_time": "08:00", "mode": "offline",
                                                                   "branch_id": value})):
                status, error = await _call(oc, method, url, data)
                if status is None or status >= 500:
                    failures.append(f"{method} {url[:80]} {str(data)[:80]} -> {status or error}")
    assert not failures, "\n".join(failures)
