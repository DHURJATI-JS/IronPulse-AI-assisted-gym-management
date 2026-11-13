"""Resource monitor of a load-test run: one CSV row every 5 s until stopped (SIGTERM / SIGINT / Ctrl+C).

    .venv-loadtest/bin/python loadtest/monitor.py --run-dir loadtest/results/<run> --server-pid <uvicorn master PID>
    (started and stopped by loadtest/run_server.sh)

Columns of <run dir>/monitor.csv:
  ts, elapsed_s                                    epoch seconds, seconds since the monitor started
  srv_procs, srv_cpu_pct, srv_rss_mb, srv_uss_mb   the uvicorn master + every worker (CPU % of ONE core, summed);
                                                   USS = memory unique to the process (what it would free on exit) -
                                                   the leak signal; RSS on macOS moves with memory compression
  master_cpu_pct, master_rss_mb, w<i>_pid/_cpu_pct/_rss_mb/_uss_mb   per process (up to 8 workers, by PID order)
  loadgen_procs, loadgen_cpu_pct, loadgen_cpu_max_proc, loadgen_rss_mb   the Locust processes (sum; busiest one)
  pg_backend_cpu_pct                               CPU of the PostgreSQL backends serving ironpulse_loadtest
  pg_total, pg_active, pg_idle, pg_idle_in_tx, pg_idle_in_tx_aborted, pg_other, pg_waiting_lock, pg_longest_query_s
                                                   pg_stat_activity of ironpulse_loadtest (superuser, via psql)
  pg_cluster_total, pg_cluster_other               client connections of the whole cluster / of other databases
  sys_cpu_pct, load1                               whole machine: CPU busy % (0-100 of all cores), 1-min load average
  other_cpu_pct, other_top                         CPU (% of one core) of everything that is NOT this test (other
                                                   teams' pytest + their PostgreSQL backends, IDEs, ...) and its top 5
  tcp_listen_overflow, tcp_time_wait               cumulative 'listen queue overflow' (netstat -s; macOS caps the
                                                   accept backlog at kern.ipc.somaxconn=128) and TIME_WAIT sockets
  redis_instance_used_memory_mb, redis_used_memory_delta_mb   the WHOLE Redis instance (shared with db 0) and its
                                                   change since the monitor started; redis_clients_db9, redis_keys_db9,
                                                   redis_clients_total
  log_lines, log_5xx, log_429, log_traceback, log_timeout, log_querycanceled, log_pool_timeout, log_error,
  log_worker_died, log_server_started, log_asgi_exception, log_db_timeout_warning, log_5xx_junk
                                                   cumulative counts of server.log lines (tailed incrementally);
                                                   log_5xx_junk = the 5xx of them answered to a JunkUser address
                                                   (ltenv.junk_ip: kept out of the normal users' 5xx SLO)
Side file <run dir>/pg_slow.csv: every query of ironpulse_loadtest seen active for more than 1 s (text, wait event),
and every connection seen IDLE IN TRANSACTION for more than 1 s (its last statement; wait_event_type "idle in
transaction") - a request or job holding a pooled connection while it awaits something else.
Only reads: PIDs it is given, pg_stat_activity, Redis INFO/CLIENT LIST, netstat counters, the run's server.log.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ltenv  # noqa: E402

MAX_WORKERS = 8
SLOW_QUERY_S = 1.0
ACCESS_STATUS = re.compile(r'" (\d{3})(?: |$)')
ACCESS_CLIENT = re.compile(r'^\w+:\s+(\S+?):\d+ - "')  # uvicorn access line: the X-Forwarded-For client address
PATTERNS = {
    "log_traceback": re.compile(r"Traceback \(most recent call last\)"),
    "log_timeout": re.compile(r"timeout|timed out", re.I),
    "log_querycanceled": re.compile(r"QueryCanceled|statement timeout|canceling statement", re.I),
    "log_pool_timeout": re.compile(r"pool timeout|QueuePool limit|TimeoutError: QueuePool", re.I),
    "log_error": re.compile(r"\bERROR\b|\bCRITICAL\b"),
    "log_worker_died": re.compile(r"Child process \[\d+\] (?:died|failed to start)"),  # uvicorn's supervisor restarts
    "log_server_started": re.compile(r"Started server process \[\d+\]"),
    "log_asgi_exception": re.compile(r"Exception in ASGI application"),
    "log_db_timeout_warning": re.compile(r"database (?:statement|pool) timeout on "),
}

running = True


def _stop(*_: object) -> None:
    global running
    running = False


class LogTail:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.partial = b""
        self.counts = {"log_lines": 0, "log_5xx": 0, "log_429": 0, **{k: 0 for k in PATTERNS}, "log_5xx_junk": 0}

    def poll(self) -> dict[str, int]:
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                data = fh.read()
                self.offset = fh.tell()
        except OSError:
            return self.counts
        data = self.partial + data
        lines = data.split(b"\n")
        self.partial = lines.pop()  # an unfinished last line waits for the next poll
        for raw in lines:
            line = raw.decode("utf-8", "replace")
            self.counts["log_lines"] += 1
            m = ACCESS_STATUS.search(line) if '"' in line and "HTTP/" in line else None
            if m:
                code = int(m.group(1))
                if code >= 500:
                    self.counts["log_5xx"] += 1
                    who = ACCESS_CLIENT.match(line)
                    if who and ltenv.junk_ip(who.group(1)):
                        self.counts["log_5xx_junk"] += 1
                elif code == 429:
                    self.counts["log_429"] += 1
                continue  # an access line is not an application message
            for key, rx in PATTERNS.items():
                if rx.search(line):
                    self.counts[key] += 1
        return self.counts


def psql(query: str) -> list[list[str]]:
    env = dict(os.environ, PGPASSWORD=ltenv.superuser_password(), PGCONNECT_TIMEOUT="3")
    p = ltenv.db_parts()
    try:
        out = subprocess.run([str(ltenv.PSQL), "-h", str(p["host"]), "-p", str(p["port"]), "-U", "postgres", "-d", "postgres",
                              "-X", "-A", "-t", "-F", "\x1f", "-c", query], env=env, capture_output=True, text=True, timeout=4)
    except (subprocess.TimeoutExpired, OSError):
        return []
    return [line.split("\x1f") for line in out.stdout.splitlines() if line.strip()]


def redis(*args: str) -> str:
    env = dict(os.environ, REDISCLI_AUTH=ltenv.redis_password())
    try:
        out = subprocess.run([str(ltenv.REDIS_CLI), "-h", "127.0.0.1", "-p", "6379", "-n", str(ltenv.REDIS_DB), *args],
                             env=env, capture_output=True, text=True, timeout=3)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return out.stdout


def sh(*cmd: str, timeout: float = 4.0) -> str:
    try:
        return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout).stdout
    except (subprocess.TimeoutExpired, OSError):
        return ""


# every client backend of the cluster (one row each): ours are counted by state, the others only counted
PG_QUERY = """
SELECT pid, coalesce(datname, ''), coalesce(state, ''), coalesce(wait_event_type, ''), coalesce(wait_event, ''),
       coalesce(round(extract(epoch FROM now() - query_start)::numeric, 2)::text, ''),
       coalesce(extract(epoch FROM query_start)::bigint::text, ''),
       regexp_replace(left(coalesce(query, ''), 300), '[[:cntrl:]]+', ' ', 'g'),
       coalesce(round(extract(epoch FROM now() - state_change)::numeric, 2)::text, '')
FROM pg_stat_activity WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()
"""


def proc_sample(p: psutil.Process, uss: bool = False) -> tuple[float, float, float]:
    try:
        cpu = p.cpu_percent(None)
        if uss:
            mi = p.memory_full_info()
            return cpu, mi.rss / 1048576.0, mi.uss / 1048576.0
        return cpu, p.memory_info().rss / 1048576.0, 0.0
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return 0.0, 0.0, 0.0


def tcp_counters() -> tuple[str, str]:
    stats = sh("netstat", "-s", "-p", "tcp")
    m = re.search(r"(\d+) listen queue overflow", stats)
    socks = sh("netstat", "-an", "-p", "tcp")
    return (m.group(1) if m else ""), str(socks.count("TIME_WAIT"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--server-pid", type=int, required=True)
    ap.add_argument("--interval", type=float, default=5.0)
    args = ap.parse_args()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    rd = Path(args.run_dir)
    tail = LogTail(rd / "server.log")
    cache: dict[int, psutil.Process] = {}
    me = os.getpid()
    ncpu = os.cpu_count() or 1

    def proc(pid: int) -> psutil.Process | None:
        p = cache.get(pid)
        if p is None:
            try:
                p = cache[pid] = psutil.Process(pid)
                p.cpu_percent(None)  # prime: the next call measures since now
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return None
        return p

    header = ["ts", "elapsed_s", "srv_procs", "srv_cpu_pct", "srv_rss_mb", "srv_uss_mb", "master_cpu_pct", "master_rss_mb"]
    for i in range(1, MAX_WORKERS + 1):
        header += [f"w{i}_pid", f"w{i}_cpu_pct", f"w{i}_rss_mb", f"w{i}_uss_mb"]
    header += ["loadgen_procs", "loadgen_cpu_pct", "loadgen_cpu_max_proc", "loadgen_rss_mb", "pg_backend_cpu_pct",
               "pg_total", "pg_active", "pg_idle", "pg_idle_in_tx", "pg_idle_in_tx_aborted", "pg_other",
               "pg_waiting_lock", "pg_longest_query_s", "pg_cluster_total", "pg_cluster_other",
               "sys_cpu_pct", "load1", "other_cpu_pct", "other_top", "tcp_listen_overflow", "tcp_time_wait",
               "redis_instance_used_memory_mb", "redis_used_memory_delta_mb", "redis_clients_db9", "redis_keys_db9",
               "redis_clients_total", "log_lines", "log_5xx", "log_429", *PATTERNS.keys(), "log_5xx_junk"]
    out_path = rd / "monitor.csv"
    new = not out_path.exists()
    fh = open(out_path, "a", newline="", encoding="utf-8")  # noqa: SIM115
    w = csv.writer(fh)
    if new:
        w.writerow(header)
    slow_path = rd / "pg_slow.csv"
    slow_new = not slow_path.exists()
    slow_fh = open(slow_path, "a", newline="", encoding="utf-8")  # noqa: SIM115
    slow_w = csv.writer(slow_fh)
    if slow_new:
        slow_w.writerow(["ts", "pid", "query_start", "active_s", "wait_event_type", "wait_event", "query"])
    t0 = time.time()
    first = True
    redis_base: float | None = None
    psutil.cpu_percent(None)  # prime the whole-machine counter
    while running:
        tick = time.time()
        master = proc(args.server_pid)
        if master is None or not master.is_running():
            print("[monitor] the server is gone - stopping", flush=True)
            break
        try:
            kids = sorted(master.children(recursive=True), key=lambda c: c.pid)
        except psutil.NoSuchProcess:
            kids = []

        def is_worker(k: psutil.Process) -> bool:  # uvicorn workers run multiprocessing's spawn_main
            try:
                return "spawn_main" in " ".join(k.cmdline())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return False

        workers = [x for x in (proc(k.pid) for k in kids if is_worker(k)) if x is not None]
        aux = [x for x in (proc(k.pid) for k in kids if not is_worker(k)) if x is not None]  # resource tracker
        m_cpu, m_rss, _ = proc_sample(master)
        for x in aux:
            a_cpu, a_rss, _ = proc_sample(x)
            m_cpu, m_rss = m_cpu + a_cpu, m_rss + a_rss  # counted with the master (it is its helper)
        wsamples = [(x.pid, *proc_sample(x, uss=True)) for x in workers]
        test_pids = {args.server_pid, me, *(x.pid for x in workers), *(x.pid for x in aux)}

        # ---- the load generator, every other process (who else is using this machine?)
        lg: list[tuple[float, float, float]] = []
        others: list[tuple[float, str, int]] = []
        infra_names = ("redis-server", "smtp_sink", "WindowServer", "kernel_task")
        pg = psql(PG_QUERY)
        our_backends = {int(r[0]) for r in pg if len(r) >= 8 and r[1] == ltenv.DB_NAME}
        client_backends = {int(r[0]) for r in pg if r and r[0].isdigit()}
        test_pids |= our_backends
        other_cpu = 0.0
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            pid = p.info["pid"]
            if pid in test_pids:
                continue
            name = p.info.get("name") or ""
            cmd = " ".join(p.info.get("cmdline") or [])
            try:
                cpu = p.cpu_percent(None)  # psutil's process_iter cache keeps the Process: measures since last tick
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            if "locustfile.py" in cmd or ("locust" in name.lower() and "locust" in cmd):
                q = proc(pid)
                if q is not None:
                    lg.append(proc_sample(q))
                continue
            if "smtp_sink.py" in cmd or name in infra_names or "monitor.py" in cmd:
                continue  # infrastructure of this test (Redis serves db 9; mostly our traffic during a run)
            if name == "postgres" and pid not in client_backends:
                continue  # postmaster / checkpointer / walwriter / autovacuum: shared infrastructure
            other_cpu += cpu
            if cpu >= 1.0:
                others.append((cpu, (os.path.basename(cmd.split(" ")[0]) if cmd else name)[:24], pid))
        others.sort(reverse=True)
        other_top = ";".join(f"{n}:{pid}:{c:.0f}" for c, n, pid in others[:5])

        # ---- PostgreSQL
        mine = [r for r in pg if len(r) >= 8 and r[1] == ltenv.DB_NAME]
        states = [r[2] for r in mine]
        known = ("active", "idle", "idle in transaction", "idle in transaction (aborted)")
        longest = max((float(r[5]) for r in mine if r[2] == "active" and r[5]), default=0.0)
        pg_row = [len(mine), states.count("active"), states.count("idle"), states.count("idle in transaction"),
                  states.count("idle in transaction (aborted)"), sum(1 for s in states if s not in known),
                  sum(1 for r in mine if r[3] == "Lock"), f"{longest:.2f}"] if pg else [""] * 8
        cluster_total = len(pg) if pg else ""
        cluster_other = (len(pg) - len(mine)) if pg else ""
        for r in mine:
            if r[2] == "active" and r[5] and float(r[5]) >= SLOW_QUERY_S:
                slow_w.writerow([f"{tick:.1f}", r[0], r[6], r[5], r[3], r[4], r[7]])
            elif r[2].startswith("idle in transaction") and len(r) >= 9 and r[8] and float(r[8]) >= SLOW_QUERY_S:
                slow_w.writerow([f"{tick:.1f}", r[0], r[6], r[8], "idle in transaction", r[2], r[7]])
        pg_cpu = 0.0
        for pid in our_backends:
            q = proc(pid)
            if q is not None:
                pg_cpu += proc_sample(q)[0]

        # ---- Redis, TCP, log
        info = redis("INFO", "memory")
        mem = re.search(r"used_memory:(\d+)", info)
        mem_mb = int(mem.group(1)) / 1048576.0 if mem else None
        if mem_mb is not None and redis_base is None:
            redis_base = mem_mb
        clients = redis("CLIENT", "LIST")
        db9 = sum(1 for line in clients.splitlines() if f" db={ltenv.REDIS_DB} " in f" {line} ")
        keys = redis("DBSIZE").strip()
        overflow, time_wait = tcp_counters()
        counts = tail.poll()
        sys_cpu = psutil.cpu_percent(None)

        row = [f"{tick:.1f}", f"{tick - t0:.1f}", 1 + len(wsamples), f"{m_cpu + sum(s[1] for s in wsamples):.1f}",
               f"{m_rss + sum(s[2] for s in wsamples):.1f}", f"{sum(s[3] for s in wsamples):.1f}", f"{m_cpu:.1f}",
               f"{m_rss:.1f}"]
        for i in range(MAX_WORKERS):
            if i < len(wsamples):
                pid, cpu, rss, uss = wsamples[i]
                row += [pid, f"{cpu:.1f}", f"{rss:.1f}", f"{uss:.1f}"]
            else:
                row += ["", "", "", ""]
        row += [len(lg), f"{sum(x[0] for x in lg):.1f}", f"{max((x[0] for x in lg), default=0.0):.1f}",
                f"{sum(x[1] for x in lg):.1f}", f"{pg_cpu:.1f}", *pg_row, cluster_total, cluster_other,
                f"{sys_cpu:.1f}", f"{os.getloadavg()[0]:.2f}", f"{other_cpu:.1f}", other_top, overflow, time_wait,
                f"{mem_mb:.2f}" if mem_mb is not None else "",
                f"{mem_mb - redis_base:.2f}" if mem_mb is not None and redis_base is not None else "",
                db9, keys, len(clients.splitlines()),
                *[counts[k] for k in ["log_lines", "log_5xx", "log_429", *PATTERNS.keys(), "log_5xx_junk"]]]
        if not first:  # the first sample only primes the CPU counters
            w.writerow(row)
            fh.flush()
            slow_fh.flush()
        first = False
        for pid in [p for p in cache if not psutil.pid_exists(p)]:
            cache.pop(pid, None)
        while running and time.time() - tick < args.interval:
            time.sleep(0.25)
    fh.close()
    slow_fh.close()
    print(f"[monitor] stopped ({ncpu} CPUs)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
