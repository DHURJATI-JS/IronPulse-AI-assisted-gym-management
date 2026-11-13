"""Shared pagination: numbered pages for lists people browse, keyset windows for feeds (ARCHITECTURE.md §3).

Every list that grows with the data is bounded *in SQL* (LIMIT), never sliced in Python::

    from app.core.pagination import paginate, parse_page

    pr = parse_page(request, default_per_page=25, max_per_page=100)     # ?page= / ?per_page=; junk -> defaults
    page = await paginate(db, select(Order).where(...).order_by(Order.id.desc()), page=pr.page, per_page=pr.per_page)
    return render(request, "orders.html", {"page": page})                # {{ ui.pager(page, noun="orders") }}

* ``paginate`` issues exactly two statements whatever the row count - a COUNT and one LIMIT/OFFSET page - or one with
  ``window_count=True`` (``count(*) OVER ()``). A page past the end shows the last page. ``count_cap=10_000`` stops the
  COUNT early on huge tables (``Page.total_capped``; the pager then says "10,000+").
* Feeds (notifications, chat, activity) use ``keyset`` with ``?before_id=`` / ``?after_id=``: the cost of a page does
  not depend on how far the reader scrolled, and ``has_more`` says whether to offer "Load more".
* Order by a unique key (``..., Model.id``) so OFFSET pages are stable, and index the ORDER BY.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Generic, TypeVar
from urllib.parse import urlencode

from sqlalchemy import Select, func, literal, select, tuple_

T = TypeVar("T")

DEFAULT_PER_PAGE = 25
MAX_PER_PAGE = 100  # hard ceiling for ?per_page= unless a caller passes a smaller one
MAX_PAGE = 100_000  # page numbers beyond this are never meaningful (and OFFSET would be absurd)
DEFAULT_FEED_LIMIT = 20
MAX_FEED_LIMIT = 100
WINDOW_RADIUS = 2  # pages shown either side of the current one: 1 … 4 5 [6] 7 8 … 40
MAX_ID = 2**63 - 1  # BIGINT ids
_MAX_DIGITS = 18  # longer digit strings are clamped without int() parsing them


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def _int_param(raw: Any, *, default: int | None, maximum: int) -> int | None:
    """A positive decimal integer from a query string value. Junk (empty, signs, letters, 0) -> ``default``;
    too large -> ``maximum``."""
    if raw is None:
        return default
    text = str(raw).strip()
    if not text or not text.isascii() or not text.isdigit():
        return default
    if len(text) > _MAX_DIGITS:
        return maximum
    value = int(text)
    if value < 1:
        return default
    return min(value, maximum)


def _query(source: Any) -> Mapping[str, Any]:
    """``request.query_params`` of a Request, or the mapping itself."""
    params = getattr(source, "query_params", source)
    return params if params is not None else {}


# ============================================================================================ numbered pages
@dataclass(frozen=True, slots=True)
class PageRequest:
    """What the visitor asked for (already clamped)."""

    page: int = 1
    per_page: int = DEFAULT_PER_PAGE

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page


def parse_page(request: Any, default_per_page: int = DEFAULT_PER_PAGE, max_per_page: int = MAX_PER_PAGE, *,
               max_page: int = MAX_PAGE) -> PageRequest:
    """Read ``?page=`` and ``?per_page=`` safely from a Request (or any mapping of query values).

    Junk (``abc``, ``-3``, ``0``, ``1.5``, empty) falls back to page 1 / ``default_per_page``; ``per_page`` is clamped to
    ``1..max_per_page`` and ``page`` to ``1..max_page`` (``paginate`` then clamps it to the last page)."""
    max_per_page = max(1, int(max_per_page))
    per_page_default = _clamp(int(default_per_page), 1, max_per_page)
    params = _query(request)
    page = _int_param(params.get("page"), default=1, maximum=max(1, int(max_page)))
    per_page = _int_param(params.get("per_page"), default=per_page_default, maximum=max_per_page)
    return PageRequest(page=page or 1, per_page=per_page or per_page_default)


def page_url(path: str, params: Mapping[str, Any] | None, page: int) -> str:
    """``path`` + the other query parameters + ``page`` (omitted for page 1, so page 1 has one canonical URL).

    ``params`` may be a dict (list values repeat the key) or Starlette ``QueryParams`` (every value is kept). Its own
    ``page`` and empty values are dropped."""
    pairs: list[tuple[str, Any]] = []
    if params is not None:
        multi = getattr(params, "multi_items", None)
        items = multi() if callable(multi) else params.items()
        for key, value in items:
            if key == "page" or value is None or value == "":
                continue
            if isinstance(value, (list, tuple)):
                pairs.extend((key, v) for v in value if v is not None and v != "")
            else:
                pairs.append((key, value))
    if page > 1:
        pairs.append(("page", page))
    return path + ("?" + urlencode(pairs) if pairs else "")


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    """One page of a list plus what a pager needs. Iterating a Page iterates its items."""

    items: list[T]
    total: int
    page: int
    per_page: int
    total_capped: bool = False  # the COUNT stopped at ``count_cap``: ``total`` means "at least"

    @property
    def pages(self) -> int:
        return max(1, math.ceil(self.total / self.per_page)) if self.per_page > 0 else 1

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def prev_page(self) -> int | None:
        return self.page - 1 if self.has_prev else None

    @property
    def next_page(self) -> int | None:
        return self.page + 1 if self.has_next else None

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page

    @property
    def first(self) -> int:
        """1-based position of the first item shown (0 when the page is empty)."""
        return self.offset + 1 if self.items else 0

    @property
    def last(self) -> int:
        return self.offset + len(self.items)

    def window(self, radius: int = WINDOW_RADIUS) -> list[int | None]:
        """Page numbers to link, ``None`` for a skipped run: ``[1, None, 4, 5, 6, 7, 8, None, 40]`` = 1 … 4 5 [6] 7 8 … 40.

        Never every page number: at most ``2 * radius + 5`` entries, whatever the page count. A gap of a single page
        shows that page instead of an ellipsis."""
        pages = self.pages
        current = _clamp(self.page, 1, pages)
        radius = max(0, int(radius))
        lo, hi = max(1, current - radius), min(pages, current + radius)
        out: list[int | None] = []
        if lo > 1:
            out.append(1)
            if lo == 3:
                out.append(2)
            elif lo > 3:
                out.append(None)
        out.extend(range(lo, hi + 1))
        if hi < pages:
            if hi == pages - 2:
                out.append(pages - 1)
            elif hi < pages - 2:
                out.append(None)
            out.append(pages)
        return out

    def url(self, page: int, path: str, params: Mapping[str, Any] | None = None) -> str:
        """Link to ``page`` keeping the other query parameters (see ``page_url``)."""
        return page_url(path, params, page)

    def meta(self) -> dict[str, Any]:
        """Pagination fields for a JSON response."""
        return {"page": self.page, "pages": self.pages, "per_page": self.per_page, "total": self.total,
                "total_capped": self.total_capped, "has_prev": self.has_prev, "has_next": self.has_next}

    def __iter__(self):  # noqa: ANN204
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)


def _single_column(stmt: Select[Any]) -> bool:
    """``select(Model)`` / ``select(Model.id)`` -> scalars; ``select(Model, Other.name)`` -> rows."""
    return len(stmt.column_descriptions) == 1


async def _count(db: Any, stmt: Select[Any], count_stmt: Select[Any] | None, count_cap: int | None) -> tuple[int, bool]:
    if count_stmt is not None:
        total = int(await db.scalar(count_stmt) or 0)
    elif count_cap is not None and count_cap > 0:
        bounded = stmt.order_by(None).limit(count_cap + 1).subquery()
        total = int(await db.scalar(select(func.count()).select_from(bounded)) or 0)
    else:
        total = int(await db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0)
    if count_cap is not None and count_cap > 0 and total > count_cap:
        return count_cap, True
    return total, False


async def paginate(db: Any, stmt: Select[Any], *, page: int = 1, per_page: int = DEFAULT_PER_PAGE,
                   max_per_page: int = MAX_PER_PAGE, count_stmt: Select[Any] | None = None, window_count: bool = False,
                   count_cap: int | None = None, unique: bool = False, max_page: int = MAX_PAGE) -> Page[Any]:
    """One page of ``stmt`` (an unpaged SELECT with its ORDER BY) as a ``Page``.

    * ``page`` / ``per_page`` are clamped (``1..max_per_page``; a page past the end becomes the last page).
    * Default: ``SELECT count(*)`` of ``stmt`` (or ``count_stmt``, a cheaper count you supply) + one LIMIT/OFFSET query.
      ``window_count=True``: a single query carrying ``count(*) OVER ()`` (a second one only when the page is past the
      end); not for statements that joinedload collections (use selectinload).
    * ``count_cap``: count at most this many rows (``Page.total_capped``) - bounds the COUNT on very large tables.
    * Items are ORM objects / scalars for a single-entity or single-column select, else rows (window mode: tuples).
      ``unique=True`` when the statement joinedloads a collection."""
    per_page = _clamp(int(per_page), 1, max(1, int(max_per_page)))
    page = _clamp(int(page), 1, max(1, int(max_page)))
    scalars = _single_column(stmt)
    if window_count:
        return await _paginate_window(db, stmt, page=page, per_page=per_page, scalars=scalars, unique=unique,
                                      count_cap=count_cap)
    total, capped = await _count(db, stmt, count_stmt, count_cap)
    page = min(page, max(1, math.ceil(total / per_page)))
    result = await db.execute(stmt.limit(per_page).offset((page - 1) * per_page))
    if unique:
        result = result.unique()
    items = list(result.scalars().all()) if scalars else list(result.all())
    return Page(items=items, total=total, page=page, per_page=per_page, total_capped=capped)


async def _paginate_window(db: Any, stmt: Select[Any], *, page: int, per_page: int, scalars: bool, unique: bool,
                           count_cap: int | None) -> Page[Any]:
    counted = stmt.add_columns(func.count().over().label("page_total"))

    async def fetch(p: int) -> list[Any]:
        result = await db.execute(counted.limit(per_page).offset((p - 1) * per_page))
        if unique:
            result = result.unique()
        return list(result.all())

    rows = await fetch(page)
    if not rows and page > 1:  # past the end: learn the total, then show the last page
        total, _ = await _count(db, stmt, None, None)
        page = max(1, math.ceil(total / per_page))
        rows = await fetch(page) if total else []
    total = int(rows[0][-1]) if rows else 0
    items = [row[0] for row in rows] if scalars else [tuple(row)[:-1] for row in rows]
    capped = bool(count_cap) and total > int(count_cap or 0)
    return Page(items=items, total=int(count_cap) if capped else total, page=page, per_page=per_page,
                total_capped=capped)


# ============================================================================================ keyset feeds
@dataclass(frozen=True, slots=True)
class KeysetRequest:
    before_id: int | None = None
    after_id: int | None = None
    limit: int = DEFAULT_FEED_LIMIT


def parse_keyset(request: Any, default_limit: int = DEFAULT_FEED_LIMIT, max_limit: int = MAX_FEED_LIMIT) -> KeysetRequest:
    """Read ``?before_id=`` / ``?after_id=`` / ``?limit=`` safely (junk ids are ignored, ``limit`` clamped to
    ``1..max_limit``). ``before_id`` wins when both are given."""
    max_limit = max(1, int(max_limit))
    params = _query(request)
    before = _int_param(params.get("before_id"), default=None, maximum=MAX_ID)
    after = None if before is not None else _int_param(params.get("after_id"), default=None, maximum=MAX_ID)
    default = _clamp(int(default_limit), 1, max_limit)
    limit = _int_param(params.get("limit"), default=default, maximum=max_limit) or default
    return KeysetRequest(before_id=before, after_id=after, limit=limit)


@dataclass(frozen=True, slots=True)
class KeysetPage(Generic[T]):
    """A window of a feed. ``newest_first`` windows (no cursor or ``before_id``) run newest -> oldest; ``after_id``
    windows run oldest -> newest (new items since the last poll). ``has_more``: more items lie beyond ``next_cursor``
    in the same direction."""

    items: list[T]
    has_more: bool
    limit: int
    newest_first: bool
    next_cursor: int | None  # id of the last item: pass it as before_id (newest_first) or after_id to continue

    def meta(self) -> dict[str, Any]:
        key = "next_before_id" if self.newest_first else "next_after_id"
        return {"has_more": self.has_more, "limit": self.limit, key: self.next_cursor if self.has_more else None}

    def __iter__(self):  # noqa: ANN204
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)


def _default_cursor(item: Any, name: str) -> int | None:
    value = getattr(item, name, None)
    if isinstance(value, int):
        return value
    if isinstance(item, int):  # select(Model.id)
        return item
    if hasattr(item, "_mapping"):  # a Row: the first element carrying the id attribute
        for part in item:
            value = getattr(part, name, None)
            if isinstance(value, int):
                return value
        value = item._mapping.get(name)
        return value if isinstance(value, int) else None
    return None


async def keyset(db: Any, stmt: Select[Any], id_col: Any, *, before_id: int | None = None, after_id: int | None = None,
                 limit: int = DEFAULT_FEED_LIMIT, max_limit: int = MAX_FEED_LIMIT, order_col: Any = None,
                 cursor_of: Callable[[Any], int | None] | None = None, unique: bool = False) -> KeysetPage[Any]:
    """One window of a feed: ``WHERE id < :before_id ORDER BY id DESC LIMIT limit + 1`` (or ``id > :after_id ... ASC``).

    ``stmt`` is the unpaged, unordered SELECT with its filters; ``id_col`` its unique, increasing key (``Model.id``).
    ``order_col`` (e.g. ``Model.created_at``, NOT NULL) orders by ``(order_col, id)`` instead - the cursor is still an
    id; its ``order_col`` value is read by a one-row subquery, so an index on ``(..., order_col, id)`` serves the window.
    A cursor whose row no longer exists ends the feed. One statement per window, whatever the depth."""
    limit = _clamp(int(limit), 1, max(1, int(max_limit)))
    newest_first = after_id is None
    cursor = before_id if newest_first else after_id
    keys = (order_col, id_col) if order_col is not None else (id_col,)
    if cursor is not None:
        cursor = int(cursor)
        if order_col is not None:
            pivot = select(order_col).where(id_col == cursor).correlate(None).scalar_subquery()
            here, there = tuple_(order_col, id_col), tuple_(pivot, literal(cursor, type_=id_col.type))
            stmt = stmt.where(here < there if newest_first else here > there)
        else:
            stmt = stmt.where(id_col < cursor if newest_first else id_col > cursor)
    stmt = stmt.order_by(None).order_by(*(k.desc() if newest_first else k.asc() for k in keys)).limit(limit + 1)
    result = await db.execute(stmt)
    if unique:
        result = result.unique()
    rows = list(result.scalars().all()) if _single_column(stmt) else list(result.all())
    has_more = len(rows) > limit
    items = rows[:limit]
    name = getattr(id_col, "key", None) or getattr(id_col, "name", "id")
    last = items[-1] if items else None
    next_cursor = (cursor_of(last) if cursor_of else _default_cursor(last, name)) if last is not None else None
    return KeysetPage(items=items, has_more=has_more, limit=limit, newest_first=newest_first, next_cursor=next_cursor)
