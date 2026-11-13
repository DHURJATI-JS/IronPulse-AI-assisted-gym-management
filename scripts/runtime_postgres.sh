#!/usr/bin/env bash
# User-space PostgreSQL + pgvector for IronPulse (macOS / Linux) - no sudo, no Docker.
# Installed by "./setup.sh --db runtime" with micromamba (conda-forge) under ~/.ironpulse:
#   binaries  ~/.ironpulse/env/bin        data  ~/.ironpulse/pgdata       log  ~/.ironpulse/postgres.log
#   superuser "postgres" (password file ~/.ironpulse/pg_superuser_password), port 5432, socket dir /tmp
#
# Usage: scripts/runtime_postgres.sh start|stop|restart|status|init|psql [psql args]|logs
#   psql examples:  scripts/runtime_postgres.sh psql -d ironpulse -c "select count(*) from users"
# Environment overrides: IRONPULSE_HOME, IRONPULSE_PGDATA, IRONPULSE_PGPORT, IRONPULSE_PGSOCKET
set -euo pipefail

IRONPULSE_HOME="${IRONPULSE_HOME:-$HOME/.ironpulse}"
BIN="$IRONPULSE_HOME/env/bin"
PGDATA_DIR="${IRONPULSE_PGDATA:-$IRONPULSE_HOME/pgdata}"
PG_PORT="${IRONPULSE_PGPORT:-5432}"
SOCKET_DIR="${IRONPULSE_PGSOCKET:-/tmp}"
LOG_FILE="$IRONPULSE_HOME/postgres.log"
SUPER_PW_FILE="$IRONPULSE_HOME/pg_superuser_password"

say() { printf '[runtime-postgres] %s\n' "$*"; }
die() { printf '[runtime-postgres] error: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
User-space PostgreSQL (+ pgvector) for IronPulse - installed by ./setup.sh --db runtime.

Usage: scripts/runtime_postgres.sh <command>
  start            start the server (initialises the data folder on first use)
  stop             stop the server (fast shutdown)
  restart          stop + start
  status           show whether the server is running and accepting connections
  init             create the data folder (superuser "postgres", scram-sha-256 auth) - done by start
  psql [args]      open psql as the superuser (default database: postgres)
  logs             follow the server log

Files: ~/.ironpulse/env/bin (binaries), ~/.ironpulse/pgdata (data), ~/.ironpulse/postgres.log (log)
Override with IRONPULSE_HOME, IRONPULSE_PGDATA, IRONPULSE_PGPORT (5432), IRONPULSE_PGSOCKET (/tmp).
EOF
}

need_bin() {
  [ -x "$BIN/$1" ] || die "$BIN/$1 not found - install the runtime first: ./setup.sh --db runtime"
}

gen_password() {
  local pw
  pw="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom 2>/dev/null | head -c 32 || true)"
  [ "${#pw}" -ge 24 ] || die "could not generate a random password"
  printf '%s' "$pw"
}

is_running() {
  [ -f "$PGDATA_DIR/PG_VERSION" ] && "$BIN/pg_ctl" -D "$PGDATA_DIR" status >/dev/null 2>&1
}

cmd_init() {
  need_bin initdb
  mkdir -p "$IRONPULSE_HOME"
  chmod 700 "$IRONPULSE_HOME" 2>/dev/null || true
  if [ ! -s "$SUPER_PW_FILE" ]; then
    (umask 077 && gen_password > "$SUPER_PW_FILE")
    say "generated the superuser password ($SUPER_PW_FILE)"
  fi
  if [ -f "$PGDATA_DIR/PG_VERSION" ]; then
    say "data folder already initialised: $PGDATA_DIR (PostgreSQL $(cat "$PGDATA_DIR/PG_VERSION"))"
    return 0
  fi
  say "initialising $PGDATA_DIR ..."
  if ! "$BIN/initdb" -D "$PGDATA_DIR" -U postgres --pwfile="$SUPER_PW_FILE" --auth=scram-sha-256 \
      -E UTF8 --locale=C >"$IRONPULSE_HOME/initdb.log" 2>&1; then
    cat "$IRONPULSE_HOME/initdb.log" >&2
    die "initdb failed (log: $IRONPULSE_HOME/initdb.log)"
  fi
  chmod 600 "$IRONPULSE_HOME/initdb.log" 2>/dev/null || true
  say "initialised (superuser: postgres)"
}

cmd_start() {
  need_bin pg_ctl
  [ -f "$PGDATA_DIR/PG_VERSION" ] || cmd_init
  if is_running; then
    say "already running on port $PG_PORT"
    return 0
  fi
  say "starting on localhost:$PG_PORT (log: $LOG_FILE) ..."
  touch "$LOG_FILE" && chmod 600 "$LOG_FILE" 2>/dev/null || true
  "$BIN/pg_ctl" -D "$PGDATA_DIR" -l "$LOG_FILE" -w -t 60 \
    -o "-p $PG_PORT -c listen_addresses=localhost -k $SOCKET_DIR" start >/dev/null \
    || { tail -n 20 "$LOG_FILE" >&2 || true; die "PostgreSQL did not start - see $LOG_FILE"; }
  say "running (PostgreSQL $(cat "$PGDATA_DIR/PG_VERSION"), port $PG_PORT)"
}

cmd_stop() {
  need_bin pg_ctl
  if ! is_running; then
    say "not running"
    return 0
  fi
  "$BIN/pg_ctl" -D "$PGDATA_DIR" -m fast -w -t 60 stop >/dev/null
  say "stopped"
}

cmd_status() {
  if [ ! -x "$BIN/pg_ctl" ]; then
    say "not installed ($BIN) - run ./setup.sh --db runtime"
    return 3
  fi
  if [ ! -f "$PGDATA_DIR/PG_VERSION" ]; then
    say "installed, data folder not initialised yet ($PGDATA_DIR) - run: scripts/runtime_postgres.sh start"
    return 3
  fi
  if is_running; then
    if "$BIN/pg_isready" -q -h localhost -p "$PG_PORT" 2>/dev/null; then
      say "running and accepting connections on localhost:$PG_PORT (PostgreSQL $(cat "$PGDATA_DIR/PG_VERSION"))"
    else
      say "running, but not accepting connections on localhost:$PG_PORT yet"
    fi
    return 0
  fi
  say "stopped - start it with: scripts/runtime_postgres.sh start"
  return 3
}

cmd_psql() {
  need_bin psql
  [ -s "$SUPER_PW_FILE" ] || die "missing $SUPER_PW_FILE"
  PGPASSWORD="$(cat "$SUPER_PW_FILE")" exec "$BIN/psql" -h localhost -p "$PG_PORT" -U postgres -d postgres "$@"
}

cmd_logs() {
  [ -f "$LOG_FILE" ] || die "no log yet ($LOG_FILE)"
  exec tail -n 100 -f "$LOG_FILE"
}

command="${1:-}"
[ $# -gt 0 ] && shift
case "$command" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_stop; cmd_start ;;
  status) cmd_status ;;
  init) cmd_init ;;
  psql) cmd_psql "$@" ;;
  logs) cmd_logs ;;
  -h|--help|help) usage ;;
  "") usage; exit 2 ;;
  *) usage >&2; die "unknown command: $command" ;;
esac
