"""Super-admin load-test results page (docs/load-test-page.md).

GET /superadmin/load-test[?run=<run_id>][&sort=p95|p99|requests|failures|name][&show=all]
    one run of the production-like Locust stress test (latest by default): verdict + SLO checklist, KPI tiles, load
    steps (table + two small multiples sharing the users axis - never a dual axis), timeline small multiples, endpoints,
    user classes, abuse, server health, findings and the exact run settings. An unknown or invalid run id is a 404; a
    run whose summary.json cannot be used - or that cannot be displayed for any other reason - renders an error card
    (never a 500).
GET /superadmin/load-test/{run_id}/report.md | /locust.html
    downloads only: ``Content-Disposition: attachment``, nosniff and a sandboxing CSP - Locust's HTML report carries
    inline scripts and is never rendered under the app's origin.

The public, curated view of the same results is GET /load-test (app/routers/public_loadtest.py: it reuses ``kpis``,
``steps_view``, ``plan_text`` and the chart macros); this page's header links it while PUBLIC_LOADTEST_PAGE is on.

Data: app/services/loadtest_results.py (summary.json schema version 1 + a cached, path-safe loader; all file work runs
in a worker thread). The page reads files only - no SQL beyond the super-admin session check, so the section badges of
the super-admin navigation (database counts) are not shown here.

Charts are inline SVG drawn by superadmin/_load_test_charts.html from the geometry computed below: no JavaScript, no
style attributes (strict CSP), at most ``MAX_PLOT_POINTS`` points per series (LTTB downsampling), tick labels in HTML
so they stay legible at any width, native <title> tooltips + a CSS crosshair for hover, and a data table for each chart.
"""

from __future__ import annotations

import bisect
import dataclasses
import logging
import math
import re
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any, BinaryIO
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from markupsafe import Markup, escape
from starlette.concurrency import run_in_threadpool

from app.config import BASE_DIR, get_settings
from app.core.deps import DB, NotFound, SuperAdminUser
from app.core.nav import LOAD_TEST_ITEM
from app.core.templating import render
from app.routers.superadmin import SECTIONS
from app.services import loadtest_results as lt

router = APIRouter()
log = logging.getLogger("ironpulse.loadtest")

PAGE_PATH = "/superadmin/load-test"
TEMPLATE = "superadmin/load_test.html"
SECTION = "loadtest"
TOP_ENDPOINTS = 25
SORTS: dict[str, tuple[str, str]] = {  # ?sort= value -> (chip label, "sorted by ...")
    "p95": ("p95", "p95 latency"), "p99": ("p99", "p99 latency"), "requests": ("Requests", "requests"),
    "failures": ("Failures", "failures"), "name": ("Name", "name"),
}
DEFAULT_SORT = "p95"
MAX_PLOT_POINTS = 400  # per series
MAX_TABLE_ROWS = 300  # rows of the timeline's data table (every n-th sample beyond that)
HOVER_SLICES = 30  # native-tooltip hit areas per timeline panel
DEFAULT_SLO_USERS = 200  # the step whose p95 is a KPI when no SLO names one
FAILURE_RED = 0.01  # endpoint / class failure rates from 1 % are flagged red, anything above 0 amber
CLIP_FACTOR = 3.0  # the p95 step chart's axis stops near 3x the SLO threshold when a step goes far beyond it
REF_HEADROOM = 1.1  # a reference line (the pool size) stays below the top of its panel
END_LABEL_REACH = 0.97  # direct end labels only for lines that reach the right edge of their panel
DOWNLOAD_CHUNK = 64 * 1024
STEP_W, STEP_H = 480, 176  # step charts: viewBox width, plot height (px)
LINE_W, LINE_H, LINE_H_COMPACT = 600, 120, 72  # timeline panels
DISPLAY_ERROR = "This run could not be displayed (unexpected error - see the server log)."
# shown on the empty state: the kit's 10-minute recipe (loadtest/README.md, section 4, has every Locust flag)
RUN_COMMAND = "\n".join((
    "loadtest/run_server.sh start --run stress-$(date +%Y%m%d-%H%M)   # production-like server on :12000",
    "RUN=$(cat loadtest/.current_run)",
    ".venv-loadtest/bin/locust -f loadtest/locustfile.py --host http://127.0.0.1:12000 --headless --processes 4 ...",
    "loadtest/run_server.sh stop",
    '.venv-loadtest/bin/python loadtest/analyze.py "loadtest/results/$RUN"',
))
_SLO_USERS = re.compile(r"(\d[\d,]*)\s*(?:concurrent\s+|virtual\s+|simulated\s+)?users?\b", re.IGNORECASE)


# ===================================================================================== formatting
class Fmt:
    """Display formats used by the templates (passed in the context as ``fmt``)."""

    @staticmethod
    def num(value: Any) -> str:
        return "—" if value is None else f"{value:,.0f}"

    @staticmethod
    def compact(value: Any) -> str:
        """1,284 / 12.9K / 4.2M: in full under 10,000."""
        if value is None:
            return "—"
        v = float(value)
        if abs(v) < 10_000:
            return f"{v:,.0f}"
        for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
            if abs(v) >= div:
                text = f"{v / div:.1f}".removesuffix(".0")
                return f"{text}{suffix}"
        return f"{v:,.0f}"

    @staticmethod
    def ms(value: Any) -> str:
        if value is None:
            return "—"
        return f"{value:.1f} ms" if value < 10 else f"{value:,.0f} ms"

    @staticmethod
    def short_ms(value: Any) -> str:
        """Latency for tight spots: 640 ms / 2.4 s / 12 s."""
        if value is None:
            return "—"
        v = float(value)
        if v < 1000:
            return f"{v:.0f} ms"
        return f"{v / 1000:.1f} s" if v < 10_000 else f"{v / 1000:,.0f} s"

    @staticmethod
    def rps(value: Any) -> str:
        if value is None:
            return "—"
        return f"{value:.1f} req/s" if value < 10 else f"{value:,.0f} req/s"

    @staticmethod
    def pct(fraction: Any) -> str:
        """A fraction 0-1 as a percentage: 0 % / <0.01 % / 0.42 % / 2.3 % / 98.7 % / 100 %."""
        if fraction is None:
            return "—"
        p = float(fraction) * 100
        if p == 0:
            return "0 %"
        if p < 0.01:
            return "<0.01 %"
        if p < 1:
            return f"{p:.2f} %"
        return f"{p:.1f}".removesuffix(".0") + " %"

    @staticmethod
    def cpu(value: Any) -> str:
        return "—" if value is None else f"{value:.0f} %"

    @staticmethod
    def mb(value: Any) -> str:
        if value is None:
            return "—"
        return f"{value / 1024:.1f} GB" if value >= 1024 else f"{value:,.0f} MB"

    @staticmethod
    def growth(value: Any) -> str:
        """Signed MB per minute (the unit is printed beside it)."""
        return "—" if value is None else f"{value:+.1f}"

    @staticmethod
    def route(name: str) -> Markup:
        """An endpoint name, escaped, with a line-break opportunity after each '/' (long routes wrap at slashes)."""
        return escape(name).replace("/", Markup("/<wbr>"))

    @staticmethod
    def clock(seconds: Any) -> str:
        """Run time as m:ss (h:mm:ss from an hour)."""
        s = int(round(float(seconds or 0)))
        h, rest = divmod(s, 3600)
        m, sec = divmod(rest, 60)
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

    @staticmethod
    def duration(seconds: Any) -> str:
        s = int(round(float(seconds or 0)))
        if s < 60:
            return f"{s} s"
        h, rest = divmod(s, 3600)
        m, sec = divmod(rest, 60)
        if h:
            return f"{h} h {m} min" if m else f"{h} h"
        return f"{m} min {sec} s" if sec else f"{m} min"

    @staticmethod
    def size(n: Any) -> str:
        n = int(n or 0)
        if n < 1024:
            return f"{n} B"
        if n < 1024 * 1024:
            return f"{n / 1024:.0f} KB"
        return f"{n / (1024 * 1024):.1f} MB"

    @staticmethod
    def ordinal(n: int) -> str:
        """1st / 2nd / 3rd / 4th ... 11th / 12th / 13th ... 21st."""
        suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"{n}{suffix}"


FMT = Fmt()


# ===================================================================================== scales & downsampling
def nice_scale(peak: float | None, *, low: float = 0.0, intervals: Sequence[int] = (3, 4),
               floor_top: float = 1.0) -> list[float]:
    """Gridline values from a round start at or below ``low`` (0 for magnitudes) up to a round top covering ``peak``,
    in equal steps of 1 / 2 / 2.5 / 5 x 10^k; the tightest of the allowed interval counts wins (fewer on a tie)."""
    hi = max(float(peak or 0.0), low + floor_top)
    best: tuple[float, int, float, float] | None = None  # (span, intervals, step, start)
    for n in intervals:
        raw = (hi - low) / n
        mag = 10 ** math.floor(math.log10(raw))
        for m in (1, 2, 2.5, 5, 10, 20):
            step = m * mag
            start = math.floor(low / step + 1e-9) * step
            if start + step * n >= hi * (1 - 1e-9):
                if best is None or step * n < best[0] - 1e-9:
                    best = (step * n, n, step, start)
                break
    assert best is not None
    _, n, step, start = best
    return [round(start + step * i, 10) for i in range(n + 1)]


def tick_text(value: float, unit: str = "") -> str:
    if abs(value) >= 10_000:
        text = Fmt.compact(value)
    elif float(value).is_integer():
        text = f"{value:,.0f}"
    else:
        text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{text} {unit}".strip() if unit else text


def _compact_tick(value: float) -> str:
    """2.5K / 10K / 1.5M (0 stays 0)."""
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= div:
            return f"{value / div:.2f}".rstrip("0").rstrip(".") + suffix
    return tick_text(value)


def y_labels(ticks: Sequence[float]) -> list[str]:
    """Tick labels top to bottom (the unit is printed once, above the axis): in full under 10,000, and all compact on
    an axis that reaches it (0 / 2.5K / 5K / 7.5K / 10K) - one style per axis."""
    if max((abs(v) for v in ticks), default=0) >= 10_000:
        return [_compact_tick(v) for v in reversed(ticks)]
    return [tick_text(v) for v in reversed(ticks)]


TIME_STEPS = (10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400)


def time_axis(duration: float, max_intervals: int = 6) -> tuple[float, list[str]]:
    """(domain end, tick labels) for a run time axis starting at 0 with evenly spaced round ticks."""
    d = max(float(duration or 0.0), 1.0)
    step = next((s for s in TIME_STEPS if math.ceil(d / s - 1e-9) <= max_intervals), 0)
    if not step:
        step = math.ceil(d / max_intervals / 3600) * 3600
    n = max(1, math.ceil(d / step - 1e-9))
    if step >= 3600:
        div, unit = 3600, "h"
    elif step >= 60:
        div, unit = 60, "min"
    else:
        div, unit = 1, "s"
    labels = [f"{step * i / div:g}" for i in range(n + 1)]
    labels[-1] += f" {unit}"
    return float(step * n), labels


def lttb(points: Sequence[tuple[float, float]], threshold: int) -> list[tuple[float, float]]:
    """Largest-Triangle-Three-Buckets downsampling: ``threshold`` points that keep the series' visual shape
    (first and last points kept, peaks preserved)."""
    n = len(points)
    if threshold >= n or n <= 2:
        return list(points)
    if threshold < 3:
        return [points[0], points[-1]]
    out = [points[0]]
    every = (n - 2) / (threshold - 2)
    a = 0
    for i in range(threshold - 2):
        avg_start = int(math.floor((i + 1) * every)) + 1
        avg_end = min(int(math.floor((i + 2) * every)) + 1, n)
        span = points[avg_start:avg_end] or [points[-1]]
        avg_x = sum(p[0] for p in span) / len(span)
        avg_y = sum(p[1] for p in span) / len(span)
        start = int(math.floor(i * every)) + 1
        end = int(math.floor((i + 1) * every)) + 1
        ax, ay = points[a]
        best, best_area = start, -1.0
        for j in range(start, end):
            area = abs((ax - avg_x) * (points[j][1] - ay) - (ax - points[j][0]) * (avg_y - ay))
            if area > best_area:
                best, best_area = j, area
        out.append(points[best])
        a = best
    out.append(points[-1])
    return out


def segments(points: Sequence[tuple[float, float | None]]) -> list[list[tuple[float, float]]]:
    """Runs of consecutive points with a value (a null value breaks the line)."""
    out: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for t, v in points:
        if v is None:
            if current:
                out.append(current)
                current = []
        else:
            current.append((t, float(v)))
    if current:
        out.append(current)
    return out


def downsample(points: Sequence[tuple[float, float | None]], budget: int = MAX_PLOT_POINTS) -> list[list[tuple[float, float]]]:
    """Segments of a series with at most ~``budget`` points in total (LTTB per segment, shares by length)."""
    parts = segments(points)
    total = sum(len(p) for p in parts)
    if total <= budget:
        return parts
    return [lttb(p, max(2, budget * len(p) // total)) for p in parts]


def _f(v: float) -> str:
    return f"{v:.1f}".removesuffix(".0")


# ===================================================================================== chart geometry
def _bar_path(x: float, w: float, top: float, base: float) -> str:
    """A column from the baseline up to ``top``: 4-unit rounded data end, square at the baseline."""
    h = base - top
    r = max(0.0, min(4.0, w / 2, h))
    return (f"M{_f(x)},{_f(base)}V{_f(top + r)}Q{_f(x)},{_f(top)} {_f(x + r)},{_f(top)}H{_f(x + w - r)}"
            f"Q{_f(x + w)},{_f(top)} {_f(x + w)},{_f(top + r)}V{_f(base)}Z")


def step_chart(steps: Sequence[lt.Step], values: Sequence[float | None], *, unit: str, knee: int | None,
               titles: Sequence[str], threshold: float | None = None, threshold_label: str = "",
               floor_top: float = 1.0, desc: str = "", clip_text: Callable[[float], str] = tick_text) -> dict[str, Any]:
    """Column chart over the load steps (one band per step; x labels are the users per step).

    With a ``threshold`` (the SLO), one step far beyond it does not squash the others: the axis stops near
    ``CLIP_FACTOR`` x the threshold, and a taller column is drawn to the top with a break mark and its value
    (``clip_text``) printed above the plot; the steps table keeps every exact value."""
    n = max(1, len(steps))
    band = STEP_W / n
    bar_w = min(18.0, band * 0.55)
    # headroom above the SLO line: it never merges into the top edge when the steps stay below it
    peak = max([v for v in values if v is not None] + ([threshold * REF_HEADROOM] if threshold is not None else []),
               default=0.0)
    clip_at = threshold * CLIP_FACTOR if threshold else None
    ticks = nice_scale(clip_at if clip_at is not None and peak > clip_at else peak, floor_top=floor_top)
    top = ticks[-1]

    def y(v: float) -> float:
        return STEP_H - v / top * STEP_H

    every = 1 if n <= 10 else math.ceil(n / 8)
    bars = []
    for i, (step, value) in enumerate(zip(steps, values, strict=True)):
        x = i * band + (band - bar_w) / 2
        clipped = value is not None and value > top
        d = None
        if value:
            vy = 0.0 if clipped else min(y(value), STEP_H - 1.0)  # a tiny value still shows a 1 px stub
            d = _bar_path(x, bar_w, vy, STEP_H)
        bars.append({"d": d, "band_x": _f(i * band), "band_w": _f(band), "title": titles[i],
                     "label": Fmt.num(step.users) if (i % every == 0 or i == knee) else "", "knee": i == knee,
                     "clip": {"x1": _f(x), "x2": _f(x + bar_w), "text": clip_text(value)} if clipped else None})
    clipped_any = any(b["clip"] for b in bars)
    if clipped_any:
        desc = (f"{desc} The axis stops at {tick_text(top)} {unit}: taller columns are cut with a break mark and "
                "their value is printed above them.").strip()
    return {
        "w": STEP_W, "h": STEP_H, "bars": bars, "y_labels": y_labels(ticks), "unit": unit, "desc": desc,
        "clipped": clipped_any,
        "grid": [_f(y(v) + (0.5 if 0 < y(v) < STEP_H else 0)) for v in ticks],
        "knee": {"x": _f(knee * band), "w": _f(band)} if knee is not None else None,
        "threshold": {"y": _f(y(threshold)), "label": threshold_label} if threshold is not None else None,
    }


def end_labels(ends: Sequence[tuple[str, float, float]], domain: float, h: float) -> list[dict[str, str]]:
    """Direct labels at the right end of each line: ``ends`` = [(label, time of the last point, its y)] ->
    [{"label", "y" (text baseline)}], the top line's label above its end and the others below theirs.

    [] - the legend still names the series - for a single line, when a line stops short of the right edge (a label
    there would float off its end), or when two labels would collide: labels are never nudged away from their lines."""
    if len(ends) < 2 or any(t < domain * END_LABEL_REACH for _, t, _ in ends):
        return []
    placed: list[tuple[str, float]] = []
    for i, (label, _, y) in enumerate(sorted(ends, key=lambda e: e[2])):
        options = (y - 5, y + 13) if i == 0 else (y + 13, y - 5)
        for base in options:  # an 11 px label: ~10 px above its baseline
            if base - 10 >= 0 and base <= h and all(abs(base - other) >= 13 for _, other in placed):
                placed.append((label, base))
                break
        else:
            return []
    return [{"label": label, "y": _f(base)} for label, base in placed]


def line_panel(key: str, title: str, unit: str, series: Sequence[dict[str, Any]], *, domain: float,
               x_labels: list[str], percent: bool = False, ref: float | None = None, ref_label: str = "",
               area: bool = False, compact: bool = False, summary: str = "", floor_top: float = 1.0,
               desc: str = "", table_note: str = "", slices: Sequence[tuple[str, str, str, lt.Point]] = (),
               readout: Callable[[lt.Point], str] | None = None, zero: bool = True,
               direct_labels: bool = False) -> dict[str, Any]:
    """One small multiple over run time. ``series``: [{"label", "tone", "points": [(t_s, value | None)]}];
    ``slices`` (from ``slice_points``) + ``readout`` (point -> tooltip text) make its hover layer; ``direct_labels``
    names each line at its right end (``end_labels``) on top of the legend."""
    h = LINE_H_COMPACT if compact else LINE_H
    values = [v for s in series for _, v in s["points"] if v is not None]
    peak = max(values + ([ref] if ref is not None else []), default=0.0)
    if ref is not None:  # headroom: at the limit, the data and the reference line never merge into the top edge
        peak *= REF_HEADROOM
    if percent and peak <= 100:
        ticks = [0.0, 25.0, 50.0, 75.0, 100.0]
    else:
        low = 0.0 if zero or not values else min(values + ([ref] if ref is not None else []))
        ticks = nice_scale(peak, low=low, intervals=(2,) if compact else (3, 4), floor_top=floor_top)
    bottom, top = ticks[0], ticks[-1]

    def yv(v: float) -> float:
        return h - (v - bottom) / (top - bottom) * h

    def xy(t: float, v: float) -> tuple[float, float]:
        return t / domain * LINE_W, yv(v)

    drawn = []
    ends: list[tuple[str, float, float]] = []
    for s in series:
        line, fill = [], []
        parts = downsample(s["points"])
        for seg in parts:
            pts = [xy(t, v) for t, v in seg]
            line.append("M" + "L".join(f"{_f(px)},{_f(py)}" for px, py in pts) + ("h0.01" if len(pts) == 1 else ""))
            if area and len(pts) > 1:
                fill.append(f"M{_f(pts[0][0])},{h}" + "".join(f"L{_f(px)},{_f(py)}" for px, py in pts)
                            + f"L{_f(pts[-1][0])},{h}Z")
        drawn.append({"label": s["label"], "tone": s["tone"], "line": "".join(line), "area": "".join(fill),
                      "points": sum(len(seg) for seg in parts)})
        if parts:
            t_end, v_end = parts[-1][-1]
            ends.append((s["label"], t_end, yv(v_end)))
    summary_text = f"{summary[:1].upper()}{summary[1:]}." if summary else ""
    return {
        "key": key, "title": title, "unit": unit, "series": drawn, "compact": compact, "w": LINE_W, "h": h,
        "y_labels": y_labels(ticks), "x_labels": x_labels, "summary": summary,
        "desc": " ".join(part for part in (desc, summary_text, table_note) if part),
        "grid": [_f(yv(v) + (0.5 if bottom < v < top else 0)) for v in ticks],
        "ref": {"y": _f(yv(ref)), "label": ref_label} if ref is not None else None,
        "legend": len(series) > 1 or ref is not None,
        "end_labels": end_labels(ends, domain, h) if direct_labels else [],
        "slices": [{"x": x, "w": w, "cx": cx, "title": f"{Fmt.clock(p.t_s)} · {p.users:,} users"
                    + (f" · {readout(p)}" if readout else "")} for x, w, cx, p in slices],
    }


def slice_points(points: Sequence[lt.Point], domain: float, n: int = HOVER_SLICES) -> list[tuple[str, str, str, lt.Point]]:
    """Evenly spaced hit areas over run time (x, width, crosshair x) with the timeline point nearest to each."""
    if not points:
        return []
    n = max(1, min(n, len(points)))
    times = [p.t_s for p in points]
    width = LINE_W / n
    out = []
    for i in range(n):
        center = (i + 0.5) * domain / n
        j = bisect.bisect_left(times, center)
        if j >= len(times) or (j > 0 and center - times[j - 1] <= times[j] - center):
            j -= 1
        p = points[max(0, j)]
        out.append((_f(i * width), _f(width), _f(p.t_s / domain * LINE_W), p))
    return out


def _peak_note(points: Sequence[lt.Point], get: Callable[[lt.Point], float | None], fmt: Callable[[Any], str]) -> str:
    best = None
    for p in points:
        v = get(p)
        if v is not None and (best is None or v > best[1]):
            best = (p.t_s, v)
    return f"peak {fmt(best[1])} at {Fmt.clock(best[0])}" if best else "not recorded"


# ===================================================================================== page model
def slo_step(s: lt.Summary) -> tuple[int, lt.Slo | None]:
    """The users of the step whose p95 is a KPI: an SLO about p95 that names its step, else 200 users."""
    for slo in s.verdict.slos:
        if "p95" not in slo.name.lower():
            continue
        if slo.users is not None:
            return slo.users, slo
        m = _SLO_USERS.search(slo.name)
        if m:
            return int(m.group(1).replace(",", "")), slo
    return DEFAULT_SLO_USERS, None


def _ms_target(text: str) -> float | None:
    """'≤ 800 ms' / '< 1.2 s' -> milliseconds (the SLO threshold line on the p95 chart)."""
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(ms|s)\b", text or "")
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    return value * 1000 if m.group(2) == "s" else value


def _judged_p95(step: lt.Step) -> float | None:
    """The p95 the latency SLO judges at this step: ``slo_p95_ms`` when the analysis wrote it, else ``p95_ms``."""
    return step.slo_p95_ms if step.slo_p95_ms is not None else step.p95_ms


def _flag(rate: float) -> str | None:
    return "red" if rate >= FAILURE_RED else ("amber" if rate > 0 else None)


def kpis(s: lt.Summary) -> list[dict[str, Any]]:
    cap, tot = s.capacity, s.totals
    users, slo = slo_step(s)
    step = next((st for st in reversed(s.steps) if st.users == users), None)
    if slo is not None:  # the value the SLO judged, so the tile always agrees with the verdict
        p95_value = slo.actual or (Fmt.ms(_judged_p95(step)) if step is not None else "—")
        p95_hint = f"SLO {slo.target} · {'met' if slo.passed else 'missed'}"
        p95_tone = "green" if slo.passed else "red"
    elif step is not None:
        p95_value = Fmt.ms(_judged_p95(step))
        p95_hint = f"step {'passed' if step.passed else 'failed'} its SLOs"
        p95_tone = "green" if step.passed else "red"
    else:
        p95_value, p95_hint, p95_tone = "—", f"no {users:,}-user step in this run", "zinc"
    knee = cap.knee_users
    srv = s.server
    if srv is None or srv.errors_5xx is None:
        e5, e5_hint, e5_tone = "—", "server errors not recorded", "zinc"
    else:
        e5 = Fmt.num(srv.errors_5xx)
        e5_hint = f"{Fmt.num(srv.timeouts)} timeout{'s' if srv.timeouts != 1 else ''}" if srv.timeouts is not None \
            else "timeouts not recorded"
        e5_tone = "red" if srv.errors_5xx else "green"
    return [  # "key": what the public page (app/routers/public_loadtest.py) picks its tiles by
        {"key": "users", "label": "Max users within SLO", "value": Fmt.num(cap.max_users_within_slo), "icon": "users",
         "hint": f"knee at {knee:,} users" if knee is not None else "no knee within the tested load",
         "tone": "green" if cap.max_users_within_slo else "red"},
        {"key": "rps", "label": "Max throughput within SLO", "value": Fmt.rps(cap.max_rps_within_slo),
         "icon": "trending-up", "hint": "while every SLO held", "tone": "blue"},
        {"key": "p95", "label": f"p95 at {users:,} users", "value": p95_value, "icon": "clock", "hint": p95_hint,
         "tone": p95_tone},
        {"key": "errors", "label": "Error rate", "value": Fmt.pct(tot.error_rate), "icon": "alert",
         "hint": f"{Fmt.num(tot.failures)} of {Fmt.num(tot.requests)} requests failed",
         "tone": "red" if tot.error_rate >= FAILURE_RED else ("amber" if tot.error_rate > 0 else "green")},
        {"key": "requests", "label": "Requests", "value": Fmt.compact(tot.requests), "icon": "activity",
         "hint": f"{Fmt.rps(tot.rps)} on average over {Fmt.duration(s.duration_s)}", "tone": "blue"},
        {"key": "5xx", "label": "5xx responses", "value": e5, "icon": "x-circle", "hint": e5_hint, "tone": e5_tone},
    ]


def step_title(st: lt.Step) -> str:
    judged = f" · SLO p95 {Fmt.ms(st.slo_p95_ms)}" if st.slo_p95_ms is not None else ""
    return (f"{st.users:,} users · p95 {Fmt.ms(st.p95_ms)}{judged} · {Fmt.rps(st.rps)} · {Fmt.pct(st.error_rate)} "
            f"errors · {'passed' if st.passed else 'failed'}")


def steps_view(s: lt.Summary) -> dict[str, Any]:
    steps = s.steps
    if not steps:
        return {"rows": [], "p95": None, "rps": None, "knee": None, "threshold": None, "slo_column": False,
                "p95_title": ""}
    knee_step = s.knee_step
    knee = next((i for i, st in enumerate(steps) if st is knee_step), None)
    users, slo = slo_step(s)
    threshold = _ms_target(slo.target) if slo is not None else None
    # the p95 the SLO judges: when the analysis wrote slo_p95_ms (a subset, e.g. page requests), every column shows it
    slo_column = any(st.slo_p95_ms is not None for st in steps)
    p95_values = [st.slo_p95_ms if slo_column else st.p95_ms for st in steps]
    titles = [step_title(st) for st in steps]
    rows = [{"users": st.users, "window": f"{Fmt.clock(st.start_s)}–{Fmt.clock(st.end_s)}", "rps": st.rps,
             "error_rate": st.error_rate, "p50": st.p50_ms, "p95": st.p95_ms, "p99": st.p99_ms,
             "slo_p95": st.slo_p95_ms, "passed": st.passed, "knee": i == knee, "flag": _flag(st.error_rate)}
            for i, st in enumerate(steps)]
    knee_note = f" The knee step ({knee_step.users:,} users) is shaded." if knee_step is not None else ""
    slo_note = (f" The dashed line is the SLO ({slo.target}), judged at {users:,} users."
                if threshold is not None and slo is not None else "")
    population = "the requests the SLO judges" if slo_column else "all requests"
    return {
        "rows": rows, "knee": knee_step, "threshold": threshold, "slo_column": slo_column,
        "p95_title": "p95 latency per step, as the SLO judges it" if slo_column else "p95 latency per step",
        "p95": step_chart(steps, p95_values, unit="ms", knee=knee, titles=titles, threshold=threshold,
                          threshold_label=(f"SLO {slo.target} · judged at {users:,} users"
                                           if threshold is not None and slo is not None else ""),
                          floor_top=10.0, clip_text=Fmt.short_ms,
                          desc=f"Columns: 95th percentile response time of {population} at each load step, by users "
                               f"per step.{slo_note}{knee_note} Every value is in the load steps table."),
        "rps": step_chart(steps, [st.rps for st in steps], unit="req/s", knee=knee, titles=titles,
                          desc="Columns: requests per second answered at each load step, by users per step."
                               f"{knee_note} Every value is in the load steps table."),
    }


def timeline_view(s: lt.Summary) -> dict[str, Any]:
    pts = s.timeline
    if not pts:
        return {"panels": [], "users": None, "rows": [], "stride": 1, "stride_text": "", "total": 0}
    domain, x_labels = time_axis(max(s.duration_s, pts[-1].t_s))
    pool = s.server.pool_size if s.server is not None else None
    slices = slice_points(pts, domain)
    stride = max(1, math.ceil(len(pts) / MAX_TABLE_ROWS))
    stride_text = Fmt.ordinal(stride)
    table_note = ("Every value is in the timeline table." if stride == 1 else
                  f"The timeline table lists every {stride_text} sample; the caption gives the peak.")
    common = {"domain": domain, "x_labels": x_labels, "slices": slices, "table_note": table_note}

    def series(get: Callable[[lt.Point], float | None]) -> list[tuple[float, float | None]]:
        return [(p.t_s, get(p)) for p in pts]

    def failures(v: float | None) -> str:
        return "—" if v is None else f"{v:,.2f}/s"

    users = line_panel("users", "Simulated users", "users", [{"label": "Users", "tone": "series",
                                                              "points": series(lambda p: p.users)}],
                       area=True, compact=True,
                       summary=f"{len(s.steps)} step{'s' if len(s.steps) != 1 else ''} · peak {s.users_peak:,} users",
                       desc="Number of simulated Locust users over the run.", **common)
    panels = [
        line_panel("rps", "Throughput", "req/s", [{"label": "Requests per second", "tone": "series",
                                                   "points": series(lambda p: p.rps)}],
                   area=True, summary=_peak_note(pts, lambda p: p.rps, Fmt.rps),
                   desc="Requests per second answered by the server over the run.",
                   readout=lambda p: Fmt.rps(p.rps), **common),
        line_panel("failures", "Failures", "failures/s", [{"label": "Failures per second", "tone": "critical",
                                                      "points": series(lambda p: p.failures_per_s)}],
                   summary=_peak_note(pts, lambda p: p.failures_per_s, failures),
                   desc="Failed requests per second over the run.",
                   readout=lambda p: f"{failures(p.failures_per_s)} failures", **common),
        line_panel("latency", "Latency", "ms", [
            {"label": "p95", "tone": "series", "points": series(lambda p: p.p95_ms)},
            {"label": "p50", "tone": "series-light", "points": series(lambda p: p.p50_ms)}],
            summary=_peak_note(pts, lambda p: p.p95_ms, Fmt.ms) + " (p95)", floor_top=10.0, direct_labels=True,
            desc="Median (p50) and 95th percentile (p95) response time over the run.",
            readout=lambda p: f"p95 {Fmt.ms(p.p95_ms)} · p50 {Fmt.ms(p.p50_ms)}", **common),
        line_panel("cpu", "Server CPU", "%", [{"label": "CPU", "tone": "series", "points": series(lambda p: p.cpu_pct)}],
                   percent=True, area=True, summary=_peak_note(pts, lambda p: p.cpu_pct, Fmt.cpu),
                   desc="CPU used by the app server, as a share of the whole machine.",
                   readout=lambda p: f"CPU {Fmt.cpu(p.cpu_pct)}", **common),
        line_panel("rss", "Server memory (RSS)", "MB", [{"label": "RSS", "tone": "series",
                                                          "points": series(lambda p: p.rss_mb)}],
                   zero=False, summary=_peak_note(pts, lambda p: p.rss_mb, Fmt.mb),
                   desc="Resident memory of the app server processes (the axis starts near the lowest value, "
                        "so steady growth shows as a slope).",
                   readout=lambda p: f"RSS {Fmt.mb(p.rss_mb)}", **common),
        line_panel("db", "Database connections", "connections", [{"label": "Connections in use", "tone": "series",
                                                             "points": series(lambda p: p.db_connections)}],
                   area=True, ref=pool, ref_label=f"Pool size {pool:,}" if pool is not None else "",
                   summary=_peak_note(pts, lambda p: p.db_connections, Fmt.num),
                   desc="Database connections in use, against the size of the connection pool.",
                   readout=lambda p: f"{Fmt.num(p.db_connections)} connections"
                                     + (f" (pool {pool:,})" if pool is not None else ""), **common),
    ]
    return {"panels": panels, "users": users, "rows": pts[::stride], "stride": stride, "stride_text": stride_text,
            "total": len(pts)}


def _sort_key(sort: str) -> Callable[[lt.Endpoint], Any]:
    if sort == "name":
        return lambda e: (e.name.lower(), e.method)
    if sort == "requests":
        return lambda e: (-e.requests, e.name.lower())
    if sort == "failures":
        return lambda e: (-e.failures, -e.failure_rate, e.name.lower())
    attr = "p99_ms" if sort == "p99" else "p95_ms"
    return lambda e: (getattr(e, attr) is None, -(getattr(e, attr) or 0.0), e.name.lower())


def page_url(run_id: str | None, **params: str | None) -> str:
    query = {"run": run_id, **params}
    clean = {k: v for k, v in query.items() if v}
    return PAGE_PATH + ("?" + urlencode(clean) if clean else "")


def endpoints_view(s: lt.Summary, run_id: str, sort: str, show_all: bool) -> dict[str, Any]:
    ordered = sorted(s.endpoints, key=_sort_key(sort))
    shown = ordered if show_all else ordered[:TOP_ENDPOINTS]
    rows = [{"e": e, "rate": e.failure_rate, "flag": _flag(e.failure_rate)} for e in shown]
    show = "all" if show_all else None
    return {
        "rows": rows, "total": len(ordered), "shown": len(rows), "sort": sort, "sort_label": SORTS[sort][1],
        "show_all": show_all, "top": TOP_ENDPOINTS,
        "sort_links": [(key, chip, page_url(run_id, sort=None if key == DEFAULT_SORT else key, show=show) + "#endpoints")
                       for key, (chip, _) in SORTS.items()],
        "toggle_url": page_url(run_id, sort=None if sort == DEFAULT_SORT else sort,
                               show=None if show_all else "all") + "#endpoints",
    }


SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
STATUS_ORDER = {"open": 0, "accepted": 1, "fixed": 2}


def plan_text(s: lt.Summary) -> str:
    """The load profile: "25 → 50 → 100 users · 2 min each" (the step length when all steps share it); "—" without
    steps."""
    if not s.steps:
        return "—"
    plan = " → ".join(f"{st.users:,}" for st in s.steps)
    step_lengths = {round(st.end_s - st.start_s) for st in s.steps}
    return f"{plan} users" + (f" · {Fmt.duration(step_lengths.pop())} each" if len(step_lengths) == 1 else "")


# The banner both load-test pages open with (this page and the public /load-test): how much data the app served during
# the run - the rows of the test database (summary.json target.db_rows, seeded before the run), the total and these
# tables in bold, in this order.
DATA_VOLUME_TABLES = (("users", "users"), ("gyms", "gyms"), ("branches", "branches"), ("subscriptions", "memberships"),
                      ("products", "shop products"), ("shop_orders", "shop orders"), ("transactions", "transactions"),
                      ("chat_messages", "chat messages"), ("metric_entries", "height & weight readings"))


def data_volume(s: lt.Summary) -> dict[str, Any] | None:
    """{total, tables, parts: [(rows, label)]} for the data-volume banner; None when the run recorded no row counts.
    (Not "items": in a template ``dv.items`` would be the dict's own method.)"""
    rows = s.target.db_rows
    if not rows:
        return None
    return {"total": sum(rows.values()), "tables": len(rows),
            "parts": [(rows[key], label) for key, label in DATA_VOLUME_TABLES if rows.get(key)]}


def build_view(run: lt.LoadedRun, sort: str, show_all: bool) -> dict[str, Any]:
    s = run.summary
    assert s is not None
    return {
        "s": s, "data_volume": data_volume(s), "kpis": kpis(s), "steps": steps_view(s), "timeline": timeline_view(s),
        "endpoints": endpoints_view(s, run.run_id, sort, show_all),
        "classes": [{"c": c, "rate": c.failure_rate, "flag": _flag(c.failure_rate)}
                    for c in sorted(s.classes, key=lambda c: -c.weight_share)],
        "findings": sorted(s.findings, key=lambda f: (SEVERITY_ORDER[f.severity], STATUS_ORDER[f.status])),
        "open_findings": sum(1 for f in s.findings if f.status == "open"),
        "db_rows": sorted(s.target.db_rows.items(), key=lambda kv: (-kv[1], kv[0])),
        "plan": plan_text(s),
    }


def _display_path(path: Any) -> str:
    try:
        return str(path.relative_to(BASE_DIR))
    except ValueError:
        return str(path)


def _unviewable(run: lt.LoadedRun) -> lt.LoadedRun:
    """``run`` as an error-card run: its data passed the schema but could not be displayed."""
    return dataclasses.replace(run, summary=None, error=DISPLAY_ERROR)


def build_page(run_param: str, sort: str = DEFAULT_SORT, show_all: bool = False) -> dict[str, Any] | None:
    """Everything the template needs (file work: call it in a worker thread). None: the requested run is unknown."""
    root = lt.results_dir()
    listing = lt.list_runs(root)
    run: lt.LoadedRun | None = None
    if run_param:
        run = lt.load_run(run_param, root)
        if run is None:
            return None
    elif listing.runs:
        run = lt.load_run(listing.runs[0].run_id, root)
    view = None
    if run is not None and run.summary is not None:
        try:
            view = build_view(run, sort, show_all)
        except Exception:  # one run's data never takes the page down: it gets an error card
            log.exception("load-test run %s could not be displayed", run.run_id)
            run = _unviewable(run)
    downloads = []
    if run is not None and run.summary is not None:
        for kind, label in (("report.md", "Report"), ("locust.html", "Locust report")):
            found = lt.download_file(run, kind)
            if found is not None:
                downloads.append({"href": f"{PAGE_PATH}/{run.run_id}/{kind}", "label": label, "kind": kind,
                                  "size": Fmt.size(found.size)})
    readable = next((r for r in listing.runs if r.passed is not None and (run is None or r.run_id != run.run_id)), None)
    return {"listing": listing, "run": run, "view": view, "downloads": downloads, "fmt": FMT,
            "results_path": _display_path(listing.root), "newest_readable": readable, "page_url": page_url,
            "schema_version": lt.SCHEMA_VERSION, "max_listed": lt.MAX_LISTED, "max_scan": lt.MAX_SCAN,
            "run_command": RUN_COMMAND, "default_root": lt.DEFAULT_RESULTS_DIR, "display_error": DISPLAY_ERROR,
            # the public page (app/routers/public_loadtest.py) while PUBLIC_LOADTEST_PAGE is on
            "public_url": LOAD_TEST_ITEM.href if get_settings().public_loadtest_page else None}


# ===================================================================================== routes
@router.get(PAGE_PATH)
async def load_test_page(request: Request, admin: SuperAdminUser):
    q = request.query_params
    sort = q.get("sort") or DEFAULT_SORT
    page = await run_in_threadpool(build_page, (q.get("run") or "").strip(), sort if sort in SORTS else DEFAULT_SORT,
                                   q.get("show") == "all")
    if page is None:
        raise NotFound("That load-test run does not exist.")
    context = {"sections": SECTIONS, "section": SECTION, "counts": {}, **page}
    try:
        return render(request, TEMPLATE, context)
    except Exception:
        if page["view"] is None:  # the empty state / error card itself failed: a real bug, let it surface
            raise
        log.exception("load-test run %s could not be rendered", page["run"].run_id)
        context.update(view=None, run=_unviewable(page["run"]), downloads=[])
        return render(request, TEMPLATE, context)


async def _chunks(fh: BinaryIO, size: int) -> AsyncIterator[bytes]:
    try:
        remaining = size
        while remaining > 0:
            chunk = await run_in_threadpool(fh.read, min(DOWNLOAD_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        fh.close()


async def _download(run_id: str, kind: str, db: Any) -> StreamingResponse:
    opened = await run_in_threadpool(lt.open_download, run_id, kind)
    if opened is None:
        raise NotFound("That file is not part of this load-test run.")
    found, fh = opened
    try:
        await db.close()  # nothing else needs the database: no pooled connection is held while the file downloads
    except Exception:
        fh.close()
        raise
    return StreamingResponse(_chunks(fh, found.size), media_type=found.media_type, headers={
        "Content-Disposition": f'attachment; filename="{found.filename}"',
        "Content-Length": str(found.size),
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cache-Control": "no-store, private",
    })


@router.get(PAGE_PATH + "/{run_id}/report.md")
async def load_test_report(run_id: str, db: DB, admin: SuperAdminUser):
    return await _download(run_id, "report.md", db)


@router.get(PAGE_PATH + "/{run_id}/locust.html")
async def load_test_locust_report(run_id: str, db: DB, admin: SuperAdminUser):
    return await _download(run_id, "locust.html", db)
