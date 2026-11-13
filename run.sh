#!/usr/bin/env bash
# Start IronPulse on macOS / Linux (after ./setup.sh).
#
#   ./run.sh                    start the app (host/port from .env)
#   ./run.sh --reload           development: restart automatically when code changes
#   ./run.sh --workers 4        several worker processes (uvicorn; live events fan out via PostgreSQL)
#   ./run.sh --lan              use it on your phone: HTTPS on this computer's LAN address (port from .env, 11000)
#   ./run.sh --host 0.0.0.0     plain HTTP for other devices (prefer --lan; add the IP to ALLOWED_HOSTS)
#   ./run.sh --no-services      don't start PostgreSQL/Redis first
#   ./run.sh --no-ai-docker     don't start the local AI fallback (Ollama)
#   ./run.sh --dry-run          print what would be started and run; start nothing
#
# Before starting the app it makes sure the database (and Redis) of your setup mode are running:
# runtime -> scripts/runtime_postgres.sh / runtime_redis.sh, docker -> docker compose up -d db redis.
# The local AI fallback (Gemma in Ollama, AI_PROVIDER_ORDER 'docker') is started in the background when Docker or
# Ollama is installed - it never delays the start (scripts/ai_fallback.py; log: var/logs/ai-fallback.log).
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
VENV_PY="$ROOT/.venv/bin/python"
MANAGE="$ROOT/scripts/manage.py"

OPT_RELOAD=0
OPT_LAN=0
WORKERS=1
HOST_ARG=""
PORT_ARG=""
SERVICES=1
AI_DOCKER=1
DRY_RUN=0

usage() {
  cat <<'EOF'
Start IronPulse (macOS / Linux).

Usage: ./run.sh [--lan] [--reload] [--workers N] [--host HOST] [--port PORT] [--no-services] [--no-ai-docker] [--dry-run]
  --lan           serve your phone / other devices on this network over HTTPS (LAN address + <name>.local;
                  install var/tls/ironpulse-lan-ca.crt on the phone once - see README "Use it on your phone")
  --reload        restart automatically when code changes (development)
  --workers N     run N worker processes (cannot be combined with --reload)
  --host HOST     override HOST from .env (plain HTTP - for phones use --lan)
  --port PORT     override PORT from .env
  --no-services   don't start the runtime/Docker database and Redis first
  --no-ai-docker  don't start the local AI fallback (Gemma in Ollama/Docker)
  --dry-run       print what would be started and run, start nothing
EOF
}

die() { printf '[run] error: %s\n' "$*" >&2; exit 1; }
need_value() {
  if [ $# -lt 2 ] || [ -z "$2" ]; then die "$1 needs a value"; fi
}
# run a command - or, with --dry-run, only say what would run
step() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[run] would run:'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}
# the docker CLI: PATH, else where Docker Desktop installs it (a shell without ~/.zprofile does not have it on PATH)
find_docker() {
  if command -v docker >/dev/null 2>&1; then command -v docker; return 0; fi
  local candidate
  for candidate in "$HOME/.docker/bin/docker" /usr/local/bin/docker /opt/homebrew/bin/docker \
                   /Applications/Docker.app/Contents/Resources/bin/docker; do
    if [ -x "$candidate" ]; then printf '%s\n' "$candidate"; return 0; fi
  done
  return 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --reload) OPT_RELOAD=1; shift ;;
    --lan) OPT_LAN=1; shift ;;
    --workers) need_value "$@"; WORKERS="$2"; shift 2 ;;
    --workers=*) WORKERS="${1#*=}"; shift ;;
    --host) need_value "$@"; HOST_ARG="$2"; shift 2 ;;
    --host=*) HOST_ARG="${1#*=}"; shift ;;
    --port) need_value "$@"; PORT_ARG="$2"; shift 2 ;;
    --port=*) PORT_ARG="${1#*=}"; shift ;;
    --no-services) SERVICES=0; shift ;;
    --no-ai-docker) AI_DOCKER=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done

case "$WORKERS" in
  ''|*[!0-9]*) die "--workers needs a whole number" ;;
esac
if [ "$WORKERS" -lt 1 ]; then die "--workers must be 1 or more"; fi
if [ "$OPT_RELOAD" -eq 1 ] && [ "$WORKERS" -gt 1 ]; then die "--reload cannot be combined with --workers"; fi
if [ "$OPT_LAN" -eq 1 ] && [ "$OPT_RELOAD" -eq 1 ]; then die "--lan cannot be combined with --reload (LAN mode never auto-reloads)"; fi
if [ "$OPT_LAN" -eq 1 ] && [ -n "$HOST_ARG" ]; then die "--lan binds every interface itself (0.0.0.0) - leave out --host"; fi
if [ -n "$PORT_ARG" ]; then
  case "$PORT_ARG" in
    *[!0-9]*) die "--port needs a number" ;;
  esac
fi

[ -x "$VENV_PY" ] || die "no virtualenv yet - run ./setup.sh first"
[ -f "$ROOT/.env" ] || die "no .env yet - run ./setup.sh first (or: .venv/bin/python scripts/make_env.py)"

# activate the virtualenv so tools started by the app resolve to it
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"

if [ "$SERVICES" -eq 1 ]; then
  mode="$("$VENV_PY" "$MANAGE" env get IRONPULSE_DB_MODE 2>/dev/null || true)"
  redis_url="$("$VENV_PY" "$MANAGE" env get REDIS_URL 2>/dev/null || true)"
  case "$mode" in
    runtime)
      step "$ROOT/scripts/runtime_postgres.sh" start
      if [ -n "$redis_url" ]; then
        step "$ROOT/scripts/runtime_redis.sh" start || echo "[run] Redis did not start - sessions fall back to PostgreSQL" >&2
      fi
      ;;
    docker)
      docker_cli="$(find_docker || true)"
      if [ -n "$docker_cli" ] && "$docker_cli" info >/dev/null 2>&1; then
        PATH="$(dirname "$docker_cli"):$PATH"  # Docker Desktop's credential helper lives next to the CLI
        export PATH
        step "$docker_cli" compose --env-file "$ROOT/.env" -f "$ROOT/docker/compose.yml" up -d db redis
      else
        echo "[run] Docker is not running - start Docker Desktop (the database runs in Docker in this setup)" >&2
      fi
      ;;
    *) ;;  # native / existing: the database is managed outside IronPulse
  esac
fi

# the local AI fallback: decided in Python (scripts/ai_fallback.py), slow work in the background, one line of output
if [ "$AI_DOCKER" -eq 1 ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    "$VENV_PY" "$MANAGE" ai-fallback --dry-run || true
  else
    "$VENV_PY" "$MANAGE" ai-fallback || true
  fi
fi

if [ "$OPT_LAN" -eq 1 ]; then
  lan_args=(--workers "$WORKERS")
  if [ -n "$PORT_ARG" ]; then lan_args+=(--port "$PORT_ARG"); fi
  if [ "$DRY_RUN" -eq 1 ]; then
    step "$VENV_PY" "$ROOT/scripts/lan.py" "${lan_args[@]}"
    exec "$VENV_PY" "$ROOT/scripts/lan.py" "${lan_args[@]}" --dry-run
  fi
  exec "$VENV_PY" "$ROOT/scripts/lan.py" "${lan_args[@]}"
fi

if [ -n "$HOST_ARG" ]; then export HOST="$HOST_ARG"; fi
if [ -n "$PORT_ARG" ]; then export PORT="$PORT_ARG"; fi

if [ "$WORKERS" -gt 1 ]; then
  if [ "$DRY_RUN" -eq 1 ]; then step "$VENV_PY" "$MANAGE" run --workers "$WORKERS"; exit 0; fi
  exec "$VENV_PY" "$MANAGE" run --workers "$WORKERS"
fi
if [ "$OPT_RELOAD" -eq 1 ]; then
  export RELOAD=true
fi
if [ "$DRY_RUN" -eq 1 ]; then step "$VENV_PY" -m app; exit 0; fi
exec "$VENV_PY" -m app
