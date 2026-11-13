"""Draw the README's load-test charts from a run's summary.json (static SVG, light + dark).

    .venv/bin/python loadtest/readme_charts.py loadtest/results/<run> docs/readme

Writes loadtest-p95.svg, loadtest-p95-dark.svg, loadtest-rps.svg and loadtest-rps-dark.svg:

* page response time (the p95 the SLO judges) per load step: one dot per step on a LOG axis - the values run from
  ~20 ms to ~10 s, which a linear axis would flatten to nothing - with a dashed reference line at the 1 s SLO;
* throughput (every request the server answered, the basis of summary.json's steps and capacity) per step: bars.

The steps are categories (plateaus, a spike, a recovery), labelled with their users and their role (SLO step, last
step within the SLO, knee, spike, recovery). One hue, selective direct labels, recessive grid and axes, two charts
instead of one dual-axis chart. Colours are the validated reference palette (categorical slot 1, light and dark
steps, the documented surfaces and ink). Each SVG has a <title> and a <desc> that lists every value.
"""

from __future__ import annotations

import json
import math
import sys
from html import escape
from pathlib import Path
from typing import Any

THEMES = {
    "light": {"surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781", "grid": "#e1e0d9",
              "axis": "#c3c2b7", "series": "#2a78d6"},
    "dark": {"surface": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#898781", "grid": "#2c2c2a",
             "axis": "#383835", "series": "#3987e5"},
}
W, H = 760, 330
PAD_L, PAD_R, PAD_T, PAD_B = 66, 20, 50, 74
PW, PH = W - PAD_L - PAD_R, H - PAD_T - PAD_B
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"


def nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(v))
    for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if v <= m * mag:
            return m * mag
    return 10 * mag


def fmt_ms(v: float) -> str:
    if v >= 1000:
        return f"{v / 1000:.1f}".removesuffix(".0") + " s"
    return f"{v:,.0f} ms"


def roles(steps: list[dict[str, Any]], summary: dict[str, Any]) -> list[str]:
    """The second x-axis line of each step: SLO step, last within the SLO, knee, spike, recovery (or nothing)."""
    cap = summary.get("capacity") or {}
    slo_users = next((int(r["users"]) for r in (summary.get("verdict") or {}).get("slos") or [] if r.get("users")), None)
    users = [int(s.get("users") or 0) for s in steps]
    peak_i = users.index(max(users)) if users else -1
    out, used = [], set()
    for i, u in enumerate(users):
        role = ""
        if i > peak_i >= 0 and len(steps) > 2:
            role = "recovery"
        elif i == peak_i and len(steps) > 2:
            role = "spike"
        elif u == slo_users and "slo" not in used:
            role, _ = "SLO step", used.add("slo")
        elif u == cap.get("knee_users"):
            role = "knee"
        elif u == cap.get("max_users_within_slo"):
            role = "last in SLO"
        out.append(role)
    return out


def frame(title: str, desc: str, t: dict[str, str]) -> list[str]:
    return [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" '
            f'aria-labelledby="t d"><title id="t">{escape(title)}</title><desc id="d">{escape(desc)}</desc>',
            f'<rect width="{W}" height="{H}" rx="12" fill="{t["surface"]}"/>',
            f'<text x="{PAD_L}" y="30" font-family="{FONT}" font-size="15" font-weight="600" fill="{t["ink"]}">'
            f'{escape(title)}</text>']


def x_axis(steps: list[dict[str, Any]], role: list[str], t: dict[str, str]) -> list[str]:
    slot = PW / max(len(steps), 1)
    parts = []
    for i, s in enumerate(steps):
        cx = PAD_L + slot * (i + 0.5)
        parts.append(f'<text x="{cx:.1f}" y="{PAD_T + PH + 19:.1f}" text-anchor="middle" font-family="{FONT}" '
                     f'font-size="12" font-weight="500" fill="{t["ink2"]}">{int(s.get("users") or 0):,}</text>')
        if role[i]:
            parts.append(f'<text x="{cx:.1f}" y="{PAD_T + PH + 35:.1f}" text-anchor="middle" font-family="{FONT}" '
                         f'font-size="10.5" fill="{t["muted"]}">{escape(role[i])}</text>')
    parts.append(f'<text x="{PAD_L + PW / 2:.1f}" y="{H - 10}" text-anchor="middle" font-family="{FONT}" '
                 f'font-size="11" fill="{t["muted"]}">simultaneous users at each load step (in the order they ran)</text>')
    return parts


def latency_chart(steps: list[dict[str, Any]], role: list[str], theme: str, slo_ms: float = 1000.0) -> str:
    """Page p95 per step as dots on a log axis (10 ms .. 10^k ms), joined in run order, with the SLO line."""
    t = THEMES[theme]
    vals = [float(s.get("slo_p95_ms") or s.get("p95_ms") or 0.0) for s in steps]
    lo, hi = 1.0, math.log10(max(vals + [slo_ms * 10]) * 2.2)  # log10: 10 ms .. headroom over the top value
    y = lambda v: PAD_T + PH - (math.log10(max(v, 10.0)) - lo) / (hi - lo) * PH  # noqa: E731
    slot = PW / max(len(vals), 1)
    xs = [PAD_L + slot * (i + 0.5) for i in range(len(vals))]
    title = "Page response time (95th percentile) at each load step"
    desc = (f"{title}, log scale, SLO under {fmt_ms(slo_ms)}: "
            + "; ".join(f"{int(s.get('users') or 0):,} users{' (' + r + ')' if r else ''}: {fmt_ms(v)}"
                        for s, r, v in zip(steps, role, vals)))
    parts = frame(title, desc, t)
    for k in range(int(lo), math.floor(hi) + 1):  # one gridline per decade
        v = 10.0 ** k
        yy = y(v)
        parts.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{yy:.1f}" y2="{yy:.1f}" '
                     f'stroke="{t["axis"] if k == lo else t["grid"]}" stroke-width="1"/>')
        parts.append(f'<text x="{PAD_L - 10}" y="{yy + 4:.1f}" text-anchor="end" font-family="{FONT}" font-size="11" '
                     f'fill="{t["muted"]}">{fmt_ms(v)}</text>')
    parts.append(f'<text x="16" y="{PAD_T + PH / 2:.1f}" transform="rotate(-90 16 {PAD_T + PH / 2:.1f})" '
                 f'text-anchor="middle" font-family="{FONT}" font-size="11" fill="{t["muted"]}">p95, log scale</text>')
    ys = y(slo_ms)  # SLO reference line, labelled at the left where the low steps leave room
    parts.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{ys:.1f}" y2="{ys:.1f}" stroke="{t["ink2"]}" '
                 f'stroke-width="1.5" stroke-dasharray="6 4"/>')
    parts.append(f'<text x="{PAD_L + 8}" y="{ys - 7:.1f}" font-family="{FONT}" font-size="11" font-weight="500" '
                 f'fill="{t["ink2"]}">SLO: page p95 under {fmt_ms(slo_ms)}</text>')
    pts = " ".join(f"{x:.1f},{y(v):.1f}" for x, v in zip(xs, vals))
    parts.append(f'<polyline points="{pts}" fill="none" stroke="{t["series"]}" stroke-width="2" '
                 f'stroke-linejoin="round" stroke-linecap="round"/>')
    for i, (x, v) in enumerate(zip(xs, vals)):
        parts.append(f'<circle cx="{x:.1f}" cy="{y(v):.1f}" r="5.5" fill="{t["series"]}" stroke="{t["surface"]}" '
                     f'stroke-width="2"><title>{int(steps[i].get("users") or 0):,} users: {fmt_ms(v)}</title></circle>')
        if role[i]:  # selective direct labels: the steps with a role
            prev_hi = i > 0 and vals[i - 1] > v * 1.5
            next_hi = i + 1 < len(vals) and vals[i + 1] > v * 1.5
            below = prev_hi and (next_hi or i + 1 == len(vals))  # the line arrives from above: label under the dot
            rising = i > 0 and vals[i - 1] * 1.5 < v and next_hi  # line from lower-left to upper-right: label upper-left
            tx, ty, anchor = (x, y(v) + 20, "middle") if below else (x - 9, y(v) - 7, "end") if rising else \
                (x, y(v) - 11, "middle")
            parts.append(f'<text x="{tx:.1f}" y="{ty:.1f}" text-anchor="{anchor}" font-family="{FONT}" font-size="11.5" '
                         f'font-weight="600" fill="{t["ink"]}">{fmt_ms(v)}</text>')
    parts += x_axis(steps, role, t)
    parts.append("</svg>")
    return "".join(parts)


def throughput_chart(steps: list[dict[str, Any]], role: list[str], theme: str) -> str:
    """Requests per second per step (every request the server answered) as bars from zero."""
    t = THEMES[theme]
    vals = [float(s.get("rps") or 0.0) for s in steps]
    top = nice_max(max(vals or [1.0]) * 1.1)
    y = lambda v: PAD_T + PH - (v / top) * PH  # noqa: E731
    slot = PW / max(len(vals), 1)
    bw = min(48.0, slot * 0.58)
    title = "Throughput: requests per second at each load step"
    desc = (f"{title} (every request the server answered): "
            + "; ".join(f"{int(s.get('users') or 0):,} users{' (' + r + ')' if r else ''}: {v:,.0f} requests/s"
                        for s, r, v in zip(steps, role, vals)))
    parts = frame(title, desc, t)
    for k in range(5):
        v = top * k / 4
        yy = y(v)
        parts.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{yy:.1f}" y2="{yy:.1f}" '
                     f'stroke="{t["axis"] if k == 0 else t["grid"]}" stroke-width="1"/>')
        parts.append(f'<text x="{PAD_L - 10}" y="{yy + 4:.1f}" text-anchor="end" font-family="{FONT}" font-size="11" '
                     f'fill="{t["muted"]}">{v:,.0f}</text>')
    parts.append(f'<text x="16" y="{PAD_T + PH / 2:.1f}" transform="rotate(-90 16 {PAD_T + PH / 2:.1f})" '
                 f'text-anchor="middle" font-family="{FONT}" font-size="11" fill="{t["muted"]}">requests / s</text>')
    base = PAD_T + PH
    peak_i = max(range(len(vals)), key=lambda i: vals[i]) if vals else -1
    for i, v in enumerate(vals):
        cx = PAD_L + slot * (i + 0.5)
        x0, y0 = cx - bw / 2, y(v)
        r = min(4.0, max(base - y0, 0.0) / 2, bw / 2)
        parts.append(f'<path d="M{x0:.1f},{base:.1f} V{y0 + r:.1f} Q{x0:.1f},{y0:.1f} {x0 + r:.1f},{y0:.1f} '
                     f'H{x0 + bw - r:.1f} Q{x0 + bw:.1f},{y0:.1f} {x0 + bw:.1f},{y0 + r:.1f} V{base:.1f} Z" '
                     f'fill="{t["series"]}"><title>{int(steps[i].get("users") or 0):,} users: {v:,.0f} requests/s'
                     f'</title></path>')
        if i == peak_i or role[i] in ("SLO step", "last in SLO"):
            parts.append(f'<text x="{cx:.1f}" y="{y0 - 8:.1f}" text-anchor="middle" font-family="{FONT}" '
                         f'font-size="11.5" font-weight="600" fill="{t["ink"]}">{v:,.0f}</text>')
    parts += x_axis(steps, role, t)
    parts.append("</svg>")
    return "".join(parts)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__.strip().splitlines()[2].strip())
        return 2
    run, out = Path(sys.argv[1]), Path(sys.argv[2])
    s = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    steps = [st for st in (s.get("steps") or []) if (st.get("users") or 0) > 0]
    role = roles(steps, s)
    out.mkdir(parents=True, exist_ok=True)
    for theme in ("light", "dark"):
        sfx = "" if theme == "light" else "-dark"
        (out / f"loadtest-p95{sfx}.svg").write_text(latency_chart(steps, role, theme), encoding="utf-8")
        (out / f"loadtest-rps{sfx}.svg").write_text(throughput_chart(steps, role, theme), encoding="utf-8")
    print(f"wrote 4 SVGs to {out} from {run.name}: {len(steps)} steps, roles {[r for r in role if r]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
