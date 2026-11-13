"""app/core/pagination.py + the shared pager macro (``ui.pager``): every list is bounded in SQL (LIMIT), never sliced
in Python, with a constant number of statements per page whatever the row count."""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, event, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import QueryParams

from app import db as dbmod
from app.core.pagination import (
    MAX_PAGE,
    Page,
    PageRequest,
    keyset,
    page_url,
    paginate,
    parse_keyset,
    parse_page,
)
from app.core.templating import env
from app.db import QueryCounter, get_db
from app.models import AuditLog, Notification
from app.models.enums import NotificationCategory, Severity
from tests.factories import make_customer

KIND = "pagination-test"


# ============================================================================================ helpers
class Statements:
    """Every SQL statement (and its parameters) sent while the block runs."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

    def _on(self, conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        self.sent.append((statement, parameters))

    def __enter__(self) -> "Statements":
        event.listen(dbmod.get_engine().sync_engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(dbmod.get_engine().sync_engine, "before_cursor_execute", self._on)

    @property
    def sql(self) -> list[str]:
        return [s for s, _ in self.sent]


async def seed_audit(db: AsyncSession, n: int) -> list[int]:
    await db.execute(insert(AuditLog), [
        {"action": "test.row", "entity_type": KIND, "entity_id": i, "detail": {}} for i in range(1, n + 1)
    ])
    await db.commit()
    return list((await db.execute(select(AuditLog.id).where(AuditLog.entity_type == KIND).order_by(AuditLog.id))).scalars())


def audit_rows():  # noqa: ANN201
    return select(AuditLog).where(AuditLog.entity_type == KIND).order_by(AuditLog.id.desc())


def fake_request(path: str = "/things", query: str = "") -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "query_string": query.encode(), "headers": [],
                    "server": ("testserver", 80), "scheme": "http", "root_path": ""})


def strip_svg(html: str) -> str:
    return re.sub(r"<svg.*?</svg>", "", html, flags=re.S)


# ============================================================================================ parse_page
@pytest.mark.parametrize("raw, expected", [
    (None, 1), ("3", 3), (" 7 ", 7), ("0", 1), ("-2", 1), ("abc", 1), ("1.5", 1), ("", 1), ("2e3", 1),
    ("²", 1), ("٣", 1),  # superscript two / Arabic-Indic three: digits for str.isdigit, junk for us
    ("99999999999999999999999999999", MAX_PAGE), (str(MAX_PAGE + 5), MAX_PAGE),
])
def test_parse_page_reads_page_safely(raw: str | None, expected: int) -> None:
    params = {} if raw is None else {"page": raw}
    assert parse_page(params).page == expected


@pytest.mark.parametrize("raw, expected", [
    (None, 20), ("10", 10), ("500", 50), ("0", 20), ("-1", 20), ("x", 20), ("50", 50), ("9" * 40, 50),
])
def test_parse_page_clamps_per_page(raw: str | None, expected: int) -> None:
    params = {} if raw is None else {"per_page": raw}
    pr = parse_page(params, default_per_page=20, max_per_page=50)
    assert pr.per_page == expected and pr.page == 1


def test_parse_page_accepts_a_request_or_query_params() -> None:
    pr = parse_page(fake_request(query="page=4&per_page=30&q=whey"), 25, 100)
    assert pr == PageRequest(page=4, per_page=30) and pr.offset == 90
    assert parse_page(QueryParams("page=2&page=9"), 25, 100).page in (2, 9)  # a repeated key never errors
    assert parse_page({}, default_per_page=500, max_per_page=40).per_page == 40  # the default is clamped too
    assert parse_page({"page": "5"}, max_page=3).page == 3


# ============================================================================================ Page.window
def page_of(page: int, pages: int, per_page: int = 25) -> Page:
    return Page(items=[], total=pages * per_page, page=page, per_page=per_page)


@pytest.mark.parametrize("page, expected", [
    (6, [1, None, 4, 5, 6, 7, 8, None, 40]),  # 1 … 4 5 [6] 7 8 … 40
    (1, [1, 2, 3, None, 40]),
    (40, [1, None, 38, 39, 40]),
    (4, [1, 2, 3, 4, 5, 6, None, 40]),
    (5, [1, 2, 3, 4, 5, 6, 7, None, 40]),  # a one-page gap shows the page, not an ellipsis
    (37, [1, None, 35, 36, 37, 38, 39, 40]),
])
def test_window_of_forty_pages(page: int, expected: list[int | None]) -> None:
    assert page_of(page, 40).window() == expected


def test_window_never_lists_every_page() -> None:
    assert page_of(1, 1).window() == [1]
    assert page_of(3, 5).window() == [1, 2, 3, 4, 5]
    for current in range(1, 1001):
        w = page_of(current, 1000).window()
        numbers = [n for n in w if n is not None]
        assert len(w) <= 2 * 2 + 5, (current, w)
        assert current in numbers and numbers[0] == 1 and numbers[-1] == 1000
        assert numbers == sorted(set(numbers))
    assert len(page_of(500, 1000).window(radius=1)) == 7


def test_page_properties() -> None:
    p = Page(items=list(range(20)), total=150, page=2, per_page=20)
    assert (p.pages, p.has_prev, p.has_next, p.prev_page, p.next_page) == (8, True, True, 1, 3)
    assert (p.offset, p.first, p.last, len(p)) == (20, 21, 40, 20)
    assert list(p) == list(range(20))
    last = Page(items=[1], total=1, page=1, per_page=20)
    assert (last.pages, last.has_prev, last.has_next, last.prev_page, last.next_page) == (1, False, False, None, None)
    empty = Page(items=[], total=0, page=1, per_page=20)
    assert empty.pages == 1 and empty.first == 0 and not empty
    assert p.meta() == {"page": 2, "pages": 8, "per_page": 20, "total": 150, "total_capped": False,
                        "has_prev": True, "has_next": True}


def test_page_url_keeps_the_other_query_parameters() -> None:
    assert page_url("/x", {"q": "whey protein", "status": "paid", "page": "4", "empty": "", "none": None}, 3) == \
        "/x?q=whey+protein&status=paid&page=3"
    assert page_url("/x", {"q": "a"}, 1) == "/x?q=a"  # page 1 has a single canonical URL
    assert page_url("/x", None, 1) == "/x"
    assert page_url("/x", {"tag": ["a", "b"]}, 2) == "/x?tag=a&tag=b&page=2"
    assert page_url("/x", QueryParams("tag=a&tag=b&page=9&per_page=50"), 2) == "/x?tag=a&tag=b&per_page=50&page=2"
    assert page_url("/x", {"q": "<a&b>"}, 2) == "/x?q=%3Ca%26b%3E&page=2"


# ============================================================================================ paginate
async def test_paginate_loads_one_page_of_150_rows_with_two_statements(db: AsyncSession) -> None:
    ids = await seed_audit(db, 150)
    with QueryCounter() as qc, Statements() as st:
        page = await paginate(db, audit_rows(), page=2, per_page=20)
    assert qc.count == 2  # COUNT + one LIMIT/OFFSET page
    assert [r.id for r in page.items] == list(reversed(ids))[20:40]
    assert (page.total, page.pages, page.page, page.first, page.last) == (150, 8, 2, 21, 40)
    count_sql, rows_sql = st.sql
    assert "count(*)" in count_sql.lower()
    assert re.search(r"\bLIMIT\b", rows_sql) and re.search(r"\bOFFSET\b", rows_sql)  # bounded in SQL ...
    assert list(st.sent[1][1]) == [KIND, 20, 20]  # ... with the page size and offset bound
    assert all(isinstance(r, AuditLog) for r in page.items)


async def test_paginate_statement_count_is_constant(db: AsyncSession) -> None:
    await seed_audit(db, 15)
    with QueryCounter() as small:
        first = await paginate(db, audit_rows(), page=1, per_page=20)
    await db.execute(delete(AuditLog))
    await db.commit()
    await seed_audit(db, 150)
    with QueryCounter() as big:
        second = await paginate(db, audit_rows(), page=1, per_page=20)
    assert small.count == big.count == 2
    assert len(first.items) == 15 and len(second.items) == 20


async def test_paginate_clamps_page_and_per_page(db: AsyncSession) -> None:
    await seed_audit(db, 150)
    past_end = await paginate(db, audit_rows(), page=999, per_page=20)
    assert (past_end.page, len(past_end.items), past_end.has_next) == (8, 10, False)
    huge = await paginate(db, audit_rows(), page=1, per_page=10_000, max_per_page=50)
    assert huge.per_page == 50 and len(huge.items) == 50 and huge.pages == 3
    tiny = await paginate(db, audit_rows(), page=-4, per_page=0)
    assert (tiny.page, tiny.per_page, len(tiny.items)) == (1, 1, 1)
    none = await paginate(db, select(AuditLog).where(AuditLog.entity_type == "nothing").order_by(AuditLog.id),
                          page=3, per_page=20)
    assert (none.items, none.total, none.page, none.pages) == ([], 0, 1, 1)


async def test_paginate_window_count_uses_a_single_statement(db: AsyncSession) -> None:
    await seed_audit(db, 150)
    plain = await paginate(db, audit_rows(), page=3, per_page=20)
    with QueryCounter() as qc, Statements() as st:
        windowed = await paginate(db, audit_rows(), page=3, per_page=20, window_count=True)
    assert qc.count == 1 and "over ()" in st.sql[0].lower() and re.search(r"\bLIMIT\b", st.sql[0])
    assert [r.id for r in windowed.items] == [r.id for r in plain.items] and windowed.total == 150
    with QueryCounter() as qc:
        past_end = await paginate(db, audit_rows(), page=50, per_page=20, window_count=True)
    assert qc.count == 3 and past_end.page == 8 and len(past_end.items) == 10  # empty page -> count -> last page
    rows = await paginate(db, select(AuditLog.id, AuditLog.entity_id).where(AuditLog.entity_type == KIND)
                          .order_by(AuditLog.id), page=1, per_page=5, window_count=True)
    assert rows.items[0] == (rows.items[0][0], 1) and len(rows.items[0]) == 2  # the total column is stripped


async def test_paginate_count_cap_bounds_the_count(db: AsyncSession) -> None:
    await seed_audit(db, 150)
    with Statements() as st:
        page = await paginate(db, audit_rows(), page=8, per_page=20, count_cap=100)
    assert (page.total, page.total_capped, page.pages, page.page) == (100, True, 5, 5)
    assert re.search(r"\bLIMIT\b", st.sql[0]) and 101 in list(st.sent[0][1])  # the COUNT itself stops at cap + 1
    exact = await paginate(db, audit_rows(), page=1, per_page=20, count_cap=1000)
    assert exact.total == 150 and not exact.total_capped


async def test_paginate_rows_and_a_custom_count(db: AsyncSession) -> None:
    await seed_audit(db, 30)
    stmt = select(AuditLog.id, AuditLog.entity_id).where(AuditLog.entity_type == KIND).order_by(AuditLog.entity_id)
    counted = select(func.count(AuditLog.id)).where(AuditLog.entity_type == KIND)
    with Statements() as st:
        page = await paginate(db, stmt, page=2, per_page=10, count_stmt=counted)
    assert "anon" not in st.sql[0]  # the supplied COUNT ran as given
    assert [row.entity_id for row in page.items] == list(range(11, 21))


# ============================================================================================ keyset
async def seed_notifications(db: AsyncSession, n: int) -> tuple[int, list[int]]:
    user = await make_customer(db)
    await db.execute(insert(Notification), [
        {"user_id": user.id, "title": f"Note {i}", "body": "x", "category": NotificationCategory.SYSTEM,
         "severity": Severity.INFO}
        for i in range(n)
    ])
    await db.commit()
    ids = list((await db.execute(select(Notification.id).where(Notification.user_id == user.id)
                                 .order_by(Notification.id))).scalars())
    return user.id, ids


async def test_keyset_walks_a_feed_one_bounded_window_at_a_time(db: AsyncSession) -> None:
    uid, ids = await seed_notifications(db, 150)
    feed = select(Notification).where(Notification.user_id == uid)
    seen: list[int] = []
    cursor = None
    windows = 0
    while True:
        with QueryCounter() as qc, Statements() as st:
            win = await keyset(db, feed, Notification.id, before_id=cursor, limit=20)
        assert qc.count == 1 and re.search(r"\bLIMIT\b", st.sql[0]) and 21 in list(st.sent[0][1])
        windows += 1
        assert len(win.items) <= 20 and win.newest_first
        seen += [n.id for n in win.items]
        if not win.has_more:
            break
        assert win.next_cursor == win.items[-1].id
        cursor = win.next_cursor
    assert seen == list(reversed(ids)) and windows == 8  # 7 full windows + the last 10
    assert win.meta() == {"has_more": False, "limit": 20, "next_before_id": None}


async def test_keyset_after_id_and_limits(db: AsyncSession) -> None:
    uid, ids = await seed_notifications(db, 150)
    feed = select(Notification).where(Notification.user_id == uid)
    newer = await keyset(db, feed, Notification.id, after_id=ids[100], limit=20)
    assert [n.id for n in newer.items] == ids[101:121] and newer.has_more and not newer.newest_first
    assert newer.meta()["next_after_id"] == ids[120]
    capped = await keyset(db, feed, Notification.id, limit=10_000, max_limit=100)
    assert capped.limit == 100 and len(capped.items) == 100 and capped.has_more
    only_ids = await keyset(db, select(Notification.id).where(Notification.user_id == uid), Notification.id, limit=5)
    assert only_ids.items == list(reversed(ids))[:5] and only_ids.next_cursor == ids[-5]


async def test_keyset_by_created_at_with_an_id_cursor(db: AsyncSession) -> None:
    uid, ids = await seed_notifications(db, 60)  # one transaction: every row has the same created_at (ties -> id)
    feed = select(Notification).where(Notification.user_id == uid)
    with Statements() as st:
        first = await keyset(db, feed, Notification.id, order_col=Notification.created_at, limit=25)
        second = await keyset(db, feed, Notification.id, order_col=Notification.created_at,
                              before_id=first.next_cursor, limit=25)
    assert [n.id for n in first.items] + [n.id for n in second.items] == list(reversed(ids))[:50]
    assert "(notifications.created_at, notifications.id) <" in st.sql[1]
    assert "ORDER BY notifications.created_at DESC, notifications.id DESC" in st.sql[1]
    await db.execute(delete(Notification).where(Notification.id == first.next_cursor))
    await db.commit()
    gone = await keyset(db, feed, Notification.id, order_col=Notification.created_at, before_id=first.next_cursor)
    assert gone.items == [] and not gone.has_more  # a deleted cursor ends the feed
    by_id = await keyset(db, feed, Notification.id, before_id=first.next_cursor, limit=5)
    assert [n.id for n in by_id.items] == list(reversed(ids))[25:30]


@pytest.mark.parametrize("query, expected", [
    ("", (None, None, 20)),
    ("before_id=42&limit=10", (42, None, 10)),
    ("after_id=7", (None, 7, 20)),
    ("before_id=5&after_id=9", (5, None, 20)),  # before wins
    ("before_id=abc&after_id=-3&limit=0", (None, None, 20)),
    ("limit=500", (None, None, 50)),
    ("before_id=" + "9" * 30, (2**63 - 1, None, 20)),
])
def test_parse_keyset(query: str, expected: tuple) -> None:
    k = parse_keyset(fake_request(query=query), default_limit=20, max_limit=50)
    assert (k.before_id, k.after_id, k.limit) == expected


# ============================================================================================ ui.pager
PAGER = env.from_string('{% import "_ui.html" as ui with context %}{{ ui.pager(p, noun="orders") }}')


def render_pager(p: Page, query: str = "", **kwargs: Any) -> str:
    if kwargs:
        tpl = env.from_string('{% import "_ui.html" as ui with context %}{{ ui.pager(p, **kw) }}')
        return strip_svg(tpl.render(request=fake_request("/orders", query), p=p, kw=kwargs, csrf_token="t"))
    return strip_svg(PAGER.render(request=fake_request("/orders", query), p=p, csrf_token="t"))


def test_pager_renders_a_window_and_keeps_the_query() -> None:
    html = render_pager(Page(items=list(range(25)), total=987, page=6, per_page=25), "q=whey+protein&status=paid&page=6")
    assert '<nav class="mt-10 flex flex-col items-center gap-3" aria-label="Pagination" data-pager>' in html
    numbers = re.findall(r'aria-label="Page (\d+)"', html)
    assert numbers == ["1", "4", "5", "6", "7", "8", "40"]  # 40 pages, 7 links: never every page
    assert html.count('aria-current="page"') == 1
    assert re.search(r'aria-label="Page 6" aria-current="page">6<', html)
    assert html.count("…") == 2
    assert 'href="/orders?q=whey+protein&amp;status=paid"' in html  # page 1: no ?page=
    assert 'href="/orders?q=whey+protein&amp;status=paid&amp;page=5" class="btn btn-secondary btn-sm" rel="prev"' in html
    assert 'href="/orders?q=whey+protein&amp;status=paid&amp;page=7" class="btn btn-secondary btn-sm" rel="next"' in html
    assert "Page <span class=\"font-semibold text-ink dark:text-white\">6</span> of 40 · 987 orders" in html
    assert " style=" not in html and "onclick" not in html


def test_pager_edges() -> None:
    assert render_pager(Page(items=[1], total=1, page=1, per_page=25)).strip() == ""  # one page: no pager
    first = render_pager(Page(items=list(range(25)), total=60, page=1, per_page=25))
    assert re.search(r'<span class="btn btn-secondary btn-sm pointer-events-none opacity-40" aria-disabled="true">Previous</span>', first)
    assert 'rel="next"' in first and 'rel="prev"' not in first
    last = render_pager(Page(items=[1], total=51, page=3, per_page=25))
    assert re.search(r'aria-disabled="true">Next</span>', last) and 'rel="prev"' in last
    capped = render_pager(Page(items=list(range(25)), total=10_000, page=1, per_page=25, total_capped=True))
    assert "· 10,000+ orders" in capped


def test_pager_with_explicit_params_path_or_url_builder() -> None:
    p = Page(items=list(range(10)), total=30, page=2, per_page=10)
    html = render_pager(p, "ignored=1", params={"tab": "open"}, path="/elsewhere")
    assert 'href="/elsewhere?tab=open&amp;page=3"' in html and "ignored" not in html
    built = render_pager(p, url=lambda n: f"/feed/{n}")
    assert 'href="/feed/1"' in built and 'href="/feed/3"' in built and "Page <span" not in built


# ============================================================================================ end to end
mini = FastAPI()
LIST = env.from_string(
    '{% import "_ui.html" as ui with context %}<ul>{% for row in page %}<li data-row="{{ row.entity_id }}"></li>'
    '{% endfor %}</ul>{{ ui.pager(page, noun="rows") }}'
)


@mini.get("/rows")
async def rows_page(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    pr = parse_page(request, default_per_page=20, max_per_page=50)
    page = await paginate(db, audit_rows(), page=pr.page, per_page=pr.per_page, max_per_page=50)
    return HTMLResponse(LIST.render(request=request, page=page, csrf_token="t"))


async def test_a_list_route_renders_and_queries_one_page_of_150(db: AsyncSession) -> None:
    await seed_audit(db, 150)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mini), base_url="http://testserver") as c:
        with QueryCounter() as qc, Statements() as st:
            r = await c.get("/rows?q=protein&per_page=500&page=2")
        assert r.status_code == 200 and qc.count == 2
        assert re.search(r"\bLIMIT\b", st.sql[1]) and list(st.sent[1][1])[1:] == [50, 50]
        rows = re.findall(r'data-row="(\d+)"', r.text)
        assert rows == [str(i) for i in range(100, 50, -1)]  # 50 of 150 (per_page clamped), newest first
        assert 'href="/rows?q=protein&amp;per_page=500&amp;page=3"' in r.text  # filters and size survive paging
        assert "Page <span class=\"font-semibold text-ink dark:text-white\">2</span> of 3 · 150 rows" in r.text
        junk = await c.get("/rows?page=%27%3B--&per_page=lots")
        assert junk.status_code == 200 and len(re.findall(r"data-row=", junk.text)) == 20
        assert re.findall(r'data-row="(\d+)"', junk.text)[0] == "150"
        last = await c.get("/rows?page=99999")
        assert re.findall(r'data-row="(\d+)"', last.text) == [str(i) for i in range(10, 0, -1)]
