#!/usr/bin/env bash
# IronPulse LOAD-TEST server: production mode, 4 uvicorn workers, 127.0.0.1:12000, database ironpulse_loadtest,
# Redis db 9, files under loadtest/var - plus the resource monitor. Never touches the dev servers (10000 / 11000).
#
#   loadtest/run_server.sh start [--run NAME] [--ai simulated|off|gateway] [--smtp spool|sink] [--workers N]
#                                [--backlog N] [--no-monitor] [--force]
#   loadtest/run_server.sh warmup [SECONDS]   # wait until SECONDS (default 330) after the server became healthy
#   loadtest/run_server.sh stop               # monitor + SMTP sink + server (only the processes this script started)
#   loadtest/run_server.sh status
#
# PID files: a process is signalled only when its PID, start time AND command line match what this script recorded
# when it started it (loadtest/ltlib/procs.py, registry loadtest/.procs.json). PID files left behind by a crash or a
# reboot (the PIDs are gone, or reused by unrelated programs) are reported as stale and removed - never killed.
#
# start: refuses (unless --force) when the shared machine is busy (loadtest/ltlib/preflight.py: load average > 2, a
# pytest run, > 30 PostgreSQL connections of other databases) or when the run folder already holds files; checks the
# connection budget (workers x (pool + overflow + 1 LISTEN)) against the database's CONNECTION LIMIT, 80 % of the
# cluster's max_connections (the app's own budget rule) and what the other databases use right now; loads the
# environment from loadtest/ltenv.py (env vars beat .env, which is never written; the server's own generated secrets
# come from loadtest/.secrets.env), validates it with the app's own settings loader (ENVIRONMENT=production refuses
# unsafe settings) and dumps the effective settings (no secrets) to <run>/server_config.json; then
#   .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 12000 --workers 4 --no-server-header
#       --proxy-headers --forwarded-allow-ips 127.0.0.1 --backlog 2048   (= scripts/manage.py run --workers 4)
# in its own session (it outlives this shell); log: loadtest/results/<run>/server.log.
set -Eeuo pipefail

LT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$LT/.." && pwd)"
PY="$ROOT/.venv/bin/python"            # the app's interpreter
LPY="$ROOT/.venv-loadtest/bin/python"  # Locust venv (psutil) for the monitor, preflight and the SMTP sink
PORT=12000
HOST=127.0.0.1
SERVER_PID_FILE="$LT/.server.pid"
MONITOR_PID_FILE="$LT/.monitor.pid"
SMTP_PID_FILE="$LT/.smtp.pid"
CURRENT_RUN_FILE="$LT/.current_run"

die() { printf '[lt-server] error: %s\n' "$*" >&2; exit 1; }
say() { printf '[lt-server] %s\n' "$*"; }

alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
procs() { "$LPY" "$LT/ltlib/procs.py" "$@"; }  # PID registry: identity = PID + start time + command line
port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

health() {  # through the "proxy" headers: FORCE_HTTPS would answer plain http with a 308
  curl -s -o /dev/null -m 3 -w '%{http_code}' -H 'X-Forwarded-Proto: https' -H 'X-Forwarded-For: 198.18.255.250' \
    -H 'User-Agent: ironpulse-loadtest-launcher/1.0' "http://$HOST:$PORT/healthz" || true
}

spawn_detached() {  # spawn_detached LOGFILE CMD... -> prints the PID; new session, survives this shell
  local log="$1"; shift
  "$PY" - "$log" "$@" <<'PYEOF'
import os, subprocess, sys
log, cmd = sys.argv[1], sys.argv[2:]
out = open(log, "ab", buffering=0)
p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
                     cwd=os.environ.get("LT_CWD") or None, env=os.environ.copy())
print(p.pid)
PYEOF
}

psql_su() {  # psql_su SQL -> unaligned rows (superuser, database "postgres"; read-only queries)
  PGPASSWORD="$(cat "${IRONPULSE_HOME:-$HOME/.ironpulse}/pg_superuser_password")" PGCONNECT_TIMEOUT=3 \
    "${IRONPULSE_HOME:-$HOME/.ironpulse}/env/bin/psql" -h localhost -p 5432 -U postgres -d postgres -X -A -t -F '|' -c "$1"
}

cmd_start() {
  local run="" ai="${LT_AI:-simulated}" smtp="spool" workers=4 monitor=1 force=0 backlog=2048
  while [ $# -gt 0 ]; do
    case "$1" in
      --run) run="$2"; shift 2 ;;
      --ai) ai="$2"; shift 2 ;;
      --smtp) smtp="$2"; shift 2 ;;
      --workers) workers="$2"; shift 2 ;;
      --backlog) backlog="$2"; shift 2 ;;
      --no-monitor) monitor=0; shift ;;
      --force) force=1; shift ;;
      *) die "unknown option $1" ;;
    esac
  done
  [ -x "$PY" ] || die "no project virtualenv at $PY"
  [ -x "$LPY" ] || die "no .venv-loadtest (loadtest/setup_venv.sh) - the preflight, monitor and SMTP sink need it"
  local sweep_rc=0
  procs sweep || sweep_rc=$?  # removes PID files / records of processes that are gone or are not ours any more
  [ "$sweep_rc" -eq 3 ] && die "the load-test server (or its monitor) is already running - loadtest/run_server.sh stop first"
  port_busy "$PORT" && die "port $PORT is already in use by another program"
  [ -f "$LT/accounts.json" ] || die "no loadtest/accounts.json - seed first: .venv/bin/python loadtest/seed_loadtest.py"
  [ -n "$run" ] || run="$(date +%Y%m%d-%H%M%S)"
  [[ "$run" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]] || die "run name '$run': letters, digits, '_', '.', '-' only (max 64)"
  local rd="$LT/results/$run"
  if [ -d "$rd" ] && [ -n "$(ls -A "$rd" 2>/dev/null)" ]; then
    [ "$force" -eq 1 ] || die "$rd already holds files - pick a new --run name (add seconds: \$(date +%Y%m%d-%H%M%S)) or pass --force"
    say "WARNING: reusing the non-empty run folder $rd (--force) - analyze.py drops rows from before this start"
  fi
  mkdir -p "$rd"

  # ---- is the shared machine quiet? (the other teams' pytest runs / dev work would show up as app limits)
  local pf_rc=0
  "$LPY" "$LT/ltlib/preflight.py" --json > "$rd/preflight_server.json" || pf_rc=$?
  "$LPY" - "$rd/preflight_server.json" <<'PYEOF'
import json, sys
r = json.load(open(sys.argv[1]))
print(f"[lt-server] preflight: load {r['load']} on {r['cpus']} CPUs, PostgreSQL connections of other databases "
      f"{r['pg_connections_other']}, pytest processes {len(r['pytest'])}")
for x in r["reasons"]:
    print(f"[lt-server] preflight BUSY: {x}")
PYEOF
  if [ "$pf_rc" -ne 0 ]; then
    [ "$force" -eq 1 ] || { rm -f "$rd/preflight_server.json"; rmdir "$rd" 2>/dev/null || true; die "the machine is busy (see above) - wait for the other teams, or pass --force (results will be marked)"; }
    say "WARNING: starting on a busy machine (--force) - the report marks the run as contaminated"
  fi
  echo "$run" > "$CURRENT_RUN_FILE"

  # ~2 sockets per simulated user + DB/Redis connections: macOS shells often start with a soft limit of 256 files
  if [ "$(ulimit -Sn)" != "unlimited" ] && [ "$(ulimit -Sn)" -lt 10240 ]; then ulimit -Sn 10240 2>/dev/null || say "could not raise the open-files limit ($(ulimit -Sn))"; fi

  # ---- the load-test environment (overrides .env; secrets stay in the process environment only)
  local envlines
  envlines="$("$PY" "$LT/ltenv.py" server-env --ai "$ai" --smtp "$smtp")" || die "loadtest/ltenv.py could not build the server environment (see above)"
  eval "$envlines"
  unset envlines
  # the worker count for the app's startup connection-budget check (app/db.py configured_workers: IRONPULSE_WORKERS,
  # then uvicorn's --workers, then WEB_CONCURRENCY) - both are set explicitly
  export PYTHONUNBUFFERED=1 IRONPULSE_WORKERS="$workers" WEB_CONCURRENCY="$workers"

  # ---- connection budget: workers x (pool + overflow + 1 LISTEN) must fit the database's CONNECTION LIMIT, at most
  # 80 % of the cluster's slots (max_connections - reserved; the app's own rule, app/db.py BUDGET_SHARE) and - right
  # now - what the other databases leave free (the server is shared with the dev servers and the other teams' tests)
  local need limit="" maxc="" reserved="" others="" share free
  need=$(( workers * (DB_POOL_SIZE + DB_MAX_OVERFLOW + 1) ))
  IFS='|' read -r limit maxc reserved others <<< "$(psql_su "SELECT (SELECT datconnlimit FROM pg_database WHERE datname = 'ironpulse_loadtest'), current_setting('max_connections')::int, current_setting('superuser_reserved_connections')::int + coalesce(current_setting('reserved_connections', true), '0')::int, (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend' AND datname IS DISTINCT FROM 'ironpulse_loadtest')" 2>/dev/null || true)"
  [ -n "$maxc" ] || die "cannot read the PostgreSQL limits (psql as superuser) - is the runtime up? scripts/runtime_postgres.sh status"
  if [ -n "$limit" ] && [ "$limit" -ge 0 ] && [ "$need" -gt "$limit" ]; then
    die "the pools need $need connections ($workers x ($DB_POOL_SIZE + $DB_MAX_OVERFLOW + 1)) but ironpulse_loadtest allows $limit - re-seed (sets CONNECTION LIMIT ${LT_DB_CONN_LIMIT:-70}) or lower LT_DB_MAX_OVERFLOW"
  fi
  share=$(( (maxc - reserved) * 8 / 10 )); free=$(( maxc - reserved - others ))
  if [ "$need" -gt "$share" ]; then
    die "the pools need $need connections but max_connections $maxc - $reserved reserved allows the app at most $share (80 %) - lower LT_DB_MAX_OVERFLOW / --workers"
  fi
  if [ "$need" -gt "$free" ]; then
    [ "$force" -eq 1 ] || die "the pools need $need connections but only $free are free right now ($others used by other databases of max_connections $maxc - $reserved reserved) - wait for the other teams or pass --force"
    say "WARNING: only $free PostgreSQL connections are free right now for a budget of $need (--force)"
  fi
  say "connection budget: $workers x ($DB_POOL_SIZE + $DB_MAX_OVERFLOW + 1 LISTEN) = $need of CONNECTION LIMIT $limit; cluster max_connections $maxc ($reserved reserved, $others in use by other databases)"

  # ---- validate the settings with the app's own loader and record the effective values (no secrets)
  LT_ROOT="$ROOT" "$PY" - "$rd/server_config.json" "$ai" "$workers" "$smtp" "$backlog" "$force" "$need" "${limit:-}" <<'PYEOF' || die "the app refuses these settings (lines above) - nothing was started"
import json, os, platform, re, subprocess, sys, time
sys.path.insert(0, os.environ["LT_ROOT"])
sys.path.insert(0, os.path.join(os.environ["LT_ROOT"], "loadtest"))
out, ai, workers, smtp, backlog, force, need, limit = sys.argv[1:9]
import ltenv
from app.config import ConfigError, build_settings, startup_warnings
try:
    s = build_settings()
except ConfigError as exc:
    exc.report()
    sys.exit(3)
secret = re.compile(r"key|secret|password|pepper|token|_url$|url$|dsn", re.I)
effective = {k: v for k, v in s.model_dump(mode="json").items() if not secret.search(k)}
pinned, source = ltenv.app_defaults()
overrides = {k: os.environ[f"LT_{k}"] for k in pinned if os.environ.get(f"LT_{k}") is not None}
def sh(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""
pg = {}
pw = open(os.path.expanduser("~/.ironpulse/pg_superuser_password")).read().strip()
q = ("SELECT name, setting, coalesce(unit, '') FROM pg_settings WHERE name IN ('max_connections', 'shared_buffers', "
     "'work_mem', 'effective_cache_size', 'jit', 'random_page_cost', 'log_min_duration_statement', 'max_wal_size', "
     "'synchronous_commit', 'server_version')")
env = dict(os.environ, PGPASSWORD=pw, PGCONNECT_TIMEOUT="3")
for db in ("postgres", ltenv.DB_NAME):
    rows = subprocess.run([str(ltenv.PSQL), "-h", "localhost", "-U", "postgres", "-d", db, "-X", "-A", "-t", "-F", "|",
                           "-c", q], env=env, capture_output=True, text=True, timeout=5).stdout.splitlines()
    pg["cluster" if db == "postgres" else "ironpulse_loadtest"] = {r.split("|")[0]: (r.split("|")[1] + r.split("|")[2]) for r in rows if "|" in r}
import uvicorn
cfg = {
    "written_at": time.time(), "ai_mode": ai, "smtp_mode": smtp, "workers": int(workers), "backlog": int(backlog),
    "forced": force == "1", "database": ltenv.DB_NAME, "redis_db": ltenv.REDIS_DB,
    "connection_budget": {"needed": int(need), "database_connection_limit": int(limit) if limit else None,
                          "formula": f"{workers} workers x (pool {s.db_pool_size} + overflow {s.db_max_overflow} + 1 LISTEN)",
                          "web_concurrency": os.environ.get("WEB_CONCURRENCY"),
                          "ironpulse_workers": os.environ.get("IRONPULSE_WORKERS")},
    "pinned_source": source, "pinned": pinned, "lt_overrides": overrides,
    "DB_POOL_SIZE": s.db_pool_size, "DB_MAX_OVERFLOW": s.db_max_overflow, "ENVIRONMENT": s.environment,
    "startup_warnings": startup_warnings(s),
    "effective_settings": effective,
    "keys_set": {"anthropic": bool(s.anthropic_api_key), "geoapify": bool(s.geoapify_api_key),
                 "cashfree": s.cashfree_enabled, "smtp_host": s.smtp_host or "", "ai_gateway": bool(s.ai_gateway_base_url),
                 "ai_docker": bool(s.ai_docker_base_url)},
    "stand_ins": {
        "map_tiles": "local transparent PNG (no GEOAPIFY_API_KEY, MAP_OSM_FALLBACK=false): no upstream fetch, no tile cache churn",
        "email": ("spooled to loadtest/var/mail (no SMTP transaction)" if smtp == "spool" else
                  "real SMTP delivery into loadtest/smtp_sink.py (127.0.0.1:2525, 100-300 ms per mail, discarded)"),
        "payments": "Cashfree TEST MODE (local test gateway page)",
        "ai": {"simulated": "app/ai/simulated.py provider (in-process, realistic latency, guard + queue real)",
               "off": "no provider: deterministic fallbacks (guard/queue not exercised)",
               "gateway": "LAN Gemma gateway from .env"}.get(ai, ai),
    },
    "platform": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                 "os": f"{platform.system()} {platform.release()} ({platform.machine()})", "mac_ver": platform.mac_ver()[0],
                 "uvicorn": uvicorn.__version__, "cpus": os.cpu_count(),
                 "memory_gb": round(int(sh(["sysctl", "-n", "hw.memsize"]) or 0) / 2**30, 1),
                 "somaxconn": sh(["sysctl", "-n", "kern.ipc.somaxconn"]),
                 "ephemeral_ports": f"{sh(['sysctl', '-n', 'net.inet.ip.portrange.first'])}-{sh(['sysctl', '-n', 'net.inet.ip.portrange.last'])}",
                 "tcp_msl_ms": sh(["sysctl", "-n", "net.inet.tcp.msl"])},
    "pg_settings": pg,
}
json.dump(cfg, open(out, "w"), indent=2, default=str)
for w in cfg["startup_warnings"]:
    print(f"[lt-server] app startup warning: {w[:220]}")
PYEOF

  if [ "$smtp" = "sink" ]; then
    port_busy 2525 && die "port 2525 (SMTP sink) is already in use"
    local spid
    spid="$(LT_CWD="$ROOT" spawn_detached "$rd/smtp_sink.log" "$LPY" "$LT/smtp_sink.py" --port 2525)"
    procs record smtp "$spid" || echo "$spid" > "$SMTP_PID_FILE"
    say "SMTP sink PID $spid on 127.0.0.1:2525 (mail is discarded)"
  fi

  say "starting $workers workers on http://$HOST:$PORT (run $run, AI $ai, mail $smtp) - log: $rd/server.log"
  local pid t_start
  t_start="$(date +%s)"
  pid="$(LT_CWD="$ROOT" spawn_detached "$rd/server.log" "$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT" \
          --workers "$workers" --no-server-header --proxy-headers --forwarded-allow-ips 127.0.0.1 --backlog "$backlog" \
          --log-level info)"
  procs record server "$pid" || echo "$pid" > "$SERVER_PID_FILE"
  echo "$pid" > "$rd/server.pid"

  local code="" i
  for i in $(seq 1 120); do
    code="$(health)"
    [ "$code" = "200" ] && break
    alive "$pid" || { tail -40 "$rd/server.log" >&2; procs sweep >/dev/null || true; die "the server exited during start-up"; }
    sleep 1
  done
  [ "$code" = "200" ] || { tail -40 "$rd/server.log" >&2; die "no healthy answer from /healthz after 120 s (last: $code)"; }
  local t_healthy
  t_healthy="$(date +%s)"

  # all workers up? (uvicorn starts them in parallel; each logs "Application startup complete")
  for i in $(seq 1 60); do
    [ "$(grep -c 'Application startup complete' "$rd/server.log" || true)" -ge "$workers" ] && break
    sleep 1
  done
  local kids
  kids="$(procs record-children workers "$pid" || true)"
  echo "$kids" > "$rd/worker.pids"
  printf '{"started_at": %s, "healthy_at": %s, "workers_up_at": %s, "master_pid": %s}\n' \
    "$t_start" "$t_healthy" "$(date +%s)" "$pid" > "$rd/server_ready.json"
  say "server ready: master PID $pid, worker PIDs: ${kids:-?}"

  if [ "$monitor" -eq 1 ]; then
    local mpid
    mpid="$(LT_CWD="$ROOT" spawn_detached "$rd/monitor.out" "$LPY" "$LT/monitor.py" --run-dir "$rd" --server-pid "$pid")"
    sleep 0.5
    procs record monitor "$mpid" || echo "$mpid" > "$MONITOR_PID_FILE"
    echo "$mpid" > "$rd/monitor.pid"
    say "monitor PID $mpid -> $rd/monitor.csv (every 5 s)"
  fi
  say "background jobs start 45-300 s after this (initial delays): for clean steps start Locust after 'loadtest/run_server.sh warmup'"
  say "stop with: loadtest/run_server.sh stop"
}

cmd_warmup() {  # wait until N seconds after the server became healthy (default 330: past every periodic job's delay)
  local secs="${1:-330}" run rd healthy now left
  [ -f "$CURRENT_RUN_FILE" ] || die "no current run"
  run="$(cat "$CURRENT_RUN_FILE")"; rd="$LT/results/$run"
  [ -f "$rd/server_ready.json" ] || die "no $rd/server_ready.json - is the server running?"
  healthy="$("$LPY" -c 'import json,sys; print(int(json.load(open(sys.argv[1]))["healthy_at"]))' "$rd/server_ready.json")"
  while :; do
    now="$(date +%s)"; left=$(( healthy + secs - now ))
    [ "$left" -le 0 ] && break
    procs check server >/dev/null || die "the server is not running"
    say "warm-up: ${left}s left (background jobs' initial delays run out 300 s after start)"
    sleep "$(( left < 30 ? left : 30 ))"
  done
  say "warm-up done: $(( $(date +%s) - healthy ))s after the server became healthy"
}

cmd_stop() {  # only processes whose PID + start time + command line match what cmd_start recorded
  local rd=""
  [ -f "$CURRENT_RUN_FILE" ] && rd="$LT/results/$(cat "$CURRENT_RUN_FILE")"
  procs stop monitor --timeout 15
  local was_running=0
  procs check server >/dev/null && was_running=1
  procs stop server --timeout 30
  procs stop workers  # normally gone with the master; a worker left behind is ours only if its identity matches
  procs stop smtp --timeout 10
  if [ "$was_running" -eq 1 ] && [ -n "$rd" ] && [ -d "$rd" ]; then date +%s > "$rd/server_stopped_at"; fi
  if port_busy "$PORT"; then say "WARNING: something still listens on $PORT"; else say "port $PORT is free"; fi
}

cmd_status() {
  procs status
  if procs check server >/dev/null; then say "/healthz: $(health)"; fi
  [ -f "$CURRENT_RUN_FILE" ] && say "current run: $LT/results/$(cat "$CURRENT_RUN_FILE")"
  return 0
}

case "${1:-}" in
  start) shift; cmd_start "$@" ;;
  warmup) shift; cmd_warmup "$@" ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
