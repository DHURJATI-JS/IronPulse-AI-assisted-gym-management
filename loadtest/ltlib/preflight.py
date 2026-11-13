"""Is this shared machine quiet enough for a load test? (run_server.sh start, the locustfile's init hook, monitor.py)

    .venv-loadtest/bin/python loadtest/ltlib/preflight.py [--json] [--max-load 2.0] [--max-other-conns 30]

Checks (each one a reason to refuse unless forced - the numbers would otherwise show the other teams' work as app
limits, and a run could starve their tests of PostgreSQL connections):
  * 1-minute load average above --max-load (default 2.0 on this 10-CPU machine)
  * a pytest process running (the other teams' test suites)
  * PostgreSQL connections of OTHER databases above --max-other-conns (default 30): max_connections is 100 with 3
    reserved, and the load test may take up to 70 (its database's CONNECTION LIMIT)
Exit status 0 = quiet, 1 = busy (reasons printed). Standard library + psutil (optional); psql from the runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import ltenv  # noqa: E402

MAX_LOAD = float(os.environ.get("LT_PREFLIGHT_MAX_LOAD", "2.0"))
MAX_OTHER_CONNS = int(os.environ.get("LT_PREFLIGHT_MAX_OTHER_CONNS", "30"))
CLUSTER_SQL = (f"SELECT count(*), count(*) FILTER (WHERE datname = '{ltenv.DB_NAME}'), "
               "current_setting('max_connections')::int FROM pg_stat_activity WHERE backend_type = 'client backend'")


def psql_rows(query: str, timeout: float = 4.0) -> list[list[str]]:
    env = dict(os.environ, PGPASSWORD=ltenv.superuser_password(), PGCONNECT_TIMEOUT="3")
    p = ltenv.db_parts()
    try:
        out = subprocess.run([str(ltenv.PSQL), "-h", str(p["host"]), "-p", str(p["port"]), "-U", "postgres", "-d",
                              "postgres", "-X", "-A", "-t", "-F", "|", "-c", query], env=env, capture_output=True,
                             text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return []
    return [line.split("|") for line in out.stdout.splitlines() if line.strip()]


def pytest_processes() -> list[str]:
    me = os.getpid()
    found: list[str] = []
    try:
        import psutil  # noqa: PLC0415

        for p in psutil.process_iter(["pid", "cmdline"]):
            cmd = " ".join(p.info.get("cmdline") or [])
            if p.info["pid"] != me and ("pytest" in cmd) and "preflight" not in cmd and "grep" not in cmd:
                found.append(f"{p.info['pid']}: {cmd[:120]}")
    except ImportError:
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if "pytest" in line and "preflight" not in line and "grep" not in line:
                found.append(line.strip()[:130])
    return found


def check(max_load: float = MAX_LOAD, max_other: int = MAX_OTHER_CONNS) -> dict[str, Any]:
    load1, load5, load15 = os.getloadavg()
    rows = psql_rows(CLUSTER_SQL)
    total = ours = maxc = None
    if rows and len(rows[0]) >= 3:
        total, ours, maxc = int(rows[0][0]), int(rows[0][1]), int(rows[0][2])
    other = None if total is None else total - (ours or 0)
    tests = pytest_processes()
    reasons: list[str] = []
    if load1 > max_load:
        reasons.append(f"load average {load1:.2f} > {max_load:g} (1 min; 5 min {load5:.2f})")
    if tests:
        reasons.append(f"{len(tests)} pytest process(es) running, e.g. {tests[0]}")
    if other is not None and other > max_other:
        reasons.append(f"{other} PostgreSQL client connections of other databases > {max_other} "
                       f"(max_connections {maxc}; the load test may take up to {ltenv.DB_CONN_LIMIT})")
    if other is None:
        reasons.append("could not read pg_stat_activity (psql as superuser) - the connection check was skipped")
    return {"ok": not reasons, "reasons": reasons, "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
            "cpus": os.cpu_count(), "pytest": tests[:5], "pg_connections_total": total, "pg_connections_loadtest": ours,
            "pg_connections_other": other, "pg_max_connections": maxc}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    ap.add_argument("--max-load", type=float, default=MAX_LOAD)
    ap.add_argument("--max-other-conns", type=int, default=MAX_OTHER_CONNS)
    args = ap.parse_args()
    res = check(args.max_load, args.max_other_conns)
    if args.json:
        print(json.dumps(res))
    else:
        print(f"[preflight] load {res['load']} on {res['cpus']} CPUs; PostgreSQL client connections: "
              f"{res['pg_connections_other']} other databases + {res['pg_connections_loadtest']} load test "
              f"(max_connections {res['pg_max_connections']}); pytest processes: {len(res['pytest'])}")
        for r in res["reasons"]:
            print(f"[preflight] BUSY: {r}")
        if res["ok"]:
            print("[preflight] OK - the machine is quiet")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
