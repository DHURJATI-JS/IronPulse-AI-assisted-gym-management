"""Turn a load-test run into ``<run dir>/report.md`` + ``summary.json`` (standard library only - any Python 3.10+).

    .venv-loadtest/bin/python loadtest/analyze.py [loadtest/results/<run>]     (default: the current run)
    .venv-loadtest/bin/python loadtest/analyze.py <run dir> --slo-users 200    (the load step the SLOs are judged at)

Inputs (all in the run directory): requests_*.csv (one row per request, written by the locustfile's request hook),
locust_stats.csv / locust_stats_history.csv / locust_exceptions.csv (Locust --csv), locust.log, run_meta.json (shape +
start), monitor.csv + pg_slow.csv (monitor.py), server.log (uvicorn access + app log), server_config.json +
server_ready.json + preflight_server.json (launcher).

How the verdict works:
* The production SLOs are judged ONLY inside the SLO step (the first step with exactly --slo-users users), on its
  steady part: error rate < 1 % (abusers excluded; AI excluded when the run had no AI provider), page p95 < 1 s, page
  p99 < 2.5 s, zero 5xx and zero DB statement / pool timeouts in that step. No step with exactly that many users ->
  the SLO rows are "n/a" and the result is INCONCLUSIVE, never PASS.
* 5xx / 503 (the app's load shedding: pool or statement timeout -> 503 + Retry-After) and timeouts in the deliberate
  overload steps are reported per step as evidence of the knee, not as a failure. The whole run fails only on an
  unhandled 500, a Traceback / "Exception in ASGI application", a worker restart, a crashed task of the kit, or
  abusers that were never throttled.
* Abusers ("ABUSE ..." stats names): every attack pattern must be refused as designed (429 / 403, 404 for the scan,
  the WebSocket flood closed with 4429 or refused at the handshake) and no abuse request may fail - except a 503, a
  transport / connect timeout or a socket turned away with 1013 "try again later" in a step where the normal users
  were shed too (overload, not the abuse - the junk verdict's rule; reported separately, not counted). The abuser's
  own sign-in ("(own account)") and the sign-in page it loads first are setup: shown, never judged as abuse.
* The knee is searched only in the rising steps (up to the peak); the pre-spike step is the largest step before the
  peak; steps after the peak are the recovery (time until page p95 < 1 s and errors < 1 % again).
* Memory: worker USS (memory unique to the process); a leak verdict needs >= 10 minutes at constant load (LT_PROFILE=
  soak) - in the 10-minute step test it is shown as information only.
* Random junk (the JunkUser class, stats names "JUNK ..."): kept out of the normal users' SLOs and judged by its own
  row "zero 5xx under random junk input" (every 5xx / timeout / connection error / WebSocket 1011 of a junk request,
  but a 503 or timeout in a step where the normal users were shed too) plus "junk: other users' objects refused, no
  open redirect, junk rendered escaped"; report.md lists each failing junk input with the server's traceback
  (junk_failures_*.jsonl + server.log). "signed in elsewhere" anywhere fails the run too (one session per account:
  two simulated users shared an account).
* A step is CONTAMINATED when other programs used more than one core, the 1-minute load average exceeded the CPU
  count (runnable threads queue for a core: every latency includes that wait) or the cluster had > 90 connections; the load
  generator is SATURATED when one of its processes went above 80 % CPU (or Locust warned). Either at the SLO step ->
  INCONCLUSIVE.

summary.json (docs/load-test-page.md, schema version 1) is validated BEFORE it is written by the app's own loader -
``app.services.loadtest_results.parse_summary`` run with the project interpreter (.venv/bin/python, or
LT_PROJECT_PYTHON). A rejection is a hard error (exit status 2) naming the field path; nothing is written then.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import platform
import re
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ltenv  # noqa: E402 - standard library only (junk_ip: which server.log 5xx went to a JunkUser address)

PROJECT_PYTHON = HERE.parent / ".venv" / "bin" / "python"
AI_NAMES = ("/api/assistant/chat", "/api/plans/ask", "/api/shop/products/[id]/ask")
SLO = {"error_rate": 0.01, "page_p95_ms": 1000.0, "page_p99_ms": 2500.0, "uss_mb_per_min": 3.0}
LEAK_MIN_CONSTANT_S = 600.0  # a memory verdict needs this much constant load
OTHER_CPU_LIMIT = 100.0  # % of one core used by programs that are not this test
SYS_CPU_SATURATION = 85.0  # % of ALL cores: other programs only skew a step when they compete for a nearly full machine
CLUSTER_CONN_LIMIT = 90
LOADGEN_PROC_LIMIT = 80.0
IST = timezone(timedelta(hours=5, minutes=30))
BACKGROUND = {  # timestamped app log lines of the periodic jobs -> short label
    "low-stock digest": "low-stock digest", "bedtime reminder": "bedtime reminders", "retention": "retention",
    "subscriptions": "subscriptions", "payroll": "payroll", "identity sweep": "identity sweep",
    "triage": "security triage", "expire": "expiry", "product rating": "ratings", "purge": "purge",
}
# Abusers: the answers each attack pattern must get at least once (a pattern never refused was never throttled); the
# WebSocket flood must be closed with 4429 or refused at the handshake (its rows are ok then - ltlib/ws.py ws_flood)
ABUSE_EXPECT_REFUSAL = {"(unknown account)": ("429", "403"), "(flood)": ("429", "403"),
                        "(identical burst)": ("429", "403"), "[scanner signature]": ("403", "400"),
                        "[404 directory scan]": ("404", "403", "429")}
# A socket failure the server's overload explains: a connect / handshake timeout, a connection it never served, or
# 1013 "try again later" (app/routers/core.py: the session check could not reach the database). Never "flood not
# stopped" (the server kept accepting the flood) or 1011 (an internal error).
WS_OVERLOAD = re.compile(r"timed out|timeout|\b1013\b|try again later|connection refused|connection reset|"
                         r"connection aborted|remote host was lost|broken pipe", re.IGNORECASE)


def abuse_setup(name: str) -> bool:
    """A step of the abuser that is not an attack: signing in to its own lt_abuser account before the AI / WebSocket
    floods ("(own account)", and the page it follows to) and loading the sign-in page for its CSRF token first.
    Shown in the abusers table, never judged as abuse (nothing there is meant to be refused)."""
    return "(own account)" in name or name == "ABUSE GET /auth/login"


def overload_artefact(r: dict[str, Any]) -> str | None:
    """What a FAILED request has to do with the server's overload: "503" (its load shedding), "timeout" (an HTTP
    request without an answer - a transport error - or a socket timed out / refused / turned away with 1013), else None
    (a real failure). Whether it is excused depends on its step (``shed_step``)."""
    if r["ok"]:
        return None
    if r["status"] == "503":
        return "503"
    if r["status"] in ("", "0"):
        if r["type"] == "WS":
            err = r["err"] or ""
            return "timeout" if WS_OVERLOAD.search(err) and not err.startswith("flood not stopped") else None
        return "timeout"
    return None


def step_at(step_rows: list[dict[str, Any]], t0: float, t: float) -> dict[str, Any] | None:
    for s in step_rows:
        if s["start_s"] + t0 <= t < s["end_s"] + t0:
            return s
    return None


def shed_step(step: dict[str, Any] | None) -> bool:
    """Were the NORMAL users of this step shed too - a 503, a transport error, or a socket turned away for overload
    (1013 / connect timeout)? Then the server was overloaded and an abuser's 503 / timeout there is no abuse failure."""
    return bool(step and (step["normal_503"] or step["normal_transport"] or step["normal_ws_shed"]))


# ------------------------------------------------------------------------------------------------ helpers
def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = max(0, min(len(s) - 1, math.ceil(p / 100.0 * len(s)) - 1))
    return s[k]


def fin(v: Any) -> Any:
    """None for NaN / infinity (summary.json must be finite)."""
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return None
    return v


def fmt_ms(v: float) -> str:
    return "-" if v is None or v != v else (f"{v:,.0f}" if abs(v) >= 10 else f"{v:.1f}")


def fmt_pct(v: float) -> str:
    return "-" if v is None or v != v else f"{100 * v:.2f}%"


def category(name: str, rtype: str) -> str:
    if name.startswith("JUNK"):
        return "junk"
    if rtype == "WS":
        return "abuse" if name.startswith("ABUSE") else "ws"
    if rtype in ("TASK", "NOTE"):
        return rtype.lower()
    if name.startswith("ABUSE"):
        return "abuse"
    if any(a in name for a in AI_NAMES):
        return "ai"
    base = name.split(" -> ")[-1]
    if any(s in base for s in ("/static/", "/media/", "/maps/tiles/")):
        return "static"
    if "/api/" in base:
        return "api"
    if base.startswith("GET "):
        return "page"
    return "form"


def slope_per_min(points: list[tuple[float, float]]) -> float:
    """Least-squares slope of (seconds, value) in value per minute."""
    if len(points) < 3:
        return float("nan")
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return float("nan")
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den * 60.0


def table(headers: list[str], rows: list[list[Any]], align: str | None = None) -> str:
    out = ["| " + " | ".join(headers) + " |"]
    out.append("|" + "|".join(("---:" if (align or "l" + "r" * (len(headers) - 1))[i] == "r" else "---")
                              for i in range(len(headers))) + "|")
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |")
    return "\n".join(out)


def fnum(m: dict[str, str], key: str) -> float | None:
    try:
        v = m.get(key, "")
        return float(v) if v not in ("", None) else None
    except ValueError:
        return None


# ------------------------------------------------------------------------------------------------ loading
def load_requests(rd: Path, t_min: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    dropped = 0
    for path in sorted(glob.glob(str(rd / "requests_*.csv"))):
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                try:
                    row = {"t": float(r["t"]), "cls": r["cls"], "type": r["type"], "name": r["name"],
                           "ms": float(r["ms"]), "bytes": int(float(r.get("bytes") or 0)), "ok": r["ok"] == "1",
                           "status": r["status"], "err": r["err"], "note": r.get("note") or ""}
                except (KeyError, ValueError):
                    continue
                if row["t"] < t_min:
                    dropped += 1  # an earlier attempt that wrote into the same run folder
                    continue
                (notes if row["type"] == "NOTE" else rows).append(row)
    rows.sort(key=lambda r: r["t"])
    notes.sort(key=lambda r: r["t"])
    return rows, notes, dropped


def load_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# Secrets a junk sample can carry (the shared test password, CSRF / submit / device tokens, codes): report.md and
# summary.json are shared, so every sample is redacted before it is used - the request shape stays readable.
_SECRET_FIELD = r"(?:[\w.\[\]-]*(?:password|passwd|secret|token|otp|api[_-]?key|session|cookie)[\w.\[\]-]*|submit_id|device_id|account_ref|code)"
_SECRET_FORM = re.compile(r"(?i)(^|[?&;\s`])(" + _SECRET_FIELD + r")=([^&\s`]*)")
_SECRET_JSON = re.compile(r'(?i)("' + _SECRET_FIELD + r'"\s*:\s*)("(?:[^"\\]|\\.)*"|[^,}\s]+)')


def redact(text: Any) -> Any:
    """A junk sample with its secret values replaced by [redacted] (form fields, query strings and JSON)."""
    if not isinstance(text, str) or not text:
        return text
    out = _SECRET_FORM.sub(lambda m: f"{m.group(1)}{m.group(2)}=[redacted]", text)
    out = _SECRET_JSON.sub(lambda m: f'{m.group(1)}"[redacted]"', out)
    for secret in {ltenv.PASSWORD, quote(ltenv.PASSWORD, safe=""), quote_plus(ltenv.PASSWORD)}:
        if secret:
            out = out.replace(secret, "[redacted]")
    return out


def load_junk_failures(rd: Path, t_min: float) -> list[dict[str, Any]]:
    """junk_failures_<pid>.jsonl (locustfile.py JunkUser): every failed junk request with the input that did it
    (secrets redacted - see ``redact``)."""
    out: list[dict[str, Any]] = []
    for path in sorted(glob.glob(str(rd / "junk_failures_*.jsonl"))):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and float(rec.get("t") or 0) >= t_min:
                    for key in ("what", "body", "url", "error"):
                        rec[key] = redact(rec.get(key))
                    out.append(rec)
    out.sort(key=lambda r: float(r.get("t") or 0))
    return out


ERROR_HEAD = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ (ERROR|CRITICAL)\s+[\w.]+: (.*)$")
UNHANDLED = re.compile(r"unhandled error on (\S+) (\S+)")


def scan_error_blocks(path: Path) -> list[dict[str, Any]]:
    """ERROR / CRITICAL app log lines of server.log with the traceback that follows each ("unhandled error on GET /x"
    from app/core/errors.py, or uvicorn's "Exception in ASGI application") - matched to the junk 5xx by path + time."""
    blocks: list[dict[str, Any]] = []
    if not path.is_file():
        return blocks
    cur: dict[str, Any] | None = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            head = ERROR_HEAD.match(line)
            if head or line.startswith("ERROR:    Exception in ASGI application"):
                if cur is not None:
                    blocks.append(cur)
                ts = local_ts(head.group(1)) if head else None
                msg = head.group(3) if head else line[10:]
                m = UNHANDLED.search(msg)
                cur = {"ts": ts, "msg": msg[:300], "method": m.group(1) if m else "", "path": m.group(2) if m else "",
                       "tb": []}
                continue
            if cur is None:
                continue
            if ACCESS.match(line) or APP_LINE.match(line) or (UVICORN_LINE.match(line) and not line.startswith("ERROR")):
                blocks.append(cur)
                cur = None
                continue
            if len(cur["tb"]) < 400:
                cur["tb"].append(line)
    if cur is not None:
        blocks.append(cur)
    for b in blocks:
        tb = [x for x in b["tb"] if x.strip()]
        b["exception"] = next((x.strip() for x in reversed(tb) if re.match(r"^\s*[\w.]+(Error|Exception|Exit|Warning)\b", x)
                               or re.match(r"^[\w.]+: ", x.strip())), (tb[-1].strip() if tb else ""))[:300]
        b["tb_tail"] = "\n".join(tb[-14:])
    return blocks


ACCESS = re.compile(r'^INFO:\s+(\S+) - "(\S+) (\S+) HTTP/[\d.]+" (\d{3})')
APP_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ (\w+)\s+([\w.]+): (.*)$")
UVICORN_LINE = re.compile(r"^(INFO|WARNING|ERROR|CRITICAL):\s+(.*)$")


def local_ts(text: str) -> float | None:
    try:
        return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def scan_log(path: Path, workers: int) -> dict[str, Any]:
    out: dict[str, Any] = {"lines": 0, "access": 0, "status": Counter(), "5xx": [], "traceback": 0, "levels": Counter(),
                           "timeouts_ts": [], "timeouts_untimed": 0, "errors": [], "warnings": Counter(), "bans": 0,
                           "background": [], "unhandled": 0, "asgi_exception": 0, "worker_died": [],
                           "server_started": 0, "uvicorn": Counter(), "startup": [], "redis_down": [], "5xx_junk": 0,
                           "status_junk": Counter()}
    if not path.is_file():
        return out
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            out["lines"] += 1
            m = ACCESS.match(line)
            if m:
                out["access"] += 1
                code = int(m.group(4))
                out["status"][code] += 1
                junk_addr = ltenv.junk_ip(m.group(1).rsplit(":", 1)[0])
                if junk_addr:
                    out["status_junk"][code] += 1
                if code >= 500:
                    out["5xx"].append(f"{m.group(2)} {m.group(3)[:200]} -> {code}" + (" (junk sender)" if junk_addr else ""))
                    out["5xx_junk"] += junk_addr
                continue
            if "Traceback (most recent call last)" in line:
                out["traceback"] += 1
            u = UVICORN_LINE.match(line)
            if u:
                level, msg = u.groups()
                out["uvicorn"][level] += 1
                if "Started server process" in msg:
                    out["server_started"] += 1
                if re.search(r"Child process \[\d+\] (?:died|failed to start)", msg):  # not the normal stop's
                    # "Waiting for child process" (a hung worker logs that too, but always followed by "died")
                    out["worker_died"].append(msg.strip()[:160])
                if "Exception in ASGI application" in msg:
                    out["asgi_exception"] += 1
                if level in ("ERROR", "CRITICAL") and len(out["errors"]) < 15:
                    out["errors"].append(line.strip()[:240])
                if level == "WARNING":
                    out["warnings"][f"uvicorn: {re.sub(r'[0-9]+', '#', msg)[:110]}"] += 1
                continue
            a = APP_LINE.match(line)
            if a:
                stamp, level, logger, msg = a.groups()
                ts = local_ts(stamp)
                out["levels"][level] += 1
                low = msg.lower()
                if msg.startswith("database statement timeout") or msg.startswith("database pool timeout"):
                    out["timeouts_ts"].append((ts, "statement" if "statement" in msg else "pool", msg[:200]))
                if msg.startswith("unhandled error"):
                    out["unhandled"] += 1
                if msg.startswith("Shared security limits: Redis unavailable"):
                    out["redis_down"].append(ts)  # limits fall back to each worker's memory until Redis answers
                if level in ("ERROR", "CRITICAL") and len(out["errors"]) < 15:
                    out["errors"].append(line.strip()[:240])
                if level == "WARNING":
                    key = re.sub(r"\d+(\.\d+)*", "#", msg)[:110]
                    out["warnings"][f"{logger}: {key}"] += 1
                    if " banned until " in msg or "banned permanently" in msg:
                        out["bans"] += 1
                    if "SIMULATED AI" in msg or "budget" in low or "production" in low:
                        if len(out["startup"]) < 6 and msg[:120] not in out["startup"]:
                            out["startup"].append(msg[:120])
                for k, label in BACKGROUND.items():
                    if k in low and level in ("INFO", "WARNING"):
                        out["background"].append((ts, label))
                        break
            elif "QueryCanceled" in line or "QueuePool limit" in line:
                out["timeouts_untimed"] += 1
    return out


def scan_locust_log(path: Path) -> list[tuple[float, str]]:
    """Locust's own warnings worth knowing (CPU of the generator, stopped users) with their time."""
    out: list[tuple[float, str]] = []
    if not path.is_file():
        return out
    rx = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+\] [^/]*/(WARNING|ERROR)/[^:]*: (.*)$")
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = rx.match(line.strip())
            if m and ("CPU usage" in m.group(3) or m.group(2) == "ERROR"):
                ts = local_ts(m.group(1))
                if ts is not None:
                    out.append((ts, m.group(3)[:160]))
    return out


# ------------------------------------------------------------------------------------------------ analysis
def build_steps(meta: dict[str, Any], t0: float, t_end: float) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    prev_end, prev_users = 0.0, 0
    for end, users, rate in meta.get("steps") or []:
        users, rate = int(users), float(rate) or 1.0
        span = float(end) - prev_end
        ramp = abs(users - prev_users) / rate
        steady = prev_end + min(ramp + 5.0, span / 2.0)
        steps.append({"users": users, "prev": prev_users, "rate": rate, "start": t0 + prev_end, "end": t0 + float(end),
                      "steady": t0 + steady, "ramp_s": ramp})
        prev_end, prev_users = float(end), users
    if not steps:
        steps.append({"users": 0, "prev": 0, "rate": 1.0, "start": t0, "end": t_end, "steady": t0, "ramp_s": 0.0})
    steps[-1]["end"] = max(steps[-1]["end"], t_end + 0.001)
    peak_i = max(range(len(steps)), key=lambda i: (steps[i]["users"], -i))
    for i, st in enumerate(steps):
        if i > peak_i:
            st["role"] = "recovery" if st["users"] < steps[peak_i]["users"] else "after peak"
        elif i == peak_i:
            st["role"] = "peak" if i > 0 and st["users"] > st["prev"] else "constant"
        else:
            st["role"] = "rising" if st["users"] > st["prev"] else ("constant" if st["users"] == st["prev"] else "falling")
    before = [i for i in range(peak_i) if steps[i]["role"] in ("rising", "constant")]
    if before and len(steps) > 2:
        steps[max(before, key=lambda i: (steps[i]["users"], i))]["pre_spike"] = True
    return steps


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ms = [r["ms"] for r in rows]
    fails = sum(1 for r in rows if not r["ok"])
    return {"n": len(rows), "fail": fails, "rate": fails / len(rows) if rows else float("nan"),
            "p50": pct(ms, 50), "p95": pct(ms, 95), "p99": pct(ms, 99), "max": max(ms) if ms else float("nan"),
            "avg": statistics.fmean(ms) if ms else float("nan")}


def window(rows: list[dict[str, Any]], a: float, b: float) -> list[dict[str, Any]]:
    return [r for r in rows if a <= r["t"] < b]


def mon_window(mon: list[dict[str, str]], a: float, b: float) -> list[dict[str, str]]:
    return [m for m in mon if m.get("ts") and a <= float(m["ts"]) < b]


def counter_delta(mon: list[dict[str, str]], key: str, a: float, b: float) -> int | None:
    """Increase of a cumulative monitor counter between the last sample before ``a`` and the last sample before ``b``."""
    before = [m for m in mon if m.get("ts") and float(m["ts"]) < a and m.get(key) not in ("", None)]
    inside = [m for m in mon if m.get("ts") and float(m["ts"]) < b and m.get(key) not in ("", None)]
    if not inside:
        return None
    start = int(float(before[-1][key])) if before else 0
    return int(float(inside[-1][key])) - start


def analyze(rd: Path, slo_users: int) -> tuple[str, dict[str, Any], dict[str, Any]]:
    meta = load_json(rd / "run_meta.json")
    cfg = load_json(rd / "server_config.json")
    ready = load_json(rd / "server_ready.json")
    pre_srv = load_json(rd / "preflight_server.json")
    seed = load_json(HERE / "seed_summary.json")
    workers = int(cfg.get("workers") or 4)
    ai_mode = cfg.get("ai_mode") or meta.get("ai_mode") or "?"
    t_meta = float(meta.get("start") or 0)
    rows, notes, dropped = load_requests(rd, t_meta - 1.0 if t_meta else 0.0)
    stats = load_csv(rd / "locust_stats.csv")
    history = load_csv(rd / "locust_stats_history.csv")
    exceptions = load_csv(rd / "locust_exceptions.csv")
    srv_start = float(ready.get("started_at") or 0) or (t_meta - 3600 if t_meta else 0)
    mon = [m for m in load_csv(rd / "monitor.csv") if m.get("ts") and float(m["ts"]) >= srv_start - 1]
    slow = load_csv(rd / "pg_slow.csv")
    log = scan_log(rd / "server.log", workers)
    lwarn = scan_locust_log(rd / "locust.log")

    if not rows and not stats:
        raise SystemExit(f"no request data in {rd} (requests_*.csv / locust_stats.csv)")
    t0 = t_meta or (rows[0]["t"] if rows else 0)
    t_end = rows[-1]["t"] if rows else t0
    duration = max(1.0, t_end - t0)
    steps = build_steps(meta, t0, t_end)
    for r in rows:
        r["cat"] = category(r["name"], r["type"])
        r["junk"] = r["cls"] == "JunkUser" or r["cat"] == "junk"
        r["abuse"] = (r["cls"] == "Abuser" or r["cat"] == "abuse") and not r["junk"]
        if r["junk"] and r["cat"] == "task":
            r["cat"] = "task"  # a crashed JunkUser task is a kit error like any other
        elif r["junk"]:
            r["cat"] = "junk"
    no_ai = ai_mode == "off"
    junk_rows = [r for r in rows if r["junk"] and r["cat"] != "task"]
    junk_fail = load_junk_failures(rd, t_meta - 1.0 if t_meta else 0.0)
    log_errors = scan_error_blocks(rd / "server.log")

    normal = [r for r in rows if not r["abuse"] and not r["junk"] and r["cat"] not in ("task",)]
    slo_rows = [r for r in normal if not (no_ai and r["cat"] == "ai")]  # error-rate / RPS basis
    pages = [r for r in normal if r["cat"] == "page"]
    abuse = [r for r in rows if r["abuse"]]
    task_errors = [r for r in rows if r["cat"] == "task"]
    ncpu = int((cfg.get("platform") or {}).get("cpus") or os.cpu_count() or 1)

    # ---- per step
    step_rows: list[dict[str, Any]] = []
    for st in steps:
        a, b, s = st["start"], st["end"], st["steady"]
        win = window(slo_rows, s, b)
        allwin = window(rows, s, b)
        full_all = window(rows, a, b)
        pw = [r for r in win if r["cat"] == "page"]
        s_all, s_pages, s_every = summarize(win), summarize(pw), summarize([r for r in allwin if r["cat"] != "task"])
        span = max(1.0, b - s)
        mw = mon_window(mon, s, b)
        mfull = mon_window(mon, a, b)
        cpu = [v for v in (fnum(m, "srv_cpu_pct") for m in mw) if v is not None]
        pg_cpu = [v for v in (fnum(m, "pg_backend_cpu_pct") for m in mw) if v is not None]
        uss = [(float(m["ts"]), v) for m in mw if (v := fnum(m, "srv_uss_mb")) is not None and v > 0]
        lg_max = [v for v in (fnum(m, "loadgen_cpu_max_proc") for m in mfull) if v is not None]
        other = [v for v in (fnum(m, "other_cpu_pct") for m in mfull) if v is not None]
        sysc = [v for v in (fnum(m, "sys_cpu_pct") for m in mfull) if v is not None]
        load1 = [v for v in (fnum(m, "load1") for m in mfull) if v is not None]
        cluster = [v for v in (fnum(m, "pg_cluster_total") for m in mfull) if v is not None]
        pgt = [v for v in (fnum(m, "pg_total") for m in mfull) if v is not None]
        pga = [v for v in (fnum(m, "pg_active") for m in mfull) if v is not None]
        tw = [v for v in (fnum(m, "tcp_time_wait") for m in mfull) if v is not None]
        ws = [r for r in window(normal, s, b) if r["type"] == "WS" and "connect" in r["name"]]
        ws_drops = [r for r in window(normal, a, b) if r["type"] == "WS" and "dropped" in r["name"]]
        static = [r for r in allwin if r["cat"] == "static" and not r["abuse"] and not r["junk"]]
        st_abuse = [r for r in full_all if r["abuse"]]
        st_junk = [r for r in full_all if r["junk"] and r["cat"] != "task"]
        # the normal users' 5xx (abusers included, as before); the junk senders' are judged by their own SLO row
        five = [r for r in full_all if r["status"].startswith("5") and not r["junk"]]
        transport = [r for r in full_all if not r["ok"] and r["status"] in ("", "0") and r["type"] != "WS"
                     and "transport" in r["err"] and not r["junk"]]
        normal_transport = [r for r in transport if not r["abuse"]]
        # sockets of normal users the server turned away for overload: 1013 "try again later", a connect timeout
        normal_ws_shed = [r for r in full_all if r["type"] == "WS" and not r["ok"] and not r["junk"] and not r["abuse"]
                          and "connect" in r["name"] and WS_OVERLOAD.search(r["err"] or "")]
        log5_all = counter_delta(mon, "log_5xx", a, b)
        log5_junk = counter_delta(mon, "log_5xx_junk", a, b)
        timeouts = [x for x in log["timeouts_ts"] if x[0] is not None and a <= x[0] < b]
        bg = Counter(label for ts, label in log["background"] if ts is not None and a <= ts < b)
        redis_fb = sum(1 for ts in log["redis_down"] if ts is not None and a <= ts < b)
        cpu_warn = [w for ts, w in lwarn if a <= ts < b and "CPU" in w]
        new_visits = sum(1 for n in notes if a <= n["t"] < b and n["name"] == "visit" and n["note"] == "new")
        ai_step = [r for r in window(normal, a, b) if r["cat"] == "ai"]
        ai_bad = sum(1 for r in ai_step if r["note"].split(":")[0] in ("busy", "blocked"))
        normal_req = len([r for r in win if r["type"] != "WS"])
        cpu_ms = (statistics.fmean(cpu) / 100.0 * span * 1000.0 / normal_req) if cpu and normal_req else float("nan")
        pg_ms = (statistics.fmean(pg_cpu) / 100.0 * span * 1000.0 / normal_req) if pg_cpu and normal_req else float("nan")
        info = {
            "users": st["users"], "role": st["role"], "pre_spike": st.get("pre_spike", False),
            "start_s": a - t0, "end_s": b - t0, "steady_s": s - t0, "secs": round(b - a),
            "n": len(win), "rps": s_all["n"] / span, "err": s_all["rate"], "fail": s_all["fail"],
            "p50": s_all["p50"], "p95": s_all["p95"], "p99": s_all["p99"],
            "page_p50": s_pages["p50"], "page_p95": s_pages["p95"], "page_p99": s_pages["p99"], "page_n": s_pages["n"],
            "every": s_every, "every_rps": s_every["n"] / span,
            "cpu_mean": statistics.fmean(cpu) if cpu else float("nan"), "cpu_max": max(cpu) if cpu else float("nan"),
            "cpu_ms_per_req": cpu_ms, "pg_ms_per_req": pg_ms,
            "uss_end": uss[-1][1] if uss else float("nan"), "uss_slope": slope_per_min(uss),
            "lg_proc_max": max(lg_max) if lg_max else float("nan"), "lg_warn": len(cpu_warn),
            "other_cpu_max": max(other) if other else float("nan"), "cluster_max": max(cluster) if cluster else float("nan"),
            "sys_cpu_max": max(sysc) if sysc else float("nan"),
            "load_max": max(load1) if load1 else float("nan"),
            "pg_max": max(pgt) if pgt else float("nan"), "pg_active_max": max(pga) if pga else float("nan"),
            "time_wait_max": max(tw) if tw else float("nan"),
            "listen_overflow": counter_delta(mon, "tcp_listen_overflow", a, b),
            "ws_n": len(ws), "ws_per_s": len(ws) / span, "ws_p95": pct([r["ms"] for r in ws], 95),
            "ws_fail": sum(1 for r in ws if not r["ok"]), "ws_drops": len(ws_drops),
            "static_kbps": sum(r["bytes"] for r in static) / span / 1024.0,
            "static_304": sum(1 for r in static if r["status"] == "304"), "static_n": len(static),
            "abuse_rps": len(st_abuse) / max(1.0, b - a),
            "abuse_mix": Counter(r["status"] or "err" for r in st_abuse),
            "n5xx": len(five), "n503": sum(1 for r in five if r["status"] == "503"),
            "n500": sum(1 for r in five if r["status"] == "500"), "transport": len(transport),
            "log5xx": (log5_all - (log5_junk or 0)) if log5_all is not None else None,
            "log5xx_junk": log5_junk,
            "junk_n": len(st_junk), "junk_rps": len(st_junk) / max(1.0, b - a),
            "junk_5xx": sum(1 for r in st_junk if r["status"].startswith("5")),
            "junk_503": sum(1 for r in st_junk if r["status"] == "503"),
            "junk_transport": sum(1 for r in st_junk if not r["ok"] and r["status"] in ("", "0") and r["type"] != "WS"),
            "normal_503": sum(1 for r in full_all if r["status"] == "503" and not r["junk"] and not r["abuse"]),
            "normal_transport": len(normal_transport), "normal_ws_shed": len(normal_ws_shed),
            "timeouts": len(timeouts), "timeouts_stmt": sum(1 for x in timeouts if x[1] == "statement"),
            "timeouts_pool": sum(1 for x in timeouts if x[1] == "pool"),
            "background": dict(bg), "new_visits_per_s": new_visits / max(1.0, b - a),
            "ai_n": len(ai_step), "ai_bad": ai_bad, "redis_fallbacks": redis_fb,
        }
        # Other programs only skew a step when they COMPETE for CPU: more than one core of them while the whole machine
        # was nearly full. A steady background (an IDE, macOS indexing) on a machine with spare cores is reported in
        # the step table but does not void the measurement. (NaN-safe comparisons: x == x is False for NaN.)
        info["contaminated"] = (info["other_cpu_max"] == info["other_cpu_max"] and info["other_cpu_max"] > OTHER_CPU_LIMIT
                                and (info["sys_cpu_max"] != info["sys_cpu_max"] or info["sys_cpu_max"] > SYS_CPU_SATURATION)) or \
            (info["cluster_max"] == info["cluster_max"] and info["cluster_max"] > CLUSTER_CONN_LIMIT) or \
            (info["load_max"] == info["load_max"] and info["load_max"] > ncpu)
        info["generator_saturated"] = (info["lg_proc_max"] == info["lg_proc_max"] and info["lg_proc_max"] > LOADGEN_PROC_LIMIT) \
            or info["lg_warn"] > 0
        info["within"] = bool(s_all["n"]) and not (s_pages["p95"] == s_pages["p95"] and s_pages["p95"] > SLO["page_p95_ms"]) \
            and not (s_all["rate"] > SLO["error_rate"])
        step_rows.append(info)

    # ---- knee (rising steps only), capacity, SLO step, recovery
    rising = [s for s in step_rows if s["role"] in ("rising", "peak", "constant")]
    knee = next((s for s in rising if s["n"] and not s["within"]), None)
    capacity = None
    for s in rising:
        if s is knee:
            break
        if s["n"] and s["within"]:
            capacity = s
    slo_step = next((s for s in step_rows if s["users"] == slo_users and s["role"] != "recovery"), None)
    recovery = analyze_recovery(steps, step_rows, rows, normal, mon, slo_step, t0)

    # ---- per endpoint / class
    by_name: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_name[(r["type"], r["name"])].append(r)
    by_cls: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["cat"] != "task":
            by_cls[r["cls"] or "?"].append(r)

    # ---- abusers
    abusers = abuse_summary(by_name, step_rows, t0)
    abuse_rows = abusers["rows"]
    bursts = Counter(n["note"] for n in notes if n["cls"] == "Abuser")

    # ---- AI + checkout outcomes
    ai_rows = []
    ai_busy_blocked = 0
    for (rtype, name), rs in sorted(by_name.items()):
        if name.startswith("ABUSE") or category(name, rtype) != "ai":
            continue
        oc = Counter((r["note"] or ("failed" if not r["ok"] else "?")).split(":")[0] for r in rs)
        ai_busy_blocked += oc.get("busy", 0) + oc.get("blocked", 0)
        ai_rows.append([name, len(rs), oc.get("answer", 0), oc.get("cached", 0), oc.get("fallback", 0), oc.get("busy", 0),
                        oc.get("blocked", 0), sum(1 for r in rs if not r["ok"]), fmt_ms(pct([r["ms"] for r in rs], 50)),
                        fmt_ms(pct([r["ms"] for r in rs], 95))])
    checkout: Counter = Counter()
    for r in rows:
        if not r["name"].startswith("POST /shop/checkout") or r["status"] in ("301", "302", "303", "307", "308"):
            continue  # the POST's own 303 hop: its outcome is on the page it led to
        checkout[r["note"] or ("failed" if not r["ok"] else "refused" if r["status"] == "400" else "?")] += 1
    payments = by_name.get(("POST", "POST /shop/orders/[id]/test-gateway"), [])

    # ---- resources over the whole run
    uss_all = [(float(m["ts"]), v) for m in mon if (v := fnum(m, "srv_uss_mb")) is not None and v > 0]
    rss_all = [(float(m["ts"]), v) for m in mon if (v := fnum(m, "srv_rss_mb")) is not None]
    pg_tot = [v for v in (fnum(m, "pg_total") for m in mon) if v is not None]
    pg_idle_tx = [v for v in (fnum(m, "pg_idle_in_tx") for m in mon) if v is not None]
    pg_wait = [v for v in (fnum(m, "pg_waiting_lock") for m in mon) if v is not None]
    pg_long = [v for v in (fnum(m, "pg_longest_query_s") for m in mon) if v is not None]
    redis_delta = [v for v in (fnum(m, "redis_used_memory_delta_mb") for m in mon) if v is not None]
    redis_cl = [v for v in (fnum(m, "redis_clients_db9") for m in mon) if v is not None]
    lg_cpu = [v for v in (fnum(m, "loadgen_cpu_pct") for m in mon) if v is not None]
    lg_n = [v for v in (fnum(m, "loadgen_procs") for m in mon) if v is not None]
    srv_cpu = [v for v in (fnum(m, "srv_cpu_pct") for m in mon) if v is not None]
    worker_pids: dict[int, set[str]] = defaultdict(set)
    for m in mon:
        for i in range(1, 9):
            if m.get(f"w{i}_pid"):
                worker_pids[i].add(m[f"w{i}_pid"])
    pid_changes = sum(max(0, len(v) - 1) for v in worker_pids.values())
    all_pids = {p for v in worker_pids.values() for p in v}
    pool = int(cfg.get("DB_POOL_SIZE") or 10) + int(cfg.get("DB_MAX_OVERFLOW") or 5)
    budget = workers * pool + workers  # + one LISTEN connection per worker (REALTIME_PG_BRIDGE)
    leak = memory_verdict(steps, mon)

    # ---- verdicts
    verdicts: list[list[str]] = []

    def verdict(name: str, ok: bool | None, detail: str, users: int | None = None) -> None:
        verdicts.append([name, "PASS" if ok else ("n/a" if ok is None else "**FAIL**"), detail, users])

    inconclusive: list[str] = []
    if slo_step and slo_step["n"]:
        u = slo_step["users"]
        verdict(f"error rate < 1 % at {u} users (abusers + junk excluded{'; AI excluded: no provider' if no_ai else ''})",
                slo_step["err"] < SLO["error_rate"], fmt_pct(slo_step["err"]), u)
        verdict(f"page p95 < 1 s at {u} users", slo_step["page_p95"] < SLO["page_p95_ms"],
                f"{fmt_ms(slo_step['page_p95'])} ms", u)
        verdict(f"page p99 < 2.5 s at {u} users", slo_step["page_p99"] < SLO["page_p99_ms"],
                f"{fmt_ms(slo_step['page_p99'])} ms", u)
        srv5 = slo_step["log5xx"] or 0
        verdict(f"zero 5xx at {u} users (every request of the step but the junk senders')", slo_step["n5xx"] == 0
                and srv5 == 0, f"{slo_step['n5xx']} seen by clients (503: {slo_step['n503']}), {srv5} in server.log", u)
        verdict(f"zero DB statement / pool timeouts at {u} users", slo_step["timeouts"] == 0,
                f"statement {slo_step['timeouts_stmt']}, pool {slo_step['timeouts_pool']}", u)
        if slo_step["contaminated"]:
            inconclusive.append(f"the {u}-user step is CONTAMINATED (other programs {fmt_ms(slo_step['other_cpu_max'])} % "
                                f"CPU, load average up to {slo_step['load_max']:.1f} on {ncpu} CPUs, cluster connections "
                                f"{fmt_ms(slo_step['cluster_max'])})")
        if slo_step["generator_saturated"]:
            inconclusive.append(f"the load generator was saturated at {u} users (busiest process "
                                f"{fmt_ms(slo_step['lg_proc_max'])} % CPU)")
    else:
        for n in ("error rate < 1 %", "page p95 < 1 s", "page p99 < 2.5 s", "zero 5xx", "zero DB timeouts"):
            verdict(f"{n} at {slo_users} users", None, f"no step with exactly {slo_users} users in this run")
        inconclusive.append(f"no load step with exactly {slo_users} users (use --slo-users)")
    n500 = sum(1 for r in rows if r["status"] == "500")
    verdict("no unhandled errors (whole run: HTTP 500, Tracebacks, 'Exception in ASGI application')",
            n500 == 0 and log["traceback"] == 0 and log["asgi_exception"] == 0 and log["unhandled"] == 0,
            f"500s {n500}, Tracebacks {log['traceback']}, ASGI exceptions {log['asgi_exception']}, unhandled {log['unhandled']}")
    expected_started = workers
    restarts = len(log["worker_died"]) + max(0, log["server_started"] - expected_started) + pid_changes
    verdict("no worker restarts (uvicorn 'Child process died', extra 'Started server process', worker PID changes)",
            restarts == 0, f"died {len(log['worker_died'])}, started {log['server_started']} (expected {expected_started}), "
                           f"PID changes {pid_changes}")
    verdict("abusers throttled (every abuse pattern refused: 429 / 403 / 404, WebSocket flood closed 4429)",
            abusers["ok"] if abuse else None,
            f"{sum(1 for r in abuse if r['status'] in ('429', '403'))} of {len(abuse)} abuse requests refused with 429/403"
            + abusers["detail"])
    kit_crash = len(task_errors) + len(exceptions)
    verdict("kit: no task crashed (TASK events, locust_exceptions.csv)", kit_crash == 0,
            f"{len(task_errors)} task exceptions, {len(exceptions)} Locust exception rows")
    elsewhere = [r for r in rows if "signed in elsewhere" in r["err"]]
    verdict("kit: one account per simulated user (no 'signed in elsewhere' - one session per account)",
            not elsewhere, f"{len(elsewhere)} requests / sockets of a session ended by another sign-in"
            + (f" ({', '.join(sorted({r['cls'] for r in elsewhere}))})" if elsewhere else ""))
    junk = junk_summary(junk_rows, step_rows, t0, log, junk_fail, log_errors)
    if junk_rows:
        verdict("zero 5xx under random junk input", junk["slo_ok"], junk["slo_detail"])
        verdict("junk: other users' objects refused, no open redirect, junk rendered escaped", junk["sec_ok"],
                junk["sec_detail"])
    if no_ai:
        verdict("AI guard / fair queue under load", None, "not exercised: no AI provider (--ai off) - members got the "
                                                          "deterministic fallbacks; AI rows are kept out of the SLOs")
    elif slo_step and slo_step["ai_n"]:
        verdict(f"AI asks of normal members answered at {slo_step['users']} users (no 'busy' / guard refusal; "
                f"provider: {ai_mode})", slo_step["ai_bad"] == 0,
                f"{slo_step['ai_bad']} busy / blocked of {slo_step['ai_n']} AI asks in the step; whole run "
                f"{ai_busy_blocked} of {sum(r[1] for r in ai_rows)} (overload steps: see the per-step table)",
                slo_step["users"])
    else:
        verdict(f"AI asks of normal members answered (provider: {ai_mode})", None,
                f"no AI asks in the SLO step; whole run {ai_busy_blocked} busy / blocked of {sum(r[1] for r in ai_rows)}")
    verdict(leak["name"], leak["ok"], leak["detail"])
    if cfg.get("forced") or (pre_srv and not pre_srv.get("ok", True)) or ((meta.get("preflight") or {}).get("ok") is False):
        # The start-time preflight only sees the machine's load average, which also counts the test server's own
        # warm-up work. What decides whether the numbers are trustworthy is the SLO step itself: how much CPU OTHER
        # programs used while it ran (monitor.csv 'other_cpu_pct', checked above as "contaminated"). So a forced start
        # makes the run inconclusive only when that step was contaminated or is missing.
        why = "; ".join(((pre_srv or {}).get("reasons") or (meta.get("preflight") or {}).get("reasons") or ["--force"])[:2])
        if slo_step and slo_step.get("n") and not slo_step.get("contaminated"):
            verdict(f"machine isolation (start forced - preflight said: {why})", True,
                    f"the {slo_step['users']}-user step itself was clean: other programs used at most "
                    f"{fmt_ms(slo_step['other_cpu_max'])} % CPU"
                    + (f" while the whole machine stayed at or below {fmt_ms(slo_step['sys_cpu_max'])} % busy (a step "
                       f"counts as skewed only when other programs pass {OTHER_CPU_LIMIT:.0f} % = one core AND the "
                       f"machine passes {SYS_CPU_SATURATION:.0f} %)" if slo_step["sys_cpu_max"] == slo_step["sys_cpu_max"]
                       else f" (limit {OTHER_CPU_LIMIT:.0f} % = one core)"))
        else:
            inconclusive.append("started on a busy machine (preflight overridden: " + why + ")")
    if not mon:
        inconclusive.append("no monitor.csv - server resources and the machine's load were not recorded")
    elif not any(m.get("load1") not in (None, "") for m in mon):
        inconclusive.append("monitor.csv has no machine-load columns (older kit) - isolation from other programs not verified")
    failed = any(v[1] == "**FAIL**" for v in verdicts)
    overall = "FAIL" if failed else ("INCONCLUSIVE" if inconclusive else "PASS")

    # ---- saturation estimate (calibration)
    cal = [s for s in step_rows if s["cpu_ms_per_req"] == s["cpu_ms_per_req"] and s["n"] > 200]
    est = None
    if cal:
        best = max(cal, key=lambda s: s["rps"])
        per_user = best["rps"] / max(1, best["users"])
        sat_rps = workers * 1000.0 / best["cpu_ms_per_req"] if best["cpu_ms_per_req"] > 0 else float("nan")
        est = {"step": best["users"], "cpu_ms": best["cpu_ms_per_req"], "pg_ms": best["pg_ms_per_req"],
               "rps_per_user": per_user, "sat_rps": sat_rps, "sat_users": sat_rps / per_user if per_user else float("nan")}
        # the 10-minute run thinks 2-8 s (mean 5 s): a user's request rate scales with 1 / (think time + task time)
        wait = meta.get("wait") or [2.0, 8.0]
        mean_wait = (float(wait[0]) + float(wait[1])) / 2.0
        if abs(mean_wait - 5.0) > 0.25 and per_user:
            real_per_user = per_user * (mean_wait + 0.3) / (5.0 + 0.3)
            est["stress_rps_per_user"] = real_per_user
            est["stress_sat_users"] = sat_rps / real_per_user

    # ------------------------------------------------------------------------------------------------ report
    L: list[str] = []
    when = datetime.fromtimestamp(t0, UTC).strftime("%Y-%m-%d %H:%M:%S UTC") if t0 else "?"
    when_ist = datetime.fromtimestamp(t0, IST).strftime("%H:%M IST") if t0 else "?"
    L.append(f"# IronPulse load test - {rd.name}\n")
    L.append(f"**Overall: {overall}** · profile `{meta.get('profile', '?')}` · started {when} ({when_ist}) · "
             f"{duration / 60:.1f} min · peak {max((s['users'] for s in step_rows), default=0)} users · "
             f"{len(rows):,} requests ({len(rows) / duration:.1f}/s, {len(abuse):,} of them abuse, {len(junk_rows):,} random junk) · "
             f"AI `{ai_mode}` · "
             f"{workers} workers · DB pool {cfg.get('DB_POOL_SIZE')}+{cfg.get('DB_MAX_OVERFLOW')} per worker · "
             f"WebSocket `{meta.get('ws_mode')}`\n")
    if inconclusive:
        L.append("**Why not PASS/FAIL:** " + "; ".join(inconclusive) + "\n")
    if dropped:
        L.append(f"_{dropped:,} request rows from before this run's start were ignored (reused run folder)._\n")
    L.append("## Production SLOs\n")
    L.append("Judged inside the SLO step only (its steady part); 5xx / 503 / timeouts of the overload steps are "
             "evidence of the knee (table below), not failures. Whole-run checks: unhandled errors, worker restarts, "
             "kit crashes, abuse throttling.\n")
    L.append(table(["check", "result", "measured"], [v[:3] for v in verdicts], "lll"))

    L.append("\n## Load steps\n")
    L.append("Steady part of each step = after its ramp (users / spawn rate) + 5 s. Normal users only (abusers "
             "separate); RPS / errors / latency exclude AI when no AI provider ran.\n")
    hdr = ["users", "role", "s", "requests", "RPS", "errors", "p50", "p95", "p99", "page p95", "page p99",
           "within SLO", "server CPU % mean/max", "CPU ms/req (srv/PG)", "PG conns max (active)", "flags"]
    trs = []
    for s in step_rows:
        flags = []
        if s["contaminated"]:
            flags.append("CONTAMINATED")
        if s["generator_saturated"]:
            flags.append("GENERATOR SATURATED")
        if s["listen_overflow"]:
            flags.append(f"listen-queue overflow +{s['listen_overflow']}")
        if s["pre_spike"]:
            flags.append("pre-spike")
        if s is slo_step:
            flags.append("SLO step")
        trs.append([s["users"], s["role"], s["secs"], s["n"], f"{s['rps']:.1f}", fmt_pct(s["err"]), fmt_ms(s["p50"]),
                    fmt_ms(s["p95"]), fmt_ms(s["p99"]), fmt_ms(s["page_p95"]), fmt_ms(s["page_p99"]),
                    "yes" if s["within"] else "**no**", f"{fmt_ms(s['cpu_mean'])} / {fmt_ms(s['cpu_max'])}",
                    f"{fmt_ms(s['cpu_ms_per_req'])} / {fmt_ms(s['pg_ms_per_req'])}",
                    f"{fmt_ms(s['pg_max'])} ({fmt_ms(s['pg_active_max'])})", ", ".join(flags) or "-"])
    L.append(table(hdr, trs, "lllrrrrrrrrlrrrl"))
    L.append("")
    if knee:
        L.append(f"**Knee:** the first rising step over the limits (page p95 > 1 s or errors > 1 %) is **{knee['users']} "
                 f"users** (page p95 {fmt_ms(knee['page_p95'])} ms, errors {fmt_pct(knee['err'])}, {knee['rps']:.1f} RPS)."
                 + (f" The last step within the limits: **{capacity['users']} users at {capacity['rps']:.1f} RPS**." if capacity else "")
                 + "\n")
    else:
        L.append("**Knee:** not reached - every rising step stayed within page p95 <= 1 s and errors <= 1 %"
                 + (f" (the largest: {capacity['users']} users at {capacity['rps']:.1f} RPS)." if capacity else ".") + "\n")
    if est:
        L.append(f"**Capacity estimate** (from the {est['step']}-user step): {fmt_ms(est['cpu_ms'])} ms of worker CPU and "
                 f"{fmt_ms(est['pg_ms'])} ms of PostgreSQL CPU per normal request, {est['rps_per_user']:.2f} requests/s "
                 f"per user -> {workers} workers saturate near **{fmt_ms(est['sat_rps'])} RPS ≈ {fmt_ms(est['sat_users'])} "
                 "users** (CPU-bound estimate; I/O waits and the DB pool can bring the knee earlier)."
                 + (f" At the 10-minute run's 2-8 s think time (~{est['stress_rps_per_user']:.2f} requests/s per user) that is "
                    f"**≈ {fmt_ms(est['stress_sat_users'])} users**." if est.get("stress_sat_users") else "") + "\n")
    L.append("Server CPU % is summed over the uvicorn master + workers (100 % = one core; 4 workers can use 400 %).\n")

    L.append("### Overload evidence and traffic mix per step\n")
    L.append(table(["users", "5xx (503 / 500) / server.log", "DB timeouts stmt/pool", "AI asks (busy+blocked)",
                    "transport errors", "junk req/s (5xx, 503)", "WS connects/s",
                    "WS connect p95 ms", "WS fails / drops", "static KB/s (304s)", "new visitors/s", "abuse req/s",
                    "abuse status mix", "Redis fallbacks of the shared limits", "background jobs (log)",
                    "load-gen busiest proc %", "other CPU % max",
                    "load avg max", "cluster conns max", "TIME_WAIT max"],
                   [[s["users"], f"{s['n5xx']} ({s['n503']} / {s['n500']}) / {s['log5xx'] if s['log5xx'] is not None else '-'}",
                     f"{s['timeouts_stmt']} / {s['timeouts_pool']}", f"{s['ai_n']} ({s['ai_bad']})",
                     s["transport"], f"{s['junk_rps']:.1f} ({s['junk_5xx']}, {s['junk_503']})", f"{s['ws_per_s']:.1f}",
                     fmt_ms(s["ws_p95"]), f"{s['ws_fail']} / {s['ws_drops']}",
                     f"{s['static_kbps']:.0f} ({s['static_304']})", f"{s['new_visits_per_s']:.2f}", f"{s['abuse_rps']:.1f}",
                     ", ".join(f"{k}:{v}" for k, v in sorted(s["abuse_mix"].items())) or "-", s["redis_fallbacks"],
                     ", ".join(f"{k} x{v}" for k, v in sorted(s["background"].items())) or "-",
                     fmt_ms(s["lg_proc_max"]), fmt_ms(s["other_cpu_max"]),
                     f"{s['load_max']:.1f}" if s["load_max"] == s["load_max"] else "-", fmt_ms(s["cluster_max"]),
                     fmt_ms(s["time_wait_max"])] for s in step_rows]))
    L.append("")
    if recovery:
        L.append("### Recovery after the peak\n")
        L.append(table(["what", "value"], [[k, v] for k, v in recovery["rows"]], "ll"))
        L.append("")

    L.append("## Per user class (whole run)\n")
    cls_rows = []
    for cls, rs in sorted(by_cls.items(), key=lambda kv: -len(kv[1])):
        s = summarize(rs)
        cls_rows.append([cls, s["n"], s["fail"], fmt_pct(s["rate"]), fmt_ms(s["p50"]), fmt_ms(s["p95"]), fmt_ms(s["p99"])])
    L.append(table(["class", "requests", "failures", "error rate", "p50 ms", "p95 ms", "p99 ms"], cls_rows))

    L.append("\n## AI asks of members\n")
    if ai_rows:
        L.append(f"Provider mode `{ai_mode}`. answer = the model's answer, cached = from the answer cache, fallback = the "
                 "app's deterministic answer (no provider), busy = the AI queue was full (a failure for a member), blocked "
                 "= the AI guard refused a normal member (a failure).\n")
        L.append(table(["endpoint", "asks", "answer", "cached", "fallback", "busy", "blocked", "failures", "p50 ms",
                        "p95 ms"], ai_rows, "lrrrrrrrrr"))
    else:
        L.append("_no AI asks in this run_")
    L.append(f"\nCheckout: `POST /shop/checkout` outcomes {dict(checkout) or '-'}; test-mode payments approved: "
             f"{sum(1 for r in payments if r['ok'])} of {len(payments)}.\n")

    L.append("## Per endpoint (whole run, normal users; abusers and junk senders below)\n")
    ep_rows = []
    for (rtype, name), rs in sorted(by_name.items(), key=lambda kv: (-pct([r["ms"] for r in kv[1]], 95))):
        if name.startswith(("ABUSE", "JUNK")) or rtype == "TASK":
            continue
        s = summarize(rs)
        label = name if re.match(r"^(GET|POST|PUT|PATCH|DELETE|WS) ", name) else f"{rtype} {name}"
        ep_rows.append([label, category(name, rtype), s["n"], s["fail"],
                        fmt_ms(s["p50"]), fmt_ms(s["p95"]), fmt_ms(s["p99"]), fmt_ms(s["max"]), f"{s['n'] / duration:.2f}"])
    L.append(table(["endpoint", "kind", "requests", "failures", "p50 ms", "p95 ms", "p99 ms", "max ms", "RPS"], ep_rows,
                   "llrrrrrrr"))

    L.append("\n## Abusers (expected: refused with 429 / 403, 404 for the scan; 5xx = failure)\n")
    if abuse_rows:
        L.append(table(["pattern", "requests", "2xx", "400", "401", "403", "404", "429", "5xx", "failures",
                        "overload (not counted)", "429/403", "first 429/403 at #", "p95 ms", "verdict"], abuse_rows,
                       "lrrrrrrrrrrrrrl"))
        L.append("\nverdict: `refused` = refused as designed (429 / 403, 404 for the scan; the WebSocket flood closed with "
                 "4429 or refused at the handshake) and no failure; `setup` = the abuser's own sign-in and the sign-in "
                 "page it loads first, shown but never judged as abuse. `overload (not counted)`: a 503, a transport / "
                 "connect timeout or a socket turned away with 1013 in a step where the normal users were shed too - "
                 "the server's overload, not the abuse (as for the junk senders); `failures` counts the rest.")
        if abusers["shed"]:
            L.append("\nNot counted (overload, not the abuse):\n")
            for line in abusers["shed_lines"]:
                L.append(f"- {line}")
        if abusers["problems"]:
            L.append("\n**Not throttled / failed:**\n")
            for line in abusers["problems"]:
                L.append(f"- {line}")
        L.append(f"\nBursts by address: {dict(bursts)} (fresh-ip = a new botnet address for that burst). Locust's own HTML "
                 "report aggregates abuse traffic into its 'Aggregated' row - use the tables here for normal users.")
    else:
        L.append("_no abuser traffic in this run_")
    L.extend(junk_report(junk, notes, log))

    L.append("\n## Server resources (monitor.csv, every 5 s)\n")
    if mon:
        uss_first = uss_all[0][1] if uss_all else float("nan")
        uss_last = uss_all[-1][1] if uss_all else float("nan")
        L.append(table(["metric", "value"], [
            ["server CPU % max (sum of processes)", fmt_ms(max(srv_cpu)) if srv_cpu else "-"],
            ["workers' USS MB first / max / last", f"{fmt_ms(uss_first)} / {fmt_ms(max(v for _, v in uss_all)) if uss_all else '-'} / {fmt_ms(uss_last)}"],
            ["server RSS MB first / max / last (macOS RSS moves with compression)",
             f"{fmt_ms(rss_all[0][1])} / {fmt_ms(max(v for _, v in rss_all))} / {fmt_ms(rss_all[-1][1])}" if rss_all else "-"],
            ["memory verdict", f"{leak['ok'] if leak['ok'] is not None else 'n/a'}: {leak['detail']}"],
            ["PostgreSQL connections to ironpulse_loadtest max", f"{fmt_ms(max(pg_tot)) if pg_tot else '-'} (budget {budget}: {workers} x (pool {pool}) + {workers} LISTEN)"],
            ["idle in transaction max", fmt_ms(max(pg_idle_tx)) if pg_idle_tx else "-"],
            ["waiting on locks max", fmt_ms(max(pg_wait)) if pg_wait else "-"],
            ["longest running query (s)", f"{max(pg_long):.2f}" if pg_long else "-"],
            ["queries active > 1 s / idle in transaction > 1 s (pg_slow.csv samples)",
             f"{sum(1 for r in slow if r.get('wait_event_type') != 'idle in transaction')} / "
             f"{sum(1 for r in slow if r.get('wait_event_type') == 'idle in transaction')}"],
            ["Redis instance memory change MB max (instance shared with db 0)", f"{max(redis_delta):.2f}" if redis_delta else "-"],
            ["Redis clients on db 9 max", fmt_ms(max(redis_cl)) if redis_cl else "-"],
            ["load generator processes / CPU % max (sum)", f"{fmt_ms(max(lg_n)) if lg_n else '-'} / {fmt_ms(max(lg_cpu)) if lg_cpu else '-'}"],
            ["worker PIDs seen", f"{len(all_pids)} ({workers} expected)"],
        ], "lr"))
    else:
        L.append("_no monitor.csv_")
    if slow:
        groups: dict[tuple[str, str], float] = {}
        for r in slow:
            kind = "idle in transaction after" if r.get("wait_event_type") == "idle in transaction" else "active"
            q = re.sub(r"\s+", " ", r.get("query", ""))[:160]
            try:
                groups[(kind, q)] = max(groups.get((kind, q), 0.0), float(r.get("active_s") or 0))
            except ValueError:
                pass
        L.append("\nQueries seen active > 1 s, and connections seen idle in a transaction > 1 s with their last statement "
                 "(max seconds; pg_slow.csv):\n")
        L.extend(f"- {v:.1f} s {kind} `{q}`" for (kind, q), v in sorted(groups.items(), key=lambda kv: -kv[1])[:12])

    L.append("\n## server.log\n")
    L.append(table(["what", "count"], [
        ["lines / access lines", f"{log['lines']:,} / {log['access']:,}"],
        ["status mix", ", ".join(f"{k}: {v:,}" for k, v in sorted(log["status"].items()))],
        ["5xx (access log)", len(log["5xx"])], ["Tracebacks", log["traceback"]],
        ["unhandled errors / 'Exception in ASGI application'", f"{log['unhandled']} / {log['asgi_exception']}"],
        ["worker deaths / 'Started server process'", f"{len(log['worker_died'])} / {log['server_started']}"],
        ["DB statement / pool timeouts (timestamped WARNINGs)",
         f"{sum(1 for x in log['timeouts_ts'] if x[1] == 'statement')} / {sum(1 for x in log['timeouts_ts'] if x[1] == 'pool')}"],
        ["app log levels", dict(log["levels"])], ["uvicorn log levels", dict(log["uvicorn"])],
        ["IP bans issued", log["bans"]],
    ], "lr"))
    if log["5xx"]:
        L.append("\n5xx responses (first 20):\n")
        L.extend(f"- `{x}`" for x in log["5xx"][:20])
    if log["worker_died"]:
        L.append("\nWorker deaths:\n")
        L.extend(f"- `{x}`" for x in log["worker_died"][:10])
    if log["errors"]:
        L.append("\nERROR lines (first 15):\n")
        L.extend(f"- `{x}`" for x in log["errors"])
    if log["warnings"]:
        L.append("\nWARNING lines by kind (top 12):\n")
        L.extend(f"- {n} x `{k}`" for k, n in log["warnings"].most_common(12))

    fails = Counter(f"{r['name']}: {r['err'] or r['status']}" for r in rows if not r["ok"] and not r["abuse"] and not r["junk"])
    L.append("\n## Failures of normal users (top 25)\n")
    L.append("\n".join(f"- {n} x `{k[:200]}`" for k, n in fails.most_common(25)) if fails else "_none_")
    if task_errors or exceptions:
        L.append("\n## Kit errors\n")
        L.extend(f"- `{r['name']}: {r['err']}`" for r in task_errors[:20])
        L.extend(f"- {e.get('Count', '?')} x `{(e.get('Message') or '')[:160]}`" for e in exceptions[:10])
    if lwarn:
        L.append("\nLocust warnings: " + "; ".join(sorted({w for _, w in lwarn}))[:600])

    L.append("\n## Run setup\n")
    plat = cfg.get("platform") or {}
    eff = cfg.get("effective_settings") or {}
    pinned = cfg.get("pinned") or {}
    L.append(f"- started {when} = {datetime.fromtimestamp(t0, IST).strftime('%Y-%m-%d %H:%M:%S IST') if t0 else '?'}; "
             f"server healthy {fmt_ms((t0 - float(ready['healthy_at'])) if ready.get('healthy_at') else float('nan'))} s "
             "before Locust started (background jobs have initial delays of 45-300 s: start Locust >= 330 s after the "
             "server for clean steps)")
    L.append(f"- shape `{meta.get('steps')}` (end s, users, spawn rate) · weights {meta.get('weights')} · fixed "
             f"{meta.get('fixed')} · think time {meta.get('wait')} s · WebSocket `{meta.get('ws_mode')}` · Locust "
             f"{meta.get('locust_version')} with --processes {meta.get('processes')}")
    L.append(f"- server: {workers} uvicorn workers, ENVIRONMENT={cfg.get('ENVIRONMENT')}, DB pool "
             f"{cfg.get('DB_POOL_SIZE')}+{cfg.get('DB_MAX_OVERFLOW')} per worker (budget {cfg.get('connection_budget')}), "
             f"AI `{ai_mode}`, mail `{cfg.get('smtp_mode')}`, backlog {cfg.get('backlog')} (macOS caps it at "
             f"somaxconn={plat.get('somaxconn')}; production Linux default 4096 - near the knee accept-queue overflows "
             "add 1-3 s SYN retries, counted as 'listen-queue overflow' per step)")
    L.append(f"- pinned settings ({cfg.get('pinned_source')}): " + ", ".join(f"{k}={v}" for k, v in sorted(pinned.items())))
    if cfg.get("lt_overrides"):
        L.append(f"- LT_ overrides: {cfg.get('lt_overrides')}")
    L.append(f"- effective (app loader): rate limits {eff.get('rate_limit_per_minute')}/{eff.get('rate_limit_auth_per_minute')}"
             f"/{eff.get('rate_limit_ai_per_minute')} per min (all/auth/AI), AI queue {eff.get('ai_queue_concurrency')} "
             f"running, max {eff.get('ai_queue_max_total')}, provider order {eff.get('ai_provider_order')}, statement timeout "
             f"{eff.get('db_statement_timeout_ms')} ms, pool timeout {eff.get('db_pool_timeout_s')} s, cookies secure "
             f"{eff.get('cookie_secure')}, force https {eff.get('force_https')}, proxy trust {eff.get('trust_proxy_headers')}")
    if cfg.get("startup_warnings"):
        L.append(f"- app startup warnings: {[w[:140] for w in cfg['startup_warnings']]}")
    L.append(f"- stand-ins (cheaper than production): {cfg.get('stand_ins')}")
    L.append(f"- platform: {plat} · PostgreSQL: {cfg.get('pg_settings')}")
    pre = meta.get("preflight") or {}
    L.append(f"- preflight at the Locust start: {'quiet' if pre.get('ok') else 'BUSY: ' + '; '.join(pre.get('reasons') or [])} "
             f"(load {pre.get('load')}, other DB connections {pre.get('pg_connections_other')}, forced {pre.get('forced')})")
    if seed:
        L.append(f"- data: seeded in {seed.get('total_seconds')} s at {seed.get('finished_at')}; rows: "
                 + ", ".join(f"{k} {v:,}" for k, v in sorted((seed.get('table_rows') or {}).items())
                             if not k.startswith("_") and isinstance(v, int) and v >= 1000))
    L.append("- files: `locust_report.html` (Locust's own report; its Aggregated row includes abuse traffic), "
             "`locust_stats*.csv`, `requests_*.csv`, `monitor.csv`, `pg_slow.csv`, `server.log`, `summary.json`")
    report = "\n".join(L) + "\n"

    summary_internal = {"overall": overall, "verdicts": verdicts, "inconclusive": inconclusive,
                        "knee": knee["users"] if knee else None, "capacity": capacity["users"] if capacity else None,
                        "steps": [{k: (v if not isinstance(v, (Counter, dict)) else dict(v)) for k, v in s.items() if k != "every"}
                                  for s in step_rows],
                        "requests": len(rows), "normal_failures": sum(1 for r in normal if not r["ok"]),
                        "abuse_requests": len(abuse), "estimate": est, "recovery": recovery,
                        "abusers": {k: v for k, v in abusers.items() if k != "rows"},
                        "junk": {k: v for k, v in junk.items() if k not in ("security", "elsewhere", "idor", "five_counted",
                                                                            "findings")}}
    page = page_summary(rd, meta, cfg, seed, rows, normal, abuse, step_rows, slo_step, knee, capacity, verdicts, overall,
                        inconclusive, history, mon, by_name, by_cls, log, t0, t_end, duration, workers, pool, leak,
                        ai_mode, est, junk, abusers)
    return report, summary_internal, page


# ------------------------------------------------------------------------------------------------ abusers
def abuse_summary(by_name: dict[tuple[str, str], list[dict[str, Any]]], step_rows: list[dict[str, Any]],
                  t0: float) -> dict[str, Any]:
    """The verdict "abusers throttled" and the abusers table. Every attack pattern ("ABUSE ..." stats names but the
    setup steps, ``abuse_setup``) must be refused as designed - at least one of its ``ABUSE_EXPECT_REFUSAL`` answers;
    the WebSocket flood closed with 4429 or refused at the handshake (its ok rows) - and must have no failure, except
    an overload artefact (``overload_artefact``: a 503, a transport / connect timeout, a socket turned away with 1013)
    in a step whose normal users were shed too (``shed_step``): the server's overload, not the abuse - reported
    separately, not counted, the rule of the junk verdict. ``ok`` is None when no attack pattern ran at all."""
    rows: list[list[Any]] = []
    problems: list[str] = []
    shed: list[dict[str, Any]] = []
    setup_fails: list[dict[str, Any]] = []
    judged = 0
    ws_total = ws_closed = 0
    for (rtype, name), rs in sorted(by_name.items()):
        if not name.startswith("ABUSE"):
            continue
        st = Counter(r["status"] or ("ok" if r["ok"] else "err") for r in rs)
        thr = st.get("429", 0) + st.get("403", 0)
        first_thr = next((i for i, r in enumerate(rs) if r["status"] in ("429", "403")), None)
        five = sum(v for k, v in st.items() if k.startswith("5"))
        counted: list[dict[str, Any]] = []
        excused = 0
        for r in rs:
            if r["ok"]:
                continue
            kind = overload_artefact(r)
            step = step_at(step_rows, t0, r["t"]) if kind else None
            if kind and shed_step(step):
                excused += 1
                shed.append({"pattern": name, "kind": kind, "t": r["t"], "what": r["status"] or r["err"] or "error",
                             "users": step["users"] if step else None, "role": step["role"] if step else None,
                             "normal_503": step["normal_503"] if step else 0,
                             "normal_transport": step["normal_transport"] if step else 0,
                             "normal_ws_shed": step["normal_ws_shed"] if step else 0})
            else:
                counted.append(r)
        if abuse_setup(name):
            setup_fails += counted
            verdict = "setup"
        else:
            judged += 1
            issues = []
            if rtype == "WS":
                ws_total += len(rs)
                ws_closed += sum(1 for r in rs if r["ok"])
                if not any(r["ok"] for r in rs):
                    issues.append("never closed with 4429 (nor refused at the handshake)")
            else:
                codes = next((c for key, c in ABUSE_EXPECT_REFUSAL.items() if key in name), None)
                if codes and not any(st.get(c, 0) for c in codes):
                    issues.append(f"never refused (no {' / '.join(codes)} at all)")
            if counted:
                mix = Counter(r["status"] or (r["err"] or "error")[:60] for r in counted)
                issues.append(f"{len(counted)} failed outside the load-shedding steps ("
                              + ", ".join(f"{k} x{v}" for k, v in mix.most_common(4)) + ")")
            verdict = "refused" if not issues else ("**not throttled**" if any(i.startswith("never") for i in issues)
                                                    else "**failed**")
            problems += [f"`{name}`: {i}" for i in issues]
        rows.append([name, len(rs), st.get("200", 0) + st.get("201", 0), st.get("400", 0), st.get("401", 0),
                     st.get("403", 0), st.get("404", 0), st.get("429", 0), five, len(counted), excused,
                     fmt_pct(thr / len(rs)) if rs else "-",
                     first_thr + 1 if first_thr is not None else "never", fmt_ms(pct([r["ms"] for r in rs], 95)),
                     verdict])
    groups = Counter((s["pattern"], s["what"], s["users"], s["role"], s["normal_503"], s["normal_transport"],
                      s["normal_ws_shed"]) for s in shed)
    shed_lines = [f"{n} x {what} in `{pattern}` - the {users}-user step ({role}), where the normal users got "
                  f"{n503} 503s, {ntr} transport errors and {nws} sockets turned away"
                  for (pattern, what, users, role, n503, ntr, nws), n in sorted(groups.items(), key=lambda kv: -kv[1])]
    ok = (not problems) if judged else None
    kinds = Counter(s["kind"] for s in shed)
    detail = ""
    if ws_total:
        detail += f"; WebSocket floods closed 4429 or refused: {ws_closed} of {ws_total}"
    if shed:
        detail += (f"; {len(shed)} overload failures in load-shedding steps not counted ("
                   + ", ".join(f"{v} x {k}" for k, v in kinds.most_common()) + ")")
    if problems:
        detail += "; NOT THROTTLED / FAILED: " + "; ".join(problems)[:600]
    if setup_fails:
        detail += f"; {len(setup_fails)} failures of setup rows (own sign-in), not judged"
    return {"rows": rows, "ok": ok, "problems": problems, "shed": shed, "shed_lines": shed_lines,
            "setup_failures": len(setup_fails), "judged_patterns": judged, "detail": detail}


# ------------------------------------------------------------------------------------------------ random junk input
SECURITY_ERR = ("IDOR", "open redirect")


def junk_summary(junk_rows: list[dict[str, Any]], step_rows: list[dict[str, Any]], t0: float, log: dict[str, Any],
                 junk_fail: list[dict[str, Any]], log_errors: list[dict[str, Any]]) -> dict[str, Any]:
    """The JunkUser class on its own: status mix, 4xx breakdown, 5xx per endpoint with the input and the server's
    traceback, IDOR probes, and the verdict 'zero 5xx under random junk input'. A 503 / timeout of a junk request is
    NOT counted when the normal users of that load step were shed too (503 or transport errors: overload, not the
    junk); every other 5xx (500 above all) and every socket error (1011 / dropped) is."""

    def overloaded(t: float) -> bool:
        for s in step_rows:
            if s["start_s"] + t0 <= t < s["end_s"] + t0:
                return bool(s["normal_503"] or s["transport"])
        return False

    http = [r for r in junk_rows if r["type"] != "WS"]
    ws = [r for r in junk_rows if r["type"] == "WS"]
    status = Counter(r["status"] or "err" for r in http)
    five = [r for r in http if r["status"].startswith("5")]
    shed = [r for r in five if r["status"] == "503" and overloaded(r["t"])]
    five_counted = [r for r in five if not (r["status"] == "503" and overloaded(r["t"]))]
    transport = [r for r in http if not r["ok"] and r["status"] in ("", "0")]
    transport_counted = [r for r in transport if not overloaded(r["t"])]
    ws_bad = [r for r in ws if not r["ok"]]
    sec = [r for r in junk_rows if r["err"].startswith(SECURITY_ERR) or "XSS" in r["err"]]
    elsewhere = [r for r in junk_rows if "signed in elsewhere" in r["err"]]
    slo_ok = not five_counted and not transport_counted and not ws_bad
    excluded = len(shed) + (len(transport) - len(transport_counted))
    slo_detail = (f"{len(five_counted)} 5xx, {len(transport_counted)} timeouts/connection errors, {len(ws_bad)} socket "
                  f"errors of {len(junk_rows):,} junk requests"
                  + (f" ({excluded} in load-shedding steps not counted)" if excluded else "")
                  + f"; server.log: {log.get('5xx_junk', 0)} 5xx to junk addresses")
    idor = [r for r in junk_rows if r["name"].endswith("(IDOR)")]
    sec_detail = (f"{sum(1 for r in sec if r['err'].startswith('IDOR'))} IDOR, "
                  f"{sum(1 for r in sec if r['err'].startswith('open redirect'))} open redirects, "
                  f"{sum(1 for r in sec if 'XSS' in r['err'])} unescaped of {len(idor):,} IDOR probes + "
                  f"{len(junk_rows):,} junk requests")
    # per endpoint
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in junk_rows:
        by_name[r["name"]].append(r)
    endpoints = []
    for name, rs in sorted(by_name.items(), key=lambda kv: (-sum(1 for r in kv[1] if r["status"].startswith("5")),
                                                            -len(kv[1]))):
        sc = Counter(r["status"] or "err" for r in rs)
        endpoints.append({"name": name, "n": len(rs), "2xx": sum(v for k, v in sc.items() if k.startswith("2")),
                          "3xx": sum(v for k, v in sc.items() if k.startswith("3")),
                          "4xx": sum(v for k, v in sc.items() if k.startswith("4")),
                          "5xx": sum(v for k, v in sc.items() if k.startswith("5")),
                          "transport": sum(1 for r in rs if not r["ok"] and r["status"] in ("", "0")),
                          "fail": sum(1 for r in rs if not r["ok"]), "p95": pct([r["ms"] for r in rs], 95),
                          "top4xx": ", ".join(f"{k}:{v}" for k, v in sc.most_common() if k.startswith("4"))[:80]})
    # the failing inputs, each with the server's traceback (same path, within 10 s)
    findings = []
    for rec in junk_fail:
        st = int(rec.get("status") or 0) if str(rec.get("status") or "").isdigit() else 0
        path = urlsplit(str(rec.get("url") or "")).path
        tb = None
        if st >= 500 or rec.get("method") == "WS":
            cands = [b for b in log_errors if b["ts"] is not None and abs(b["ts"] - float(rec.get("t") or 0)) <= 10
                     and (not b["path"] or b["path"] == path or rec.get("method") == "WS")]
            tb = min(cands, key=lambda b: abs(b["ts"] - float(rec.get("t") or 0))) if cands else None
        findings.append({**rec, "traceback": tb})
    return {"requests": len(junk_rows), "http": len(http), "ws": len(ws), "status": status,
            "s2xx": sum(v for k, v in status.items() if k.startswith("2")),
            "s3xx": sum(v for k, v in status.items() if k.startswith("3")),
            "s4xx": sum(v for k, v in status.items() if k.startswith("4")),
            "s5xx": len(five), "s5xx_counted": len(five_counted), "shed": len(shed), "transport": len(transport),
            "transport_counted": len(transport_counted), "ws_bad": len(ws_bad), "security": sec, "elsewhere": elsewhere,
            "idor": idor, "endpoints": endpoints, "findings": findings, "slo_ok": slo_ok, "slo_detail": slo_detail,
            "sec_ok": not sec, "sec_detail": sec_detail, "five_counted": five_counted,
            "fails": sum(1 for r in junk_rows if not r["ok"])}


def junk_report(junk: dict[str, Any], notes: list[dict[str, Any]], log: dict[str, Any]) -> list[str]:
    L: list[str] = ["\n## Random junk input (JunkUser - kept out of the normal users' SLOs)\n"]
    if not junk["requests"]:
        L.append("_no junk traffic in this run_")
        return L
    modes = Counter(n["note"] for n in notes if n["cls"] == "JunkUser" and n["name"] == "mode")
    fresh = sum(1 for n in notes if n["cls"] == "JunkUser" and n["name"] == "fresh address after a ban")
    unsendable = sum(1 for n in notes if n["cls"] == "JunkUser" and n["name"] == "unsendable junk request")
    L.append(f"Junk senders: {dict(modes) or '-'}; {fresh} came back from a fresh address after a ban; "
             f"{unsendable} junk requests the client could not send (not counted). Expected answers: 2xx / 3xx / 4xx "
             "(429 / 403 throttling and bans included). FAILURE: any 5xx, timeout or connection error, a 2xx on another "
             "user's object (IDOR), a redirect to another site, junk markup reflected unescaped.\n")
    four = Counter({k: v for k, v in junk["status"].items() if k.startswith("4")})
    L.append(table(["what", "value"], [
        ["junk requests (HTTP / WebSocket)", f"{junk['requests']:,} ({junk['http']:,} / {junk['ws']:,})"],
        ["2xx / 3xx / 4xx / 5xx / transport errors",
         f"{junk['s2xx']:,} / {junk['s3xx']:,} / {junk['s4xx']:,} / {junk['s5xx']:,} / {junk['transport']:,}"],
        ["4xx breakdown", ", ".join(f"{k}: {v:,}" for k, v in sorted(four.items())) or "-"],
        ["5xx counted by the SLO (503 / timeouts while the normal users were shed too are not)",
         f"{junk['s5xx_counted']} (+ {junk['shed']} load-shedding 503s, {junk['transport'] - junk['transport_counted']} "
         "overload timeouts)"],
        ["socket errors (1011 / dropped) on junk WebSocket frames", junk["ws_bad"]],
        ["server.log: 5xx answered to junk addresses / their status mix",
         f"{log.get('5xx_junk', 0)} / " + (", ".join(f"{k}: {v:,}" for k, v in sorted(log.get('status_junk', {}).items())) or "-")],
        ["IDOR probes (other users' objects)", f"{len(junk['idor']):,}: " + ", ".join(
            f"{k}: {v}" for k, v in sorted(Counter(r['status'] or 'err' for r in junk['idor']).items()))],
        ["IDOR probes answered with a redirect: where to (review: a sign-in page or a list is fine)", ", ".join(
            f"{k} x{v}" for k, v in Counter(r["note"] for r in junk["idor"] if r["status"].startswith("3")
                                            and r["note"]).most_common(8)) or "-"],
        ["security failures (IDOR / open redirect / unescaped)", len(junk["security"])],
        ["failures in all (incl. the above)", junk["fails"]],
    ], "lr"))
    L.append("\nPer junk endpoint (5xx first, then by volume; top 60):\n")
    L.append(table(["endpoint", "requests", "2xx", "3xx", "4xx", "5xx", "transport", "failures", "p95 ms", "4xx mix"],
                   [[e["name"], e["n"], e["2xx"], e["3xx"], e["4xx"], e["5xx"], e["transport"], e["fail"],
                     fmt_ms(e["p95"]), e["top4xx"] or "-"] for e in junk["endpoints"][:60]], "lrrrrrrrrl"))
    if junk["findings"]:
        L.append("\n### Junk failures with their input (first 25; all in junk_failures_*.jsonl)\n")
        for f in junk["findings"][:25]:
            when = datetime.fromtimestamp(float(f.get("t") or 0), IST).strftime("%H:%M:%S")
            L.append(f"- **{f.get('name')}** -> {f.get('status') or 'no answer'} at {when} IST ({f.get('mode')}, "
                     f"{f.get('ip')}): {f.get('error')}")
            L.append(f"  - URL: `{str(f.get('url'))[:300]}`")
            if f.get("what"):
                L.append(f"  - input: `{str(f.get('what'))[:400]}`")
            if f.get("body") and f.get("body") not in str(f.get("what")):
                L.append(f"  - body ({f.get('content_type') or 'no content type'}): `{str(f.get('body'))[:400]}`")
            tb = f.get("traceback")
            if tb:
                L.append(f"  - server.log: `{tb['msg'][:200]}` -> `{tb['exception'][:200]}`")
                L.append("    ```\n" + "\n".join("    " + x for x in tb["tb_tail"].splitlines()) + "\n    ```")
            elif str(f.get("status") or "").startswith("5"):
                L.append("  - server.log: no traceback within 10 s of it (a 503 is load shedding / a handled error)")
    else:
        L.append("\n_no junk request failed_")
    return L


# ------------------------------------------------------------------------------------------------ recovery + memory
def analyze_recovery(steps: list[dict[str, Any]], step_rows: list[dict[str, Any]], rows: list[dict[str, Any]],
                     normal: list[dict[str, Any]], mon: list[dict[str, str]], slo_step: dict[str, Any] | None,
                     t0: float) -> dict[str, Any] | None:
    rec_i = next((i for i, s in enumerate(steps) if s["role"] == "recovery"), None)
    if rec_i is None:
        return None
    st, info = steps[rec_i], step_rows[rec_i]
    a, b = st["start"], st["end"]
    pages = [r for r in normal if r["cat"] == "page" and a <= r["t"] < b]
    norm = [r for r in normal if a <= r["t"] < b]
    recovered_at = None
    t = a
    while t + 10 <= b:
        w_pages = [r["ms"] for r in pages if t <= r["t"] < t + 10]
        w_all = [r for r in norm if t <= r["t"] < t + 10]
        ok = (not w_pages or pct(w_pages, 95) <= SLO["page_p95_ms"]) and \
             (not w_all or sum(1 for r in w_all if not r["ok"]) / len(w_all) <= SLO["error_rate"])
        if ok:
            later = [r for r in norm if t + 10 <= r["t"] < t + 30]
            later_p = [r["ms"] for r in pages if t + 10 <= r["t"] < t + 30]
            if (not later_p or pct(later_p, 95) <= SLO["page_p95_ms"]) and \
                    (not later or sum(1 for r in later if not r["ok"]) / len(later) <= SLO["error_rate"]):
                recovered_at = t
                break
        t += 5
    base = [v for m in mon_window(mon, slo_step["steady_s"] + t0, slo_step["end_s"] + t0)
            if (v := fnum(m, "pg_active")) is not None] if slo_step else []
    baseline = statistics.fmean(base) if base else None
    pg_back = None
    if baseline is not None:
        for m in mon_window(mon, a, b):
            v = fnum(m, "pg_active")
            if v is not None and v <= baseline + 2:
                pg_back = float(m["ts"]) - a
                break
    rows_out = [
        ["recovery step", f"{info['users']} users from {info['start_s']:.0f} s to {info['end_s']:.0f} s"],
        ["seconds until page p95 < 1 s and errors < 1 % (held 20 s)",
         f"{recovered_at - a:.0f} s" if recovered_at is not None else "**not recovered within the step**"],
        ["page p95 / errors in the recovery step's steady part", f"{fmt_ms(info['page_p95'])} ms / {fmt_pct(info['err'])}"],
        ["PostgreSQL active connections back to the SLO-step level",
         (f"after {pg_back:.0f} s (baseline {baseline:.1f})" if pg_back is not None else "not within the step")
         if baseline is not None else "-"],
        ["WebSocket drops during recovery", info["ws_drops"]],
    ]
    return {"seconds": (recovered_at - a) if recovered_at is not None else None, "pg_back_s": pg_back, "rows": rows_out}


def memory_verdict(steps: list[dict[str, Any]], mon: list[dict[str, str]]) -> dict[str, Any]:
    """Per-worker USS slope over the longest constant-load window, only when it is >= LEAK_MIN_CONSTANT_S long."""
    best = None
    for st in steps:
        span = st["end"] - st["steady"]
        if best is None or span > best[1]:
            best = (st, span)
    name = "no unbounded memory growth (worker USS at constant load)"
    if best is None or best[1] < LEAK_MIN_CONSTANT_S:
        return {"name": name, "ok": None,
                "detail": f"inconclusive: the longest constant-load window is {best[1] if best else 0:.0f} s; a leak verdict "
                          f"needs >= {LEAK_MIN_CONSTANT_S:.0f} s (LT_PROFILE=soak: 30 min at 200 users)"}
    st, span = best
    a = st["end"] - min(span, 1200.0)  # the last 20 minutes of it
    slopes = []
    for i in range(1, 9):
        pts = [(float(m["ts"]), v) for m in mon_window(mon, a, st["end"])
               if m.get(f"w{i}_pid") and (v := fnum(m, f"w{i}_uss_mb")) is not None and v > 0]
        if len(pts) >= 10:
            slopes.append(slope_per_min(pts))
    if not slopes:
        return {"name": name, "ok": None, "detail": "no per-worker USS samples"}
    worst = max(slopes)
    return {"name": name, "ok": worst < SLO["uss_mb_per_min"], "worst": worst,
            "detail": f"worst worker {worst:+.2f} MB/min over the last {min(span, 1200.0) / 60:.0f} min at {st['users']} "
                      f"users (limit {SLO['uss_mb_per_min']:.0f} MB/min per worker); all: "
                      + ", ".join(f"{x:+.2f}" for x in slopes)}


# ------------------------------------------------------------------------------------------------ summary.json
def page_summary(rd: Path, meta: dict[str, Any], cfg: dict[str, Any], seed: dict[str, Any], rows: list[dict[str, Any]],
                 normal: list[dict[str, Any]], abuse: list[dict[str, Any]], step_rows: list[dict[str, Any]],
                 slo_step: dict[str, Any] | None, knee: dict[str, Any] | None, capacity: dict[str, Any] | None,
                 verdicts: list[list[Any]], overall: str, inconclusive: list[str], history: list[dict[str, str]],
                 mon: list[dict[str, str]], by_name: dict[Any, list[dict[str, Any]]], by_cls: dict[str, list[dict[str, Any]]],
                 log: dict[str, Any], t0: float, t_end: float, duration: float, workers: int, pool: int,
                 leak: dict[str, Any], ai_mode: str, est: dict[str, Any] | None,
                 junk: dict[str, Any] | None = None, abusers: dict[str, Any] | None = None) -> dict[str, Any]:
    """summary.json, schema version 1, for GET /superadmin/load-test (app/services/loadtest_results.py)."""
    plat = cfg.get("platform") or {}
    ncpu = int(plat.get("cpus") or os.cpu_count() or 1)
    reqs = [r for r in rows if r["cat"] != "task"]
    tot = summarize(reqs)
    iso = lambda t: datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")  # noqa: E731
    users_at: list[tuple[float, int]] = []
    for h in history:
        if h.get("Name") == "Aggregated" and h.get("Timestamp"):
            try:
                users_at.append((float(h["Timestamp"]), int(float(h.get("User Count") or 0))))
            except ValueError:
                pass

    def users_for(t: float) -> int:
        best = None
        for ts, u in users_at:
            if ts <= t:
                best = u
            else:
                break
        if best is not None:
            return best
        st = next((s for s in step_rows if s["start_s"] + t0 <= t < s["end_s"] + t0), None)
        return st["users"] if st else 0

    timeline = []
    t = t0
    while t < t_end and len(timeline) < 2000:
        w = [r for r in reqs if t <= r["t"] < t + 10]
        ms = [r["ms"] for r in w]
        m = mon_window(mon, t, t + 10)
        cpu = [v for v in (fnum(x, "srv_cpu_pct") for x in m) if v is not None]
        rss = [v for v in (fnum(x, "srv_rss_mb") for x in m) if v is not None]
        pgc = [v for v in (fnum(x, "pg_total") for x in m) if v is not None]
        timeline.append({"t_s": round(t - t0, 1), "users": users_for(t + 5), "rps": round(len(w) / 10.0, 2),
                         "p50_ms": fin(round(pct(ms, 50), 1)) if ms else None, "p95_ms": fin(round(pct(ms, 95), 1)) if ms else None,
                         "failures_per_s": round(sum(1 for r in w if not r["ok"]) / 10.0, 2),
                         "cpu_pct": fin(round(min(100.0, statistics.fmean(cpu) / ncpu), 1)) if cpu else None,
                         "rss_mb": fin(round(statistics.fmean(rss), 1)) if rss else None,
                         "db_connections": int(max(pgc)) if pgc else None})
        t += 10
    endpoints = []
    for (rtype, name), rs in sorted(by_name.items(), key=lambda kv: -len(kv[1]))[:500]:
        if rtype == "TASK":
            continue
        s = summarize(rs)
        method, _, path = name.partition(" ") if re.match(r"^(GET|POST|PUT|PATCH|DELETE|WS) ", name) else (rtype, "", name)
        if name.startswith("ABUSE "):
            method, _, path = name[6:].partition(" ")
            path = "ABUSE " + path
        elif name.startswith("JUNK "):
            method, _, path = name[5:].partition(" ")
            path = "JUNK " + path
        endpoints.append({"name": (path or name)[:200], "method": (method or rtype)[:10],
                          "group": category(name, rtype), "requests": s["n"], "failures": s["fail"],
                          "p50_ms": fin(s["p50"]), "p95_ms": fin(s["p95"]), "p99_ms": fin(s["p99"]), "max_ms": fin(s["max"]),
                          "rps": round(s["n"] / duration, 3)})
    weights = meta.get("weights") or {}
    fixed = meta.get("fixed") or {}
    wsum = sum(v for k, v in weights.items() if not fixed.get(k)) or 1
    ref_users = (slo_step or {}).get("users") or max((s["users"] for s in step_rows), default=1) or 1
    fixed_total = sum(v for v in fixed.values() if v)
    classes = []
    for cls, rs in sorted(by_cls.items(), key=lambda kv: -len(kv[1]))[:50]:
        s = summarize(rs)
        share = (fixed[cls] / ref_users) if fixed.get(cls) else (weights.get(cls, 0) / wsum) * max(0.0, 1 - fixed_total / ref_users)
        classes.append({"name": cls[:80], "weight_share": round(min(1.0, max(0.0, share)), 4), "requests": s["n"],
                        "failures": s["fail"], "p95_ms": fin(s["p95"])})
    thr = sum(1 for r in abuse if r["status"] in ("429", "403"))
    slos = [{"name": v[0][:120], "target": "pass", "actual": str(v[2])[:60], "passed": v[1] == "PASS",
             **({"users": v[3]} if v[3] else {})} for v in verdicts if v[1] != "n/a"]
    for s in slos:  # targets in the page's words
        n = s["name"]
        s["target"] = ("< 1 %" if "error rate" in n else "< 1 s" if "p95" in n else "< 2.5 s" if "p99" in n else
                       "0" if n.startswith("zero") or n.startswith("no ") else "yes")
    pass_steps = [s for s in step_rows if s["role"] in ("rising", "peak", "constant")]
    headline = {"PASS": f"Within every SLO at {ref_users} users",
                "FAIL": "SLO failed: " + ", ".join(v[0] for v in verdicts if v[1] == "**FAIL**")[:200],
                "INCONCLUSIVE": "Inconclusive: " + "; ".join(inconclusive)[:250]}[overall]
    if knee:
        headline += f"; knee at {knee['users']} users"
    elif capacity:
        headline += f"; no knee up to {capacity['users']} users"
    # summary.json's steps count every request the server answered (normal users + abuse + junk senders), so the
    # capacity figure uses the same basis - the page's "max throughput" tile must match its step table.
    note = (f"Last step within the SLOs: {capacity['users']} users at {capacity['every_rps']:.0f} requests/s "
            f"({capacity['rps']:.0f} of them from normal users, the rest from the abuse and junk senders). "
            if capacity else "") + \
           (f"Estimated CPU saturation ~{est['sat_rps']:.0f} normal-user requests/s (~{est['sat_users']:.0f} users at "
            f"this run's think time"
            + (f", ~{est['stress_sat_users']:.0f} at 2-8 s" if est.get("stress_sat_users") else "")
            + f") from {est['cpu_ms']:.1f} ms worker CPU per request. " if est and est.get("sat_rps") == est.get("sat_rps") else "") + \
           f"AI mode: {ai_mode}."
    findings = []
    if knee:
        findings.append({"severity": "medium", "title": f"Latency/error knee at {knee['users']} users",
                         "detail": f"page p95 {fmt_ms(knee['page_p95'])} ms, errors {fmt_pct(knee['err'])}, 5xx {knee['n5xx']} "
                                   f"(503 {knee['n503']}), DB timeouts {knee['timeouts']}", "status": "open"})
    dirty = [s for s in step_rows if s["contaminated"]]
    if dirty:
        findings.append({"severity": "low",
                         "title": f"{len(dirty)} of {len(step_rows)} load steps contaminated by other programs on the machine",
                         "detail": "; ".join(f"{s['users']} users: other CPU {fmt_ms(s['other_cpu_max'])} %, load average "
                                             f"{s['load_max']:.1f}, cluster connections {fmt_ms(s['cluster_max'])}"
                                             for s in dirty)[:2000], "status": "accepted"})
    if log["worker_died"]:
        findings.append({"severity": "high", "title": "uvicorn workers restarted", "detail": "; ".join(log["worker_died"][:3]),
                         "status": "open"})
    for v in verdicts:
        if v[1] == "**FAIL**" and not v[3]:  # whole-run checks (the SLO-step rows are the verdict itself)
            findings.append({"severity": "high", "title": f"Failed: {v[0]}"[:200], "detail": str(v[2])[:2000],
                             "status": "open"})
    shed = [s for s in step_rows if s["n503"] or s["timeouts"]]
    if shed:
        first = shed[0]
        findings.append({"severity": "medium", "title": f"Load shedding (503 / DB timeouts) from {first['users']} users",
                         "detail": "; ".join(f"{s['users']} users: {s['n503']} x 503, {s['timeouts_stmt']} statement / "
                                             f"{s['timeouts_pool']} pool timeouts" for s in shed)[:2000], "status": "open"})
    if slo_step:
        slow_pages = []
        for (rtype, name), rs in by_name.items():
            w = [r["ms"] for r in rs if r["cat"] == "page" and not r["abuse"]
                 and slo_step["steady_s"] + t0 <= r["t"] < slo_step["end_s"] + t0]
            if len(w) >= 5 and pct(w, 95) > SLO["page_p95_ms"]:
                slow_pages.append((pct(w, 95), name, len(w)))
        for p95, name, n in sorted(slow_pages, reverse=True)[:5]:
            findings.append({"severity": "medium", "title": f"{name} p95 {p95:,.0f} ms at {slo_step['users']} users"[:200],
                             "detail": f"{n} requests in the SLO step's steady part; the page SLO is p95 < 1 s",
                             "status": "open"})
    sat = [s for s in step_rows if s["generator_saturated"]]
    if sat:
        findings.append({"severity": "low", "title": f"Load generator saturated from {sat[0]['users']} users",
                         "detail": "a Locust process went above 80 % CPU (or Locust warned): latencies of these steps include "
                                   "client-side queueing - rerun with more --processes", "status": "open"})
    overflow = [s for s in step_rows if s["listen_overflow"]]
    if overflow:
        findings.append({"severity": "low", "title": f"Listen-queue overflows from {overflow[0]['users']} users",
                         "detail": "macOS caps the accept backlog at kern.ipc.somaxconn (128): SYN retries add 1-3 s that a "
                                   "Linux server (backlog 4096) would not show", "status": "accepted"})
    if log["redis_down"]:
        findings.append({"severity": "low", "title": f"Shared rate limits fell back to per-worker memory "
                                                     f"{len(log['redis_down'])} times (Redis timeout)",
                         "detail": "app/security/ratelimit.py: a Redis call slower than RATE_LIMIT_REDIS_TIMEOUT_MS switches "
                                   "rate limits, threat scores and the sign-in lockout to each worker's memory until Redis "
                                   "answers again (per step: report.md)", "status": "open"})
    if leak["ok"] is None:
        findings.append({"severity": "low", "title": "Memory-leak check not judged in this run",
                         "detail": leak["detail"][:2000], "status": "accepted"})
    if junk and junk["requests"]:
        for name, n in Counter(r["name"] for r in junk["five_counted"]).most_common(10):
            ex = next((f for f in junk["findings"] if f.get("name") == name
                       and str(f.get("status") or "").startswith("5")), None)
            tb = (ex or {}).get("traceback") or {}
            findings.insert(0, {"severity": "high", "title": f"5xx under random junk input: {name} ({n}x)"[:200],
                                "detail": (f"input: {(ex or {}).get('what') or (ex or {}).get('url') or '?'}"[:900]
                                           + (f" | server: {tb.get('exception', '')}" if tb else "")
                                           + " | report.md 'Random junk input' has the request and the traceback")[:2000],
                                "status": "open"})
        for kind in ("IDOR", "open redirect", "XSS"):
            hits = [r for r in junk["security"] if kind in r["err"]]
            if hits:
                findings.insert(0, {"severity": "high", "title": f"{kind} found by the junk senders ({len(hits)}x)"[:200],
                                    "detail": "; ".join(sorted({f"{r['name']}: {r['err']}" for r in hits}))[:2000],
                                    "status": "open"})
    summary = {
        "schema": 1, "run_id": rd.name[:64], "started_at": iso(t0), "ended_at": iso(max(t_end, t0)),
        "duration_s": round(duration, 1),
        "target": {"host": str(meta.get("host") or "http://127.0.0.1:12000")[:200], "workers": workers,
                   "environment": str(cfg.get("ENVIRONMENT") or "production")[:40], "redis": True,
                   "notes": (f"profile {meta.get('profile')}, AI {ai_mode}, DB pool {cfg.get('DB_POOL_SIZE')}+"
                             f"{cfg.get('DB_MAX_OVERFLOW')} per worker, WebSocket {meta.get('ws_mode')}, think time "
                             f"{meta.get('wait')} s; seeded with loadtest/seed_loadtest.py")[:2000],
                   "db_rows": dict(sorted(((k, int(v)) for k, v in (seed.get("table_rows") or {}).items()
                                           if not k.startswith("_") and isinstance(v, int) and v > 0),
                                          key=lambda kv: -kv[1])[:100])},
        "machine": {"cpus": ncpu, "memory_gb": float(plat.get("memory_gb") or 0),
                    "os": str(plat.get("os") or platform.platform())[:120], "python": platform.python_version(),
                    "locust_version": str(meta.get("locust_version") or "?")[:40]},
        "verdict": {"passed": overall == "PASS", "headline": headline[:300], "slos": slos[:50]},
        "capacity": {"max_users_within_slo": int(capacity["users"]) if capacity else 0,
                     "max_rps_within_slo": round(capacity["every_rps"], 2) if capacity else 0.0,
                     "knee_users": int(knee["users"]) if knee else None, "note": note[:1000]},
        "totals": {"requests": tot["n"], "failures": tot["fail"], "error_rate": round(fin(tot["rate"]) or 0.0, 6),
                   "rps": round(tot["n"] / duration, 2), "p50_ms": fin(tot["p50"]) or 0.0, "p95_ms": fin(tot["p95"]) or 0.0,
                   "p99_ms": fin(tot["p99"]) or 0.0, "max_ms": fin(tot["max"]) or 0.0},
        "steps": [{"users": s["users"], "start_s": round(s["start_s"], 1), "end_s": round(max(s["end_s"], s["start_s"]), 1),
                   "rps": round(s["every_rps"], 2), "error_rate": round(fin(s["every"]["rate"]) or 0.0, 6),
                   "p50_ms": fin(s["every"]["p50"]), "p95_ms": fin(s["every"]["p95"]), "p99_ms": fin(s["every"]["p99"]),
                   "slo_p95_ms": fin(s["page_p95"]), "passed": bool(s["within"])} for s in step_rows][:100],
        "timeline": timeline,
        "endpoints": endpoints,
        "classes": classes,
        "abuse": {"requests": len(abuse), "throttled": thr, "throttled_share": round(thr / len(abuse), 4) if abuse else 0.0,
                  "note": "429/403 from rate limits and bans; the 404 scan and the WebSocket flood are judged in report.md"
                          + (f"; {len(abusers['shed'])} overload failures (503 / timeouts while the normal users were "
                             "shed too) not counted" if abusers and abusers["shed"] else ""),
                  # extra keys (the page's schema-1 loader ignores them): the verdict and what it did not count
                  **({"passed": abusers["ok"], "overload_not_counted": len(abusers["shed"]),
                      "problems": abusers["problems"][:20]} if abusers else {})},
        "server": {"peak_cpu_pct": fin(round(min(100.0, max((fnum(m, "srv_cpu_pct") or 0) for m in mon) / ncpu), 1)) if mon else None,
                   "peak_rss_mb": fin(max((fnum(m, "srv_rss_mb") or 0) for m in mon)) if mon else None,
                   "rss_growth_mb_per_min": fin(round(leak["worst"], 2)) if leak.get("worst") is not None else None,
                   "peak_db_connections": int(max((fnum(m, "pg_total") or 0) for m in mon)) if mon else None,
                   "pool_size": workers * int(cfg.get("DB_POOL_SIZE") or 10), "errors_5xx": sum(1 for r in rows if r["status"].startswith("5")),
                   "timeouts": len(log["timeouts_ts"]), "log_errors": [e[:300] for e in log["errors"][:50]]},
        "findings": findings[:100],
        "files": {"report_md": "report.md", "locust_html": "locust_report.html" if (rd / "locust_report.html").is_file() else None},
    }
    if junk and junk["requests"]:  # extra key: the page's schema-1 loader ignores it; the SLO row above is what it shows
        summary["junk"] = {"requests": junk["requests"], "status_2xx": junk["s2xx"], "status_3xx": junk["s3xx"],
                           "status_4xx": junk["s4xx"], "status_5xx": junk["s5xx"],
                           "status_5xx_counted": junk["s5xx_counted"], "load_shedding_503": junk["shed"],
                           "transport_errors": junk["transport"], "socket_errors": junk["ws_bad"],
                           "four_xx": {k: v for k, v in sorted(junk["status"].items()) if k.startswith("4")},
                           "idor_probes": len(junk["idor"]), "security_failures": len(junk["security"]),
                           "five_xx_endpoints": dict(Counter(r["name"] for r in junk["five_counted"]).most_common(20)),
                           "passed": bool(junk["slo_ok"])}
    return summary


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def nonfinite_paths(obj: Any, path: str = "") -> list[str]:
    """Field paths of NaN / infinity values (summary.json must be finite: docs/load-test-page.md)."""
    if isinstance(obj, float) and (obj != obj or obj in (float("inf"), float("-inf"))):
        return [path or "summary"]
    if isinstance(obj, dict):
        return [p for k, v in obj.items() for p in nonfinite_paths(v, f"{path}.{k}" if path else str(k))]
    if isinstance(obj, list):
        return [p for i, v in enumerate(obj) for p in nonfinite_paths(v, f"{path}[{i}]")]
    return []


VALIDATOR = r"""
import sys
sys.path.insert(0, sys.argv[1])
from app.services.loadtest_results import SummaryError, parse_summary
try:
    s = parse_summary(sys.stdin.buffer.read())
except SummaryError as exc:
    print(str(exc))
    sys.exit(1)
print(f"schema {s.schema_version if hasattr(s, 'schema_version') else 1}: verdict passed={s.verdict.passed}, "
      f"{len(s.steps)} steps, {len(s.timeline)} timeline points, {len(s.endpoints)} endpoints, {len(s.classes)} classes, "
      f"{len(s.findings)} findings")
"""


def validate_summary(raw: bytes) -> str:
    """Run the app's own loader on the bytes (project interpreter, clean environment); SystemExit(2) on rejection."""
    py = Path(os.environ.get("LT_PROJECT_PYTHON") or PROJECT_PYTHON)
    if not py.is_file():
        print(f"[analyze] ERROR: cannot validate summary.json - no project interpreter at {py} (the app's parse_summary "
              "needs the project .venv; or set LT_PROJECT_PYTHON)", file=sys.stderr)
        raise SystemExit(2)
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", ""), "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
    try:
        p = subprocess.run([str(py), "-I", "-c", VALIDATOR, str(HERE.parent)], input=raw, capture_output=True, timeout=120,
                           env=env, cwd=str(HERE.parent))
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[analyze] ERROR: the summary.json validator did not run: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    out = p.stdout.decode("utf-8", "replace").strip()
    if p.returncode != 0:
        why = out or (p.stderr.decode("utf-8", "replace").strip().splitlines() or ["?"])[-1]
        print(f"[analyze] ERROR: summary.json rejected by app.services.loadtest_results.parse_summary - NOT written: {why}",
              file=sys.stderr)
        raise SystemExit(2)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?", help="loadtest/results/<run> (default: the current run)")
    ap.add_argument("--slo-users", type=int, default=200, help="the load step the SLOs are judged at (default 200)")
    args = ap.parse_args()
    if args.run_dir:
        rd = Path(args.run_dir)
    else:
        cur = HERE / ".current_run"
        if not cur.is_file():
            raise SystemExit("no run given and no loadtest/.current_run")
        rd = HERE / "results" / cur.read_text(encoding="utf-8").strip()
    report, summary, page = analyze(rd, args.slo_users)
    write_atomic(rd / "report.md", report)
    (rd / "report_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    bad = nonfinite_paths(page)
    if bad:
        print(f"[analyze] ERROR: summary.json would contain non-finite numbers at {', '.join(bad[:10])} - NOT written",
              file=sys.stderr)
        return 2
    for key, name in (page.get("files") or {}).items():
        if name and not (rd / name).is_file():
            print(f"[analyze] ERROR: summary.json files.{key} = {name!r} does not exist in {rd} - NOT written", file=sys.stderr)
            return 2
    text = json.dumps(page, indent=1, allow_nan=False, default=str)
    checked = validate_summary(text.encode("utf-8"))
    write_atomic(rd / "summary.json", text)
    print(f"[analyze] {rd / 'report.md'} - overall {summary['overall']}; {summary['requests']:,} requests, "
          f"{summary['normal_failures']} normal-user failures; knee {summary['knee']}")
    print(f"[analyze] {rd / 'summary.json'} validated by the app's parse_summary ({checked})")
    for v in summary["verdicts"]:
        print(f"  {v[1]:9} {v[0]}: {v[2]}")
    for why in summary["inconclusive"]:
        print(f"  INCONCLUSIVE: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
