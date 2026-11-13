"""WP-C1 shift rules: overlap (same person, all gyms), 6 h between different branches on a circular week
(Sunday -> Monday wrap), 50 km same-brand limit, opening hours, verified/open branches, work mode, online
shifts, owner HTTP flow and constant query counts."""

from __future__ import annotations

import math

import pytest
from sqlalchemy import func, select

from app.core.geo import EARTH_RADIUS_KM, haversine_km
from app.db import QueryCounter
from app.models import Notification, TrainerShift, User
from app.models.enums import EmploymentStatus, Role, WorkMode
from app.services import staffing as S
from app.services.staffing import BranchInfo, ShiftSlot, check_new_shifts, circular_gap_minutes
from tests.conftest import login_as, post_form
from tests.factories import (
    INDIRANAGAR,
    MYSURU,
    WHITEFIELD,
    make_branch,
    make_employee,
    make_gym,
    make_owner,
    make_shift,
    make_user,
)

GAP = 6 * 60
MAX_KM = 50.0


def north_of(origin: tuple[float, float], km: float) -> tuple[float, float]:
    """A point ``km`` kilometres due north of ``origin`` (exact on the haversine sphere)."""
    return origin[0] + math.degrees(km / EARTH_RADIUS_KM), origin[1]


def hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def slot(day: int, start: str, end: str, *, branch: BranchInfo | None = None, label: str = "") -> ShiftSlot:
    if branch is None:
        return ShiftSlot(day, hm(start), hm(end), "online", label=label or "online")
    return ShiftSlot(day, hm(start), hm(end), "offline", branch_id=branch.id, gym_id=branch.gym_id, lat=branch.lat,
                     lon=branch.lon, label=label or branch.name)


def binfo(bid: int, coords: tuple[float, float] = INDIRANAGAR, *, gym_id: int = 1, verified: bool = True,
          is_open: bool = True, hours: tuple[str, str] = ("05:00", "19:00"), name: str | None = None) -> BranchInfo:
    return BranchInfo(id=bid, gym_id=gym_id, name=name or f"Branch {bid}", verified=verified, is_open=is_open,
                      lat=coords[0], lon=coords[1], hours={d: (hm(hours[0]), hm(hours[1])) for d in range(7)})


def check(new: list[ShiftSlot], existing: list[ShiftSlot], branch: BranchInfo | None, *,
          work_mode: WorkMode = WorkMode.BOTH, gym_id: int = 1) -> list[str]:
    return check_new_shifts(new, existing, work_mode=work_mode, employee_gym_id=gym_id, branch=branch,
                            min_gap_minutes=GAP, max_distance_km=MAX_KM, person="Ana")


# ====================================================================== pure rules
def test_circular_gap_wraps_sunday_to_monday():
    a = ShiftSlot(6, hm("18:00"), hm("22:00"), "offline", branch_id=1)
    b = ShiftSlot(0, hm("03:00"), hm("07:00"), "offline", branch_id=2)
    assert circular_gap_minutes(a, b) == 5 * 60
    assert circular_gap_minutes(b, a) == 5 * 60
    c = ShiftSlot(0, hm("04:00"), hm("08:00"), "offline", branch_id=2)
    assert circular_gap_minutes(a, c) == 6 * 60
    same_day = ShiftSlot(6, hm("10:00"), hm("12:00"), "offline", branch_id=2)
    assert circular_gap_minutes(a, same_day) == 6 * 60
    overlapping = ShiftSlot(6, hm("21:00"), hm("23:00"), "offline", branch_id=2)
    assert circular_gap_minutes(a, overlapping) == 0


def test_overlap_with_same_person_is_rejected():
    a, b = binfo(1), binfo(2, WHITEFIELD)
    errors = check([slot(0, "09:00", "12:00", branch=a)], [slot(0, "06:00", "10:00", branch=a)], a)
    assert any("overlaps" in e for e in errors)
    # back-to-back at the SAME branch is fine
    assert check([slot(0, "10:00", "12:00", branch=a)], [slot(0, "06:00", "10:00", branch=a)], a) == []
    # different branch, overlapping -> overlap error
    errors = check([slot(0, "09:00", "12:00", branch=b)], [slot(0, "06:00", "10:00", branch=a)], b)
    assert any("overlaps" in e for e in errors)


def test_different_branches_need_six_hours():
    a, b = binfo(1), binfo(2, WHITEFIELD)
    existing = [slot(0, "06:00", "10:00", branch=a)]
    too_close = check([slot(0, "15:00", "19:00", branch=b)], existing, b)
    assert len(too_close) == 1 and "only 5 h away" in too_close[0] and "at least 6 h apart" in too_close[0]
    assert check([slot(0, "16:00", "19:00", branch=b)], existing, b) == []  # exactly 6 h is fine
    # earlier the same day: new shift ends 01:00 before the existing one starts
    assert check([slot(0, "05:00", "05:30", branch=b)], existing, b) != []


def test_six_hours_across_adjacent_days_and_week_wrap():
    a = binfo(1, hours=("09:00", "23:00"))
    b = binfo(2, WHITEFIELD, hours=("04:00", "18:00"))
    # Sunday night at A, Monday early at B: 5 h apart across the week boundary -> rejected
    errors = check([slot(0, "04:00", "08:00", branch=b)], [slot(6, "18:00", "23:00", branch=a)], b)
    assert errors and "5 h" in errors[0] and "Sunday" in errors[0]
    # exactly 6 h across the wrap -> accepted
    assert check([slot(0, "04:00", "08:00", branch=b)], [slot(6, "18:00", "22:00", branch=a)], b) == []
    # adjacent weekdays (Tue night -> Wed morning)
    assert check([slot(2, "04:00", "06:00", branch=b)], [slot(1, "20:00", "23:00", branch=a)], b) != []


def test_same_brand_distance_limit():
    a = binfo(1)
    far = binfo(2, north_of(INDIRANAGAR, 50.2))
    ok49 = binfo(3, north_of(INDIRANAGAR, 49.0))
    existing = [slot(0, "06:00", "08:00", branch=a)]
    assert 49.0 - 0.01 < haversine_km(*INDIRANAGAR, ok49.lat, ok49.lon) < 49.0 + 0.01
    errors = check([slot(3, "10:00", "12:00", branch=far)], existing, far)
    assert len(errors) == 1 and "50.2 km apart" in errors[0]
    assert check([slot(3, "10:00", "12:00", branch=ok49)], existing, ok49) == []
    # exactly the limit is rejected (>= 50 km)
    exact = haversine_km(*INDIRANAGAR, far.lat, far.lon)
    errs = check_new_shifts([slot(3, "10:00", "12:00", branch=far)], existing, work_mode=WorkMode.BOTH,
                            employee_gym_id=1, branch=far, min_gap_minutes=GAP, max_distance_km=exact)
    assert errs
    # Mysuru (~128 km) is far, but a branch of ANOTHER gym brand is not subject to the 50 km rule
    other_brand = binfo(9, MYSURU, gym_id=2)
    other_existing = [ShiftSlot(0, hm("06:00"), hm("08:00"), "offline", branch_id=9, gym_id=2, lat=MYSURU[0],
                                lon=MYSURU[1], label="a branch of another gym")]
    assert check([slot(3, "10:00", "12:00", branch=a)], other_existing, a) == []
    assert other_brand.gym_id == 2


def test_opening_hours_verification_open_and_work_mode():
    b = binfo(1, hours=("06:00", "20:00"))
    assert any("outside" in e for e in check([slot(0, "05:30", "09:00", branch=b)], [], b))
    assert any("outside" in e for e in check([slot(0, "18:00", "21:00", branch=b)], [], b))
    assert check([slot(0, "06:00", "20:00", branch=b)], [], b) == []
    closed_day = binfo(2)
    closed_day.hours.pop(6)
    assert any("closed on Sundays" in e for e in check([slot(6, "08:00", "10:00", branch=closed_day)], [], closed_day))
    unverified = binfo(3, verified=False)
    assert any("not verified" in e for e in check([slot(0, "08:00", "10:00", branch=unverified)], [], unverified))
    shut = binfo(4, is_open=False)
    assert any("currently closed" in e for e in check([slot(0, "08:00", "10:00", branch=shut)], [], shut))
    assert any("online only" in e for e in check([slot(0, "08:00", "10:00", branch=b)], [], b, work_mode=WorkMode.ONLINE))
    assert any("branch only" in e for e in check([slot(0, "08:00", "10:00")], [], None, work_mode=WorkMode.OFFLINE))
    foreign = binfo(5, gym_id=99)
    assert check([slot(0, "08:00", "10:00", branch=foreign)], [], foreign) == ["That branch belongs to a different gym."]


def test_online_shifts_only_need_to_not_overlap():
    a, b = binfo(1), binfo(2, WHITEFIELD)
    existing = [slot(0, "06:00", "10:00", branch=a)]
    assert any("overlaps" in e for e in check([slot(0, "09:00", "11:00")], existing, None))
    assert check([slot(0, "10:00", "12:00")], existing, None) == []  # adjacent online is fine (no travel)
    # an online shift between two branch shifts does not trigger the 6 h rule
    assert check([slot(0, "12:00", "13:00")], existing + [slot(0, "18:00", "19:00", branch=b)], None) == []
    # online vs online overlap
    assert any("overlaps" in e for e in check([slot(2, "18:00", "20:00")], [slot(2, "19:00", "21:00")], None))


def test_parse_shift_form_validation():
    req, errors, _ = S.parse_shift_form({"start_time": "10:00", "end_time": "09:00", "mode": "offline"})
    assert req is None
    assert "Pick at least one weekday." in errors
    assert any("end after it starts" in e for e in errors)
    assert any("branch" in e for e in errors)
    req, errors, _ = S.parse_shift_form({"weekdays": "9", "start_time": "25:00", "end_time": "x", "mode": "online"})
    assert req is None and "Choose valid weekdays." in errors and len(errors) >= 3
    req, errors, values = S.parse_shift_form({"weekdays": "2", "start_time": "06:30", "end_time": "08:00", "mode": "online"})
    assert errors == [] and req is not None and req.weekdays == [2] and req.branch_id is None


# ====================================================================== HTTP flow
async def _setup(db, *, work_mode: WorkMode = WorkMode.BOTH, linked: bool = True):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    a = await make_branch(db, gym, name="Indiranagar", coords=INDIRANAGAR)
    b = await make_branch(db, gym, name="Whitefield", coords=WHITEFIELD)
    user = await make_user(db, role=Role.STAFF) if linked else None
    emp = await make_employee(db, gym, user=user, work_mode=work_mode,
                              status=EmploymentStatus.ACTIVE if linked else EmploymentStatus.INVITED)
    return owner, gym, a, b, emp


async def _add(client, emp_id: int, days: list[int], start: str, end: str, branch=None):
    data = {"weekdays": [str(d) for d in days], "start_time": start, "end_time": end,
            "mode": "offline" if branch is not None else "online"}
    if branch is not None:
        data["branch_id"] = str(branch.id)
    return await post_form(client, f"/owner/staff/{emp_id}/shifts", data)


async def _count_shifts(db, emp_id: int) -> int:
    return int(await db.scalar(select(func.count(TrainerShift.id)).where(TrainerShift.employee_id == emp_id)))


async def test_owner_adds_shifts_for_several_weekdays_and_staff_is_notified(client, db):
    owner, gym, a, b, emp = await _setup(db)
    await login_as(client, owner)
    r = await _add(client, emp.id, [0, 2, 4], "06:00", "10:00", a)
    assert r.status_code == 303 and r.headers["location"].endswith("tab=schedule")
    assert await _count_shifts(db, emp.id) == 3
    note = await db.scalar(select(Notification).where(Notification.user_id == emp.user_id))
    assert note is not None and "Mon, Wed, Fri 06:00–10:00" in note.body
    page = await client.get(f"/owner/staff/{emp.id}?tab=schedule")
    assert page.status_code == 200 and "Indiranagar" in page.text and "06:00–10:00" in page.text


async def test_http_rules_reject_with_specific_messages(client, db):
    owner, gym, a, b, emp = await _setup(db)
    await make_shift(db, emp, branch=a, weekday=0, start="06:00", end="10:00")
    await login_as(client, owner)
    r = await _add(client, emp.id, [0], "15:00", "19:00", b)  # 5 h after Indiranagar
    assert r.status_code == 400 and "only 5 h away" in r.text and "at least 6 h apart" in r.text
    r = await _add(client, emp.id, [0], "16:00", "20:00", b)  # exactly 6 h
    assert r.status_code == 303
    r = await _add(client, emp.id, [0], "09:00", "11:00")  # online overlapping a branch shift
    assert r.status_code == 400 and "overlaps" in r.text
    r = await _add(client, emp.id, [1], "05:00", "09:00", a)  # before opening
    assert r.status_code == 400 and "outside Indiranagar" in r.text
    assert await _count_shifts(db, emp.id) == 2


async def test_http_sunday_to_monday_wrap(client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    late = await make_branch(db, gym, name="Late club", hours=("09:00", "23:00"))
    early = await make_branch(db, gym, name="Early club", coords=WHITEFIELD, hours=("04:00", "18:00"))
    emp = await make_employee(db, gym)
    await make_shift(db, emp, branch=late, weekday=6, start="18:00", end="23:00")
    await login_as(client, owner)
    r = await _add(client, emp.id, [0], "04:00", "08:00", early)
    assert r.status_code == 400 and "Sunday 18:00–23:00" in r.text and "5 h" in r.text
    r = await _add(client, emp.id, [0], "05:00", "08:00", early)
    assert r.status_code == 303


async def test_http_same_brand_distance_and_unverified_branch(client, db):
    owner, gym, a, b, emp = await _setup(db)
    far = await make_branch(db, gym, name="Mysuru", coords=MYSURU, city="Mysuru")
    near49 = await make_branch(db, gym, name="Hosur Road", coords=north_of(INDIRANAGAR, 49.0))
    draft = await make_branch(db, gym, name="Koramangala draft", verified=False)
    await make_shift(db, emp, branch=a, weekday=0, start="06:00", end="08:00")
    await login_as(client, owner)
    r = await _add(client, emp.id, [3], "10:00", "12:00", far)
    assert r.status_code == 400 and "km apart" in r.text and "50 km or more" in r.text
    r = await _add(client, emp.id, [3], "10:00", "12:00", draft)
    assert r.status_code == 400 and "not verified" in r.text
    r = await _add(client, emp.id, [3], "10:00", "12:00", near49)
    assert r.status_code == 303


async def test_http_overlap_across_gyms_for_the_same_person(client, db):
    owner, gym, a, b, emp = await _setup(db)
    other_owner = await make_owner(db)
    other_gym = await make_gym(db, other_owner)
    other_branch = await make_branch(db, other_gym, name="Elsewhere", coords=WHITEFIELD)
    user = await db.get(User, emp.user_id)
    second = await make_employee(db, other_gym, user=user)
    await make_shift(db, second, branch=other_branch, weekday=4, start="08:00", end="12:00")
    await login_as(client, owner)
    r = await _add(client, emp.id, [4], "10:00", "14:00", a)
    assert r.status_code == 400 and "overlaps" in r.text and "a branch of another gym" in r.text
    assert "Elsewhere" not in r.text  # the other gym's branch name stays private
    r = await _add(client, emp.id, [4], "13:00", "15:00", a)  # 1 h after the other gym's shift
    assert r.status_code == 400 and "at least 6 h apart" in r.text
    r = await _add(client, emp.id, [4], "18:00", "19:00", a)
    assert r.status_code == 303


async def test_http_branch_of_other_gym_and_work_mode(client, db):
    owner, gym, a, b, emp = await _setup(db, work_mode=WorkMode.ONLINE)
    my_other_gym = await make_gym(db, owner)
    foreign = await make_branch(db, my_other_gym, name="Other brand branch")
    await login_as(client, owner)
    r = await _add(client, emp.id, [1], "08:00", "10:00", foreign)
    assert r.status_code == 400 and "this gym&#39;s branches" in r.text
    r = await _add(client, emp.id, [1], "08:00", "10:00", a)
    assert r.status_code == 400 and "works online only" in r.text
    r = await _add(client, emp.id, [1], "08:00", "10:00")
    assert r.status_code == 303


async def test_shift_permissions_and_delete(client, db, anon_client_factory):
    owner, gym, a, b, emp = await _setup(db)
    shift = await make_shift(db, emp, branch=a, weekday=2, start="06:00", end="10:00")
    intruder = await make_owner(db)
    await login_as(client, intruder)
    r = await _add(client, emp.id, [1], "06:00", "10:00", a)
    assert r.status_code == 404
    r = await post_form(client, f"/owner/staff/{emp.id}/shifts/{shift.id}/delete")
    assert r.status_code == 404
    assert await _count_shifts(db, emp.id) == 1
    staff_client = anon_client_factory("203.0.113.40")
    await login_as(staff_client, await db.get(User, emp.user_id))
    r = await _add(staff_client, emp.id, [1], "06:00", "10:00", a)
    assert r.status_code == 403
    await login_as(client, owner)
    r = await post_form(client, f"/owner/staff/{emp.id}/shifts/{shift.id}/delete")
    assert r.status_code == 303 and await _count_shifts(db, emp.id) == 0


async def test_shifts_for_unlinked_employee_use_their_own_shifts(client, db):
    owner, gym, a, b, emp = await _setup(db, linked=False)
    await make_shift(db, emp, branch=a, weekday=0, start="06:00", end="10:00")
    await login_as(client, owner)
    r = await _add(client, emp.id, [0], "08:00", "09:00")
    assert r.status_code == 400 and "overlaps" in r.text
    r = await _add(client, emp.id, [0], "10:00", "11:00")
    assert r.status_code == 303


@pytest.mark.parametrize("n_existing", [1, 6])
async def test_validate_shift_request_constant_queries(db, n_existing):
    owner, gym, a, b, emp = await _setup(db)
    for day in range(n_existing):
        await make_shift(db, emp, branch=a, weekday=day, start="06:00", end="07:00")
    req, errors, _ = S.parse_shift_form({"weekdays": ["0", "1", "2", "3", "4", "5", "6"], "start_time": "12:00",
                                         "end_time": "13:00", "mode": "offline", "branch_id": str(b.id)})
    assert not errors
    with QueryCounter() as qc:
        rows, errs = await S.validate_shift_request(db, emp, req)
    assert errs  # Whitefield 5 h after the morning Indiranagar shifts
    # lock + branch + hours + person's shifts: independent of how many shifts exist
    assert qc.count == 4
    await db.rollback()
