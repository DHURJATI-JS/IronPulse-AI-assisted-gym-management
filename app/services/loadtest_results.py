"""Load-test results: the ``summary.json`` schema (version 1) and a safe, cached loader for the super-admin page.

The load-test kit (loadtest/, see loadtest/README.md) writes one folder per run::

    loadtest/results/<run_id>/summary.json        <- this schema (the only file the page parses)
                              report.md           <- downloadable (named by files.report_md)
                              locust_report.html  <- Locust's own report, downloadable (named by files.locust_html;
                                                     never rendered inline)
                              *.csv, monitor.csv, server.log ...  (ignored by the page)

The page is GET /superadmin/load-test (app/routers/superadmin_loadtest.py, docs/load-test-page.md). The folder that
holds the runs is ``Settings.loadtest_results_dir`` (env LOADTEST_RESULTS_DIR; unset = <project root>/loadtest/results,
a relative value is relative to the project root).

summary.json - schema version 1
===============================
One UTF-8 JSON object, at most 5 MB. Unknown keys are ignored everywhere (add what you like). Every number must be
finite: NaN, Infinity and overflowing literals such as 1e999 are rejected (write null for a value that was not
measured; Python: ``json.dumps(summary, allow_nan=False)``). Text is cleaned (control characters removed, whitespace
collapsed) and cut to the length given below (``text<=N``) - never rejected for being long. Lists longer than their
cap are rejected (downsample before writing). Times are ISO 8601 between 2000-01-01 and 2100-01-01 (UTC); without an
offset they are read as UTC. Units are in the names: ``_ms`` milliseconds, ``_s`` seconds since the run started,
``_mb`` MiB, ``_pct`` percent 0-100, ``_share`` / ``error_rate`` a FRACTION 0-1 (0.0042 = 0.42 %), ``rps`` requests
per second. ``?`` marks a field that may be null or left out.

    schema          1 (required; any other version is shown as "unsupported")
    run_id          text<=64 - informational; the page identifies a run by its folder name
    started_at      ISO time      ended_at   ISO time (>= started_at)      duration_s   seconds >= 0
    target          {host text<=200, workers int 1-1024, environment text<=40, redis bool,
                     notes? text<=2000, db_rows? {table text<=64: row count} (<= 100 tables)}
    machine?        {cpus int >= 1, memory_gb >= 0, os text<=120, python text<=40, locust_version text<=40}
    verdict         {passed bool, headline text<=300,
                     slos? [{name text<=120, target text<=60, actual text<=60, passed bool,
                             users? int - the load step this SLO is measured at}] (<= 50)}
    capacity        {max_users_within_slo int >= 0, max_rps_within_slo >= 0, knee_users? int, note? text<=1000}
    totals          {requests int, failures int (<= requests), error_rate 0-1, rps, p50_ms, p95_ms, p99_ms, max_ms}
    steps           [{users int, start_s, end_s (>= start_s), rps, error_rate 0-1, p50_ms?, p95_ms?, p99_ms?,
                      slo_p95_ms?, passed bool}] (<= 100; shown in start_s order)
    timeline        [{t_s, users int, rps, p50_ms?, p95_ms?, failures_per_s? (0), cpu_pct?, rss_mb?,
                      db_connections? int}] (one point every 5-10 s, <= 2,000; shown in t_s order)
    endpoints       [{name text<=200, method text<=10, group? text<=60, requests int, failures int (<= requests),
                      p50_ms?, p95_ms?, p99_ms?, max_ms?, rps}] (<= 500)
    classes?        [{name text<=80, weight_share 0-1, requests int, failures int (<= requests), p95_ms?}] (<= 50)
    abuse?          {requests int, throttled int (<= requests), throttled_share 0-1 (higher is better), note? text<=1000}
    server?         {peak_cpu_pct?, peak_rss_mb?, rss_growth_mb_per_min? (may be negative), peak_db_connections? int,
                     pool_size? int, errors_5xx? int (0), timeouts? int (0), log_errors? [text<=300] (<= 50)}
    findings?       [{severity "high"|"medium"|"low", title text<=200, detail? text<=2000,
                      status "fixed"|"open"|"accepted"}] (<= 100)
    files?          {report_md? name, locust_html? name} - bare file names inside the run folder
                    (^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$: no folders, no "..")

Which requests the latencies cover: ``totals``, ``endpoints``, ``timeline`` and the steps' ``rps`` / ``error_rate`` /
``p50_ms`` / ``p95_ms`` / ``p99_ms`` cover every request of their window as Locust counted it. ``steps[].slo_p95_ms``
is the p95 of only the requests the p95 latency SLO judges, when that is a subset (this kit: page requests, abusers
excluded); when any step has it, the page's p95 step chart plots it against the SLO line and the steps table adds an
"SLO p95" column, so chart, table, KPI tile and verdict agree. Leave it out when the SLO judges every request.

``cpu_pct`` is the share of the whole machine (0-100; per-process percentages summed and divided by the CPU count).
Write the keys ``schema``, ``run_id`` and ``started_at`` first: the run list sorts runs it has not parsed yet by a
``started_at`` read from the first 4 KB of the file (else by the folder's modification time). Write summary.json
atomically (write a temporary file, then rename it) so the page never reads half a file. A complete example is in
docs/load-test-page.md (tests validate it against these models).

Loader rules
============
* a run is a sub-folder of the results folder whose name matches ``RUN_ID_RE`` and that holds a summary.json;
  the folder, summary.json and every downloaded file must resolve (symlinks followed) inside the results folder -
  anything that escapes is treated as missing;
* files are opened as they were checked: no final symlink (O_NOFOLLOW), never blocking on a FIFO (O_NONBLOCK), and
  the open file must be the regular file (device, inode) that was checked - a file swapped in between is refused;
* the run list scans at most ``MAX_SCAN`` folder entries, sorts runs newest first by started_at (fallback: folder
  modification time) and returns at most ``MAX_LISTED``; only those are parsed;
* summary.json is read with a ``MAX_SUMMARY_BYTES`` cap; unreadable, oversized, non-JSON, non-finite or
  schema-invalid files become an error state of that run (``LoadedRun.error``, at most ``MAX_ERROR_CHARS``), never an
  exception;
* parsed runs are cached by (path, mtime, size, inode, ctime) in an LRU of ``CACHE_SIZE``: a rewritten or replaced
  file is parsed again, even when it has the same size and its modification time was preserved;
* downloads: only ``files.report_md`` / ``files.locust_html`` of a readable run, regular files of at most
  ``MAX_DOWNLOAD_BYTES``.

Everything here is synchronous file work: the router calls it in a worker thread (``run_in_threadpool``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, BinaryIO, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.config import BASE_DIR, get_settings

log = logging.getLogger("ironpulse.loadtest")

SCHEMA_VERSION = 1
SUMMARY_FILE = "summary.json"
DEFAULT_RESULTS_DIR = BASE_DIR / "loadtest" / "results"
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")  # always used with fullmatch()
FILE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
MAX_SCAN = 500  # folder entries examined per run list
MAX_LISTED = 20  # runs offered in the run picker
MAX_SUMMARY_BYTES = 5 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
MAX_ERROR_CHARS = 600  # a run's error message (shown on its error card, kept in the caches)
CACHE_SIZE = 8  # parsed summaries kept in memory
HEADER_CACHE_SIZE = 1024  # tiny per-run headers (run picker) and started_at peeks
PEEK_BYTES = 4096
TIME_MIN = datetime(2000, 1, 1, tzinfo=UTC)  # started_at / ended_at (and the times shown for a run) lie in this range
TIME_MAX = datetime(2100, 1, 1, tzinfo=UTC)
# the two downloads: URL file name -> (summary.files attribute, media type)
DOWNLOADS: dict[str, tuple[str, str]] = {
    "report.md": ("report_md", "text/markdown; charset=utf-8"),
    "locust.html": ("locust_html", "text/html; charset=utf-8"),
}

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_PEEK_STARTED = re.compile(rb'"started_at"\s*:\s*"([0-9T:.+\-Z ]{10,40})"')
# open exactly what was checked: no final symlink, no blocking on a FIFO, binary on Windows
_OPEN_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
               | getattr(os, "O_BINARY", 0))


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ===================================================================================== settings
def results_dir() -> Path:
    """The folder holding one sub-folder per run (``Settings.loadtest_results_dir``; None = loadtest/results)."""
    configured = getattr(get_settings(), "loadtest_results_dir", None)
    if configured is None or str(configured).strip() in ("", "."):  # an empty LOADTEST_RESULTS_DIR= parses as "."
        return DEFAULT_RESULTS_DIR
    path = Path(configured).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


def valid_run_id(run_id: Any) -> bool:
    return isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id) is not None


# ===================================================================================== schema (version 1)
def _text(limit: int, *, optional: bool = False) -> Any:
    """A string field: control characters removed, whitespace collapsed, cut to ``limit`` characters.
    ``optional``: null (or a missing key) reads as ""."""

    def clean(value: Any) -> Any:
        if value is None:
            return "" if optional else None
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError("must be text")
        return _clip(" ".join(_CONTROL.sub(" ", str(value)).split()), limit)

    return Annotated[str, BeforeValidator(clean)]


def _empty_if_null(empty: Any) -> Any:
    """``null`` for an optional list / object reads as empty."""
    return BeforeValidator(lambda value: empty() if value is None else value)


def _file_name(value: Any) -> Any:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or FILE_NAME_RE.fullmatch(value) is None:
        raise ValueError("must be a bare file name inside the run folder (letters, digits, '_', '.', '-')")
    return value


Count = Annotated[int, Field(ge=0, le=10**12)]
Users = Annotated[int, Field(ge=0, le=10**7)]
Seconds = Annotated[float, Field(ge=0, le=10**8)]
Rate = Annotated[float, Field(ge=0, le=10**9)]  # requests (or failures) per second, MiB, ...
Ms = Annotated[float, Field(ge=0, le=10**9)]
Share = Annotated[float, Field(ge=0, le=1)]
Pct = Annotated[float, Field(ge=0, le=10_000)]
FileName = Annotated[str | None, BeforeValidator(_file_name)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False, frozen=True)


def _failures_within(requests: int, failures: int, what: str) -> None:
    if failures > requests:
        raise ValueError(f"{what}: failures ({failures}) cannot exceed requests ({requests})")


class Target(_Model):
    host: _text(200)
    workers: Annotated[int, Field(ge=1, le=1024)]
    environment: _text(40)
    redis: bool
    notes: _text(2000, optional=True) = ""
    db_rows: Annotated[dict[_text(64), Count], _empty_if_null(dict), Field(max_length=100)] = Field(default_factory=dict)


class Machine(_Model):
    cpus: Annotated[int, Field(ge=1, le=4096)]
    memory_gb: Annotated[float, Field(ge=0, le=10**6)]
    os: _text(120)
    python: _text(40)
    locust_version: _text(40)


class Slo(_Model):
    name: _text(120)
    target: _text(60)
    actual: _text(60)
    passed: bool
    users: Users | None = None  # optional: the load step this SLO is measured at


class Verdict(_Model):
    passed: bool
    headline: _text(300)
    slos: Annotated[list[Slo], _empty_if_null(list), Field(max_length=50)] = Field(default_factory=list)


class Capacity(_Model):
    max_users_within_slo: Users
    max_rps_within_slo: Rate
    knee_users: Users | None = None
    note: _text(1000, optional=True) = ""


class Totals(_Model):
    requests: Count
    failures: Count
    error_rate: Share
    rps: Rate
    p50_ms: Ms
    p95_ms: Ms
    p99_ms: Ms
    max_ms: Ms

    @model_validator(mode="after")
    def _counts(self) -> "Totals":
        _failures_within(self.requests, self.failures, "totals")
        return self


class Step(_Model):
    users: Users
    start_s: Seconds
    end_s: Seconds
    rps: Rate
    error_rate: Share
    p50_ms: Ms | None = None
    p95_ms: Ms | None = None
    p99_ms: Ms | None = None
    slo_p95_ms: Ms | None = None  # p95 of only the requests the p95 SLO judges (when that is a subset)
    passed: bool

    @model_validator(mode="after")
    def _window(self) -> "Step":
        if self.end_s < self.start_s:
            raise ValueError(f"step at {self.users} users ends ({self.end_s}) before it starts ({self.start_s})")
        return self


class Point(_Model):
    t_s: Seconds
    users: Users
    rps: Rate
    p50_ms: Ms | None = None
    p95_ms: Ms | None = None
    failures_per_s: Rate | None = 0.0
    cpu_pct: Pct | None = None
    rss_mb: Rate | None = None
    db_connections: Count | None = None


class Endpoint(_Model):
    name: _text(200)
    method: _text(10)
    group: _text(60, optional=True) = ""
    requests: Count
    failures: Count
    p50_ms: Ms | None = None
    p95_ms: Ms | None = None
    p99_ms: Ms | None = None
    max_ms: Ms | None = None
    rps: Rate

    @field_validator("method")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _counts(self) -> "Endpoint":
        _failures_within(self.requests, self.failures, f"endpoint {self.method} {self.name}")
        return self

    @property
    def failure_rate(self) -> float:
        return self.failures / self.requests if self.requests else 0.0


class UserClass(_Model):
    name: _text(80)
    weight_share: Share
    requests: Count
    failures: Count
    p95_ms: Ms | None = None

    @model_validator(mode="after")
    def _counts(self) -> "UserClass":
        _failures_within(self.requests, self.failures, f"class {self.name}")
        return self

    @property
    def failure_rate(self) -> float:
        return self.failures / self.requests if self.requests else 0.0


class Abuse(_Model):
    requests: Count
    throttled: Count
    throttled_share: Share
    note: _text(1000, optional=True) = ""

    @model_validator(mode="after")
    def _counts(self) -> "Abuse":
        if self.throttled > self.requests:
            raise ValueError(f"abuse: throttled ({self.throttled}) cannot exceed requests ({self.requests})")
        return self


class Server(_Model):
    peak_cpu_pct: Pct | None = None
    peak_rss_mb: Rate | None = None
    rss_growth_mb_per_min: Annotated[float, Field(ge=-(10**6), le=10**6)] | None = None
    peak_db_connections: Count | None = None
    pool_size: Count | None = None
    errors_5xx: Count | None = 0
    timeouts: Count | None = 0
    log_errors: Annotated[list[_text(300)], _empty_if_null(list), Field(max_length=50)] = Field(default_factory=list)


class Finding(_Model):
    severity: Literal["high", "medium", "low"]
    title: _text(200)
    detail: _text(2000, optional=True) = ""
    status: Literal["fixed", "open", "accepted"]


class Files(_Model):
    report_md: FileName = None
    locust_html: FileName = None


class Summary(_Model):
    """summary.json, schema version 1 (see the module docstring)."""

    schema_version: Literal[1] = Field(alias="schema")
    run_id: _text(64)
    started_at: datetime
    ended_at: datetime
    duration_s: Seconds
    target: Target
    machine: Machine | None = None
    verdict: Verdict
    capacity: Capacity
    totals: Totals
    steps: Annotated[list[Step], Field(max_length=100)]
    timeline: Annotated[list[Point], Field(max_length=2000)]
    endpoints: Annotated[list[Endpoint], Field(max_length=500)]
    classes: Annotated[list[UserClass], _empty_if_null(list), Field(max_length=50)] = Field(default_factory=list)
    abuse: Abuse | None = None
    server: Server | None = None
    findings: Annotated[list[Finding], _empty_if_null(list), Field(max_length=100)] = Field(default_factory=list)
    files: Annotated[Files, _empty_if_null(dict)] = Field(default_factory=Files)

    @field_validator("started_at", "ended_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        """Aware UTC within [TIME_MIN, TIME_MAX]: a sentinel such as 9999-12-31 or 0001-01-01 is a schema error here,
        never an overflow later (converting it to the viewer's time zone would raise)."""
        try:
            value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        except OverflowError:
            raise ValueError("time out of range") from None
        if not TIME_MIN <= value <= TIME_MAX:
            raise ValueError(f"must be between {TIME_MIN:%Y-%m-%d} and {TIME_MAX:%Y-%m-%d} (UTC)")
        return value

    @model_validator(mode="after")
    def _order(self) -> "Summary":
        if self.ended_at < self.started_at:
            raise ValueError("ended_at is before started_at")
        self.steps.sort(key=lambda s: (s.start_s, s.users))  # in place: the lists are plain lists
        self.timeline.sort(key=lambda p: p.t_s)
        return self

    @property
    def users_peak(self) -> int:
        return max((s.users for s in self.steps), default=0) or max((p.users for p in self.timeline), default=0)

    @property
    def knee_step(self) -> Step | None:
        knee = self.capacity.knee_users
        return next((s for s in self.steps if s.users == knee), None) if knee is not None else None


class SummaryError(Exception):
    """summary.json cannot be used: unreadable, too large, not JSON, non-finite numbers or not schema version 1.
    The message (it can quote the file) is cut to ``MAX_ERROR_CHARS``."""

    def __init__(self, message: Any = "") -> None:
        super().__init__(_clip(str(message), MAX_ERROR_CHARS))


class _Changed(SummaryError):
    """summary.json was replaced between the check and the read: not cached, the next view reads the new file."""


class _NonFinite(ValueError):
    pass


def _reject_constant(name: str) -> Any:
    raise _NonFinite(name)


def _finite_float(literal: str) -> float:
    value = float(literal)
    if value != value or value in (float("inf"), float("-inf")):
        raise _NonFinite(literal)
    return value


def _describe(exc: ValidationError, limit: int = 3) -> str:
    parts = []
    for err in exc.errors(include_url=False, include_input=False)[:limit]:
        # loc parts can be keys from the file (db_rows tables): each is cut, like the message
        where = ".".join(_clip(str(p), 40) for p in err.get("loc", ()) if p != "schema_version") or "summary"
        msg = _clip(str(err.get("msg", "invalid")).removeprefix("Value error, "), 200)
        parts.append(f"{where}: {msg}")
    more = exc.error_count() - limit
    return "; ".join(parts) + (f" (and {more} more)" if more > 0 else "")


def _size_text(n: int) -> str:
    return f"{n / (1024 * 1024):g} MB" if n >= 1024 * 1024 else f"{n:,} bytes"


def parse_summary(raw: bytes) -> Summary:
    """Bytes of a summary.json -> a validated ``Summary`` (raises ``SummaryError`` with a reason people can act on)."""
    if len(raw) > MAX_SUMMARY_BYTES:
        raise SummaryError(f"summary.json is larger than {_size_text(MAX_SUMMARY_BYTES)}")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SummaryError("summary.json is not UTF-8 text") from None
    try:
        data = json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float)
    except _NonFinite as exc:
        raise SummaryError(f"summary.json contains the number {_clip(str(exc), 40)} - every number must be finite "
                           "(write null for a missing value)") from None
    except json.JSONDecodeError as exc:
        raise SummaryError(f"summary.json is not valid JSON (line {exc.lineno}, column {exc.colno}: {exc.msg})") from None
    except (ValueError, RecursionError) as exc:
        raise SummaryError(f"summary.json could not be decoded ({type(exc).__name__})") from None
    if not isinstance(data, dict):
        raise SummaryError("summary.json must hold one JSON object")
    version = data.get("schema")
    if version is None:
        raise SummaryError(f'summary.json has no "schema" key (this page reads schema version {SCHEMA_VERSION})')
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        shown = json.dumps(version)[:20] if not isinstance(version, str) else repr(version[:20])
        raise SummaryError(f"summary.json uses schema version {shown}; this page reads version {SCHEMA_VERSION}")
    try:
        return Summary.model_validate(data)
    except ValidationError as exc:
        raise SummaryError(f"summary.json does not match schema version {SCHEMA_VERSION}: {_describe(exc)}") from None


def _from_timestamp(ts: float | None) -> datetime | None:
    """A POSIX time as an aware UTC datetime when it lies within [TIME_MIN, TIME_MAX] (else None: a sentinel or a
    bogus file time is never handed to the templates, whose time-zone conversion would overflow)."""
    if ts is None:
        return None
    try:
        when = datetime.fromtimestamp(ts, UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return when if TIME_MIN <= when <= TIME_MAX else None


# ===================================================================================== paths & files
def _resolve_root(root: Path) -> Path | None:
    try:
        base = root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    return base if base.is_dir() else None


def _inside(path: Path, base: Path) -> Path | None:
    """``path`` with every symlink resolved, if it exists and stays inside ``base`` (else None)."""
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved if resolved != base and resolved.is_relative_to(base) else None


def _open_regular(path: Path, ident: tuple[int, int] | None = None) -> tuple[BinaryIO, os.stat_result] | None:
    """Open ``path`` for reading exactly as it was checked: a final symlink is not followed (O_NOFOLLOW), a FIFO never
    blocks (O_NONBLOCK), and the open file must be a regular file - with ``ident``, the very (st_dev, st_ino) checked
    before, so a file swapped in between is refused. None otherwise; the caller closes the returned handle."""
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except (OSError, ValueError):
        return None
    try:
        st = os.fstat(fd)
        if stat.S_ISREG(st.st_mode) and (ident is None or (st.st_dev, st.st_ino) == ident):
            return os.fdopen(fd, "rb"), st
    except BaseException:
        os.close(fd)
        raise
    os.close(fd)
    return None


@dataclass(slots=True)
class _Candidate:
    run_id: str  # the folder name: what URLs use
    run_dir: Path  # resolved
    summary: Path  # resolved
    key: tuple[str, int, int, int, int]  # (summary path, mtime_ns, size, inode, ctime_ns) - the cache key
    ident: tuple[int, int]  # (st_dev, st_ino) of summary.json as checked: what a later open must find
    mtime_ns: int
    dir_mtime: float
    sort_ts: float = 0.0


def _candidate(base: Path, run_id: str) -> _Candidate | None:
    """The run folder ``run_id`` under ``base`` (already resolved), or None when it is not a usable run."""
    if not valid_run_id(run_id):
        return None
    run_dir = _inside(base / run_id, base)
    if run_dir is None or not run_dir.is_dir():
        return None
    summary = _inside(run_dir / SUMMARY_FILE, base)
    if summary is None:
        return None
    try:
        st = summary.stat()
        dir_mtime = run_dir.stat().st_mtime
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    # inode + ctime: a same-size rewrite whose mtime was preserved (cp -p, rsync -t, tar, os.replace) still changes them
    key = (str(summary), st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)
    return _Candidate(run_id, run_dir, summary, key, (st.st_dev, st.st_ino), st.st_mtime_ns, dir_mtime)


# ===================================================================================== caches
_lock = threading.Lock()
_parsed: OrderedDict[tuple[str, int, int, int, int], Summary | SummaryError] = OrderedDict()
_headers: OrderedDict[tuple[str, int, int, int, int], "RunHeader"] = OrderedDict()
_peeks: OrderedDict[tuple[str, int, int, int, int], float | None] = OrderedDict()


def clear_caches() -> None:
    with _lock:
        _parsed.clear()
        _headers.clear()
        _peeks.clear()


def _remember(cache: OrderedDict, key: Any, value: Any, size: int) -> None:
    with _lock:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > size:
            cache.popitem(last=False)


def _recall(cache: OrderedDict, key: Any) -> Any:
    with _lock:
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
    return None


def _read_capped(c: _Candidate, cap: int) -> bytes:
    opened = _open_regular(c.summary, c.ident)
    if opened is None:
        raise _Changed("summary.json changed while it was being read - reload the page")
    fh, st = opened
    with fh:
        if st.st_size > cap:
            raise SummaryError(f"summary.json is larger than {_size_text(cap)}")
        data = fh.read(cap + 1)
    if len(data) > cap:
        raise SummaryError(f"summary.json is larger than {_size_text(cap)}")
    return data


def _load(c: _Candidate) -> Summary | SummaryError:
    """The parsed summary (or its error), from the LRU when the file has not changed."""
    hit = _recall(_parsed, c.key)
    if hit is not None:
        return hit
    try:
        result: Summary | SummaryError = parse_summary(_read_capped(c, MAX_SUMMARY_BYTES))
    except _Changed as exc:
        return exc  # not cached: the file under this key is gone, the next view parses its replacement
    except SummaryError as exc:
        result = exc
    except OSError as exc:
        result = SummaryError(f"summary.json could not be read ({exc.strerror or type(exc).__name__})")
    except Exception:  # never a 500 for a bad file
        log.exception("load-test summary %s could not be parsed", c.summary)
        result = SummaryError("summary.json could not be read (unexpected error - see the server log)")
    _remember(_parsed, c.key, result, CACHE_SIZE)
    return result


def _peek_started(c: _Candidate) -> float | None:
    """started_at from the first ``PEEK_BYTES`` of summary.json (cheap ordering of runs not parsed yet); None when it
    is missing, unreadable or outside [TIME_MIN, TIME_MAX]."""
    with _lock:
        if c.key in _peeks:
            return _peeks[c.key]
    ts: float | None = None
    opened = _open_regular(c.summary, c.ident)
    if opened is not None:
        fh, _ = opened
        try:
            with fh:
                m = _PEEK_STARTED.search(fh.read(PEEK_BYTES))
            if m:
                when = datetime.fromisoformat(m.group(1).decode("ascii").replace(" ", "T"))
                ts = (when if when.tzinfo else when.replace(tzinfo=UTC)).timestamp()
        except (OSError, ValueError, OverflowError):
            ts = None
    if ts is not None and _from_timestamp(ts) is None:
        ts = None
    _remember(_peeks, c.key, ts, HEADER_CACHE_SIZE)
    return ts


# ===================================================================================== runs
@dataclass(frozen=True, slots=True)
class RunHeader:
    """One entry of the run picker."""

    run_id: str
    started_at: datetime | None
    sort_ts: float
    passed: bool | None  # None: the run could not be read
    users_peak: int | None
    headline: str
    error: str | None


@dataclass(slots=True)
class RunListing:
    root: Path
    exists: bool  # the results folder exists
    runs: list[RunHeader] = field(default_factory=list)  # newest first, at most MAX_LISTED
    total: int = 0  # runs found among the scanned entries
    truncated: bool = False  # the folder has more than MAX_SCAN entries: older runs may be missing from the list


@dataclass(frozen=True, slots=True)
class LoadedRun:
    run_id: str
    path: Path  # resolved run folder
    summary: Summary | None
    error: str | None  # set when summary.json could not be used
    modified: datetime | None  # summary.json modification time (UTC); None when outside [TIME_MIN, TIME_MAX]


def _header(c: _Candidate) -> RunHeader:
    cached = _recall(_headers, c.key)
    if cached is not None:
        return cached
    result = _load(c)
    if isinstance(result, Summary):
        header = RunHeader(c.run_id, result.started_at, result.started_at.timestamp(), result.verdict.passed,
                           result.users_peak, result.verdict.headline, None)
    else:
        ts = _peek_started(c)
        started = _from_timestamp(ts)
        header = RunHeader(c.run_id, started, ts if started is not None and ts is not None else c.dir_mtime,
                           None, None, "", str(result))
    if not isinstance(result, _Changed):
        _remember(_headers, c.key, header, HEADER_CACHE_SIZE)
    return header


def _sort_ts(c: _Candidate) -> float:
    cached = _recall(_headers, c.key)
    if cached is not None:
        return cached.sort_ts
    ts = _peek_started(c)
    return ts if ts is not None else c.dir_mtime


def list_runs(root: Path | None = None) -> RunListing:
    """Runs in the results folder, newest first (at most ``MAX_LISTED``; at most ``MAX_SCAN`` entries examined)."""
    root = root if root is not None else results_dir()
    base = _resolve_root(root)
    if base is None:
        return RunListing(root=root, exists=False)
    found: dict[str, _Candidate] = {}  # resolved summary path -> candidate (a symlinked alias is listed once)
    scanned = 0
    truncated = False
    try:
        with os.scandir(base) as entries:
            for entry in entries:
                if scanned >= MAX_SCAN:
                    truncated = True
                    break
                scanned += 1
                c = _candidate(base, entry.name)
                if c is None:
                    continue
                other = found.get(c.key[0])
                if other is None or (other.run_id != other.run_dir.name and c.run_id == c.run_dir.name):
                    found[c.key[0]] = c
    except OSError as exc:
        log.warning("load-test results folder %s could not be listed: %s", base, exc)
    candidates = list(found.values())
    for c in candidates:
        c.sort_ts = _sort_ts(c)
    candidates.sort(key=lambda c: (c.sort_ts, c.run_id), reverse=True)
    # parsed oldest first: the newest runs are the last ones into the LRU, so the run the page shows by default is
    # still cached when it is loaded right after (no second parse of a file of up to 5 MB on a cold cache)
    runs = [_header(c) for c in reversed(candidates[:MAX_LISTED])]
    runs.sort(key=lambda h: (h.sort_ts, h.run_id), reverse=True)
    return RunListing(root=root, exists=True, runs=runs, total=len(candidates), truncated=truncated)


def load_run(run_id: str, root: Path | None = None) -> LoadedRun | None:
    """The run in folder ``run_id`` (None: no such run, an invalid id, or a folder that escapes the results folder)."""
    base = _resolve_root(root if root is not None else results_dir())
    if base is None or not valid_run_id(run_id):
        return None
    c = _candidate(base, run_id)
    if c is None:
        return None
    result = _load(c)
    modified = _from_timestamp(c.mtime_ns / 1e9)
    if isinstance(result, Summary):
        return LoadedRun(c.run_id, c.run_dir, result, None, modified)
    return LoadedRun(c.run_id, c.run_dir, None, str(result), modified)


# ===================================================================================== downloads
@dataclass(frozen=True, slots=True)
class DownloadFile:
    path: Path  # resolved, inside the run folder
    size: int
    media_type: str
    filename: str  # suggested name: <run_id>-report.md / <run_id>-locust.html
    ident: tuple[int, int] | None = None  # (st_dev, st_ino) as checked: the file open_download must find


def download_file(run: LoadedRun, kind: str) -> DownloadFile | None:
    """``kind`` ("report.md" / "locust.html") of a readable run: the file summary.files names, if it is a regular
    file inside the run folder of at most ``MAX_DOWNLOAD_BYTES`` (else None)."""
    spec = DOWNLOADS.get(kind)
    if spec is None or run.summary is None:
        return None
    attr, media_type = spec
    name = getattr(run.summary.files, attr)
    if not name or FILE_NAME_RE.fullmatch(name) is None:
        return None
    path = _inside(run.path / name, run.path)
    if path is None:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_DOWNLOAD_BYTES:
        return None
    return DownloadFile(path, st.st_size, media_type, f"{run.run_id}-{kind}", (st.st_dev, st.st_ino))


def open_download(run_id: str, kind: str, root: Path | None = None) -> tuple[DownloadFile, BinaryIO] | None:
    """Open a download for streaming: the file that was checked (no symlink or FIFO swapped in since), its size
    re-checked on the open handle. The caller closes the handle."""
    run = load_run(run_id, root)
    if run is None:
        return None
    found = download_file(run, kind)
    if found is None:
        return None
    opened = _open_regular(found.path, found.ident)
    if opened is None:
        return None
    fh, st = opened
    if st.st_size > MAX_DOWNLOAD_BYTES:
        fh.close()
        return None
    return DownloadFile(found.path, st.st_size, found.media_type, found.filename, found.ident), fh
