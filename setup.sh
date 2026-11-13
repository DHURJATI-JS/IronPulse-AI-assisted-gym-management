#!/usr/bin/env bash
# IronPulse setup for macOS and Linux (bash 3.2+). Safe to run again: finished steps are skipped.
#
#   ./setup.sh                      user-space runtime: PostgreSQL + pgvector + Redis + Tesseract under
#                                   ~/.ironpulse via micromamba (conda-forge) - no sudo, no Docker (default)
#   ./setup.sh --db docker          PostgreSQL 17 + pgvector and Redis in Docker (docker/compose.yml)
#   ./setup.sh --db native          system packages: Homebrew / apt / dnf / pacman / zypper (may ask for sudo)
#   ./setup.sh --db existing --database-url postgresql://user:pass@host:5432/ironpulse
#   ./setup.sh --with-ai-docker     + the Gemma failsafe in Docker (Ollama, gemma3:4b)
#   ./setup.sh --with-voice         + Kokoro text-to-speech in Docker for the live voice coach
#
# Windows: use setup.ps1 (PowerShell). Full guide: README.md
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

IRONPULSE_HOME="${IRONPULSE_HOME:-$HOME/.ironpulse}"
RUNTIME_PG_MAJOR="${IRONPULSE_PG_MAJOR:-18}"
VENV="$ROOT/.venv"
VENV_PY="$VENV/bin/python"
MANAGE="$ROOT/scripts/manage.py"
COMPOSE_FILE="$ROOT/docker/compose.yml"

DB_MODE=""
WITH_AI_DOCKER=0
WITH_VOICE=0
SKIP_ASSETS=0
SKIP_VIDEOS=0
SKIP_SEED=0
SKIP_PIP=0
WITH_DEV=1
DRY_RUN=0
DATABASE_URL_ARG=""
ADMIN_URL_ARG=""
PYTHON_ARG="${PYTHON:-}"
PY=""
TESSERACT_PATH=""
NATIVE_ADMIN_URL=""

# ----------------------------------------------------------------------------- output helpers
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; CYAN=""; RESET=""
fi
step() { printf '\n%s==> %s%s\n' "$BOLD$CYAN" "$*" "$RESET"; }
info() { printf '    %s\n' "$*"; }
ok() { printf '    %s[ok]%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '    %s[!]%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die() { printf '\n%sERROR:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
redact() { sed -E 's#(://[^:/@ ]+):[^@ ]+@#\1:***@#g'; }
run() {
  printf '    %s$ %s%s\n' "$CYAN" "$(printf '%s ' "$@" | redact)" "$RESET"
  "$@"
}
on_error() {
  printf '\n%sSetup stopped (line %s).%s Fix the problem shown above and run ./setup.sh again - finished steps are skipped.\n' \
    "$RED" "$1" "$RESET" >&2
}
trap 'on_error $LINENO' ERR

usage() {
  cat <<'EOF'
IronPulse setup for macOS and Linux.

Usage: ./setup.sh [options]

Database (pick one; the choice is remembered in .env as IRONPULSE_DB_MODE):
  --db runtime        (default) user-space PostgreSQL 18 + pgvector + Redis + Tesseract under ~/.ironpulse,
                      installed with micromamba from conda-forge - no sudo, no Docker
  --db docker         PostgreSQL 17 + pgvector (port 5433) and Redis (port 6380) in Docker
  --db native         system packages: Homebrew (macOS) or apt / dnf / pacman / zypper (Linux, uses sudo)
  --db existing       your own server: --database-url URL [--admin-url SUPERUSER_URL]

Optional services (Docker):
  --with-ai-docker    Gemma failsafe: Ollama + gemma3:4b (~3.3 GB download) on port 11434
  --with-voice        Kokoro text-to-speech for the live voice coach on port 8880

Other options:
  --database-url URL  database for --db existing
  --admin-url URL     superuser URL used once to create the role, database and extensions
  --python PATH       Python 3.10+ interpreter used to create .venv
  --skip-assets       don't build Tailwind CSS / the JS bundle
  --skip-videos       don't generate the workout videos
  --skip-seed         don't load the demo data
  --skip-pip          don't run pip (packages already installed / offline re-run)
  --no-dev            don't install the test tools (pytest)
  --dry-run           print the plan and exit
  -h, --help          this help
EOF
}

need_value() {
  if [ $# -lt 2 ] || [ -z "$2" ]; then die "$1 needs a value (see ./setup.sh --help)"; fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --db) need_value "$@"; DB_MODE="$2"; shift 2 ;;
    --db=*) DB_MODE="${1#*=}"; shift ;;
    --with-ai-docker) WITH_AI_DOCKER=1; shift ;;
    --with-voice) WITH_VOICE=1; shift ;;
    --database-url) need_value "$@"; DATABASE_URL_ARG="$2"; shift 2 ;;
    --database-url=*) DATABASE_URL_ARG="${1#*=}"; shift ;;
    --admin-url) need_value "$@"; ADMIN_URL_ARG="$2"; shift 2 ;;
    --admin-url=*) ADMIN_URL_ARG="${1#*=}"; shift ;;
    --python) need_value "$@"; PYTHON_ARG="$2"; shift 2 ;;
    --python=*) PYTHON_ARG="${1#*=}"; shift ;;
    --skip-assets) SKIP_ASSETS=1; shift ;;
    --skip-videos) SKIP_VIDEOS=1; shift ;;
    --skip-seed) SKIP_SEED=1; shift ;;
    --skip-pip) SKIP_PIP=1; shift ;;
    --no-dev) WITH_DEV=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done

# ----------------------------------------------------------------------------- platform
OS_NAME="$(uname -s)"
ARCH="$(uname -m)"
case "$OS_NAME" in
  Darwin) PLATFORM="macos" ;;
  Linux) PLATFORM="linux" ;;
  MINGW*|MSYS*|CYGWIN*) die "On Windows run PowerShell instead:  powershell -ExecutionPolicy Bypass -File setup.ps1" ;;
  *) die "Unsupported operating system: $OS_NAME (IronPulse supports macOS, Linux and Windows)" ;;
esac
PKG=""
if [ "$PLATFORM" = "macos" ]; then
  if have brew; then PKG="brew"; fi
else
  for pm in apt-get dnf pacman zypper; do
    if have "$pm"; then PKG="$pm"; break; fi
  done
fi
SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi

# value from .env without Python (used before the virtualenv exists)
dotenv_get() {
  local line
  [ -f "$ROOT/.env" ] || return 0
  line="$(grep -E "^$1=" "$ROOT/.env" 2>/dev/null | tail -n 1 || true)"
  line="${line#*=}"
  line="${line%%[[:space:]]#*}"
  printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}

if [ -z "$DB_MODE" ]; then
  DB_MODE="$(dotenv_get IRONPULSE_DB_MODE)"
  [ -n "$DB_MODE" ] || DB_MODE="runtime"
fi
case "$DB_MODE" in
  runtime|docker|native|existing) ;;
  *) die "--db must be runtime, docker, native or existing (got '$DB_MODE')" ;;
esac
if [ "$DB_MODE" = "existing" ] && [ -z "$DATABASE_URL_ARG" ] && [ -z "$(dotenv_get DATABASE_URL)" ]; then
  die "--db existing needs --database-url postgresql://user:password@host:5432/ironpulse"
fi

print_plan() {
  echo "IronPulse setup plan ($PLATFORM/$ARCH, package manager: ${PKG:-none}, database mode: $DB_MODE)"
  echo "  1. Python 3.10+ and the project virtualenv (.venv) + pip install -r requirements$([ "$WITH_DEV" -eq 1 ] && echo "-dev").txt"
  case "$DB_MODE" in
    runtime) echo "  2. micromamba -> ~/.ironpulse/env: postgresql=$RUNTIME_PG_MAJOR pgvector redis-server tesseract (conda-forge)" ;;
    docker) echo "  2. Docker: docker compose up -d db redis (PostgreSQL 17 + pgvector on :5433, Redis on :6380); Tesseract: ${PKG:-manual}" ;;
    native) echo "  2. ${PKG:-?}: PostgreSQL + pgvector (+ role/databases/extensions) and Tesseract" ;;
    existing) echo "  2. use your PostgreSQL server ($(printf '%s' "${DATABASE_URL_ARG:-DATABASE_URL from .env}" | redact)); Tesseract: ${PKG:-manual}" ;;
  esac
  echo "  3. .env: scripts/make_env.py (secrets) + manage.py configure --db $DB_MODE (connection settings)"
  [ "$DB_MODE" = "runtime" ] && echo "     start: scripts/runtime_postgres.sh start + scripts/runtime_redis.sh start"
  echo "  4. manage.py init-db --with-test-db (database, role, extensions vector/citext/pg_trgm, tables)"
  [ "$WITH_AI_DOCKER" -eq 1 ] && echo "  +  Docker: ollama + ollama-pull (Gemma failsafe, gemma3:4b)"
  [ "$WITH_VOICE" -eq 1 ] && echo "  +  Docker: kokoro (live coach voice, profile 'voice')"
  [ "$SKIP_ASSETS" -eq 0 ] && echo "  5. manage.py build-assets (Tailwind CLI download + CSS/JS bundle)"
  [ "$SKIP_VIDEOS" -eq 0 ] && echo "  6. manage.py gen-videos (workout videos)"
  [ "$SKIP_SEED" -eq 0 ] && echo "  7. manage.py seed (demo data -> var/demo_credentials.txt)"
  local port
  port="$(dotenv_get PORT)"
  echo "  then: ./run.sh  ->  http://localhost:${port:-10000}"
  return 0
}

if [ "$DRY_RUN" -eq 1 ]; then
  print_plan
  exit 0
fi

# ----------------------------------------------------------------------------- package helpers
APT_UPDATED=0
pkg_install() {
  case "$PKG" in
    brew) run brew install "$@" ;;
    apt-get)
      if [ "$APT_UPDATED" -eq 0 ]; then run $SUDO apt-get update -y; APT_UPDATED=1; fi
      run $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" ;;
    dnf) run $SUDO dnf install -y "$@" ;;
    pacman) run $SUDO pacman -S --needed --noconfirm "$@" ;;
    zypper) run $SUDO zypper --non-interactive install "$@" ;;
    *) return 1 ;;
  esac
}

as_postgres() {
  # run a command as the "postgres" OS user (Linux system PostgreSQL; peer authentication)
  (
    cd / || exit 1
    if [ "$(id -u)" -eq 0 ]; then
      if have runuser; then runuser -u postgres -- "$@"; else su postgres -s /bin/sh -c "$(printf '%q ' "$@")"; fi
    else
      sudo -u postgres "$@"
    fi
  )
}

manage() { "$VENV_PY" "$MANAGE" "$@"; }

# ----------------------------------------------------------------------------- Python + venv
python_ok() { "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; }

find_python() {
  local c
  for c in "$PYTHON_ARG" python3.13 python3.12 python3.11 python3.10 python3 python; do
    [ -n "$c" ] || continue
    if python_ok "$c"; then
      command -v "$c" 2>/dev/null || printf '%s\n' "$c"
      return 0
    fi
  done
  return 1
}

step_python() {
  step "Python 3.10+"
  if [ -x "$VENV_PY" ] && python_ok "$VENV_PY"; then
    ok "existing virtualenv .venv (Python $("$VENV_PY" -c 'import platform; print(platform.python_version())'))"
    return 0
  fi
  PY="$(find_python || true)"
  if [ -z "$PY" ]; then
    info "Python 3.10+ not found - installing it"
    case "$PKG" in
      brew) pkg_install python@3.12 ;;
      apt-get) pkg_install python3 python3-venv python3-pip ;;
      dnf) pkg_install python3 python3-pip ;;
      pacman) pkg_install python python-pip ;;
      zypper) pkg_install python3 python3-pip ;;
      *) die "Python 3.10+ is missing. Install it from https://www.python.org/downloads/ (macOS) or your package manager, or pass --python /path/to/python3" ;;
    esac
    hash -r
    PY="$(find_python || true)"
    [ -n "$PY" ] || die "Python 3.10+ is still not available (your distribution's python3 may be too old). Install python3.12 and re-run with --python /path/to/python3.12"
  fi
  ok "$PY (Python $("$PY" -c 'import platform; print(platform.python_version())'))"
}

step_venv() {
  step "Virtual environment (.venv) and Python packages"
  if ! { [ -x "$VENV_PY" ] && python_ok "$VENV_PY"; }; then
    if ! run "$PY" -m venv --clear "$VENV"; then
      if [ "$PKG" = "apt-get" ]; then
        warn "the venv module is missing - installing python3-venv"
        local pyver
        pyver="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
        pkg_install python3-venv "python${pyver}-venv" || pkg_install python3-venv
        run "$PY" -m venv --clear "$VENV"
      else
        die "could not create the virtualenv with $PY"
      fi
    fi
  fi
  if [ "$SKIP_PIP" -eq 1 ]; then
    ok "skipping pip (--skip-pip)"
    return 0
  fi
  run "$VENV_PY" -m pip install --disable-pip-version-check --quiet --upgrade pip
  if [ "$WITH_DEV" -eq 1 ]; then
    run "$VENV_PY" -m pip install --disable-pip-version-check -r "$ROOT/requirements-dev.txt"
  else
    run "$VENV_PY" -m pip install --disable-pip-version-check -r "$ROOT/requirements.txt"
  fi
  ok "packages installed"
}

# ----------------------------------------------------------------------------- Tesseract
step_tesseract() {
  step "Tesseract OCR"
  if [ "$DB_MODE" = "runtime" ] && [ -x "$IRONPULSE_HOME/env/bin/tesseract" ]; then
    TESSERACT_PATH="$IRONPULSE_HOME/env/bin/tesseract"
  elif have tesseract; then
    TESSERACT_PATH="$(command -v tesseract)"
  else
    local installed=1
    case "$PKG" in
      brew) pkg_install tesseract || installed=0 ;;
      apt-get) pkg_install tesseract-ocr tesseract-ocr-eng || installed=0 ;;
      dnf) pkg_install tesseract tesseract-langpack-eng || installed=0 ;;
      pacman) pkg_install tesseract tesseract-data-eng || installed=0 ;;
      zypper) pkg_install tesseract-ocr tesseract-ocr-traineddata-english || installed=0 ;;
      *) installed=0 ;;
    esac
    hash -r
    if have tesseract; then TESSERACT_PATH="$(command -v tesseract)"; fi
    [ "$installed" -eq 1 ] || warn "Tesseract could not be installed automatically - OCR uses the other engines (install it later and set TESSERACT_CMD)"
  fi
  if [ -n "$TESSERACT_PATH" ]; then ok "$TESSERACT_PATH"; fi
}

# ----------------------------------------------------------------------------- runtime (micromamba)
micromamba_platform() {
  case "$PLATFORM-$ARCH" in
    macos-arm64) echo "osx-arm64" ;;
    macos-x86_64) echo "osx-64" ;;
    linux-x86_64) echo "linux-64" ;;
    linux-aarch64|linux-arm64) echo "linux-aarch64" ;;
    linux-ppc64le) echo "linux-ppc64le" ;;
    *) return 1 ;;
  esac
}

step_runtime_install() {
  step "User-space runtime under $IRONPULSE_HOME (PostgreSQL $RUNTIME_PG_MAJOR + pgvector, Redis, Tesseract)"
  local mamba="$IRONPULSE_HOME/bin/micromamba" prefix="$IRONPULSE_HOME/env" plat need=""
  if [ -x "$mamba" ]; then
    ok "micromamba $("$mamba" --version 2>/dev/null || echo '')"
  else
    plat="$(micromamba_platform)" || die "micromamba has no build for $PLATFORM/$ARCH - use --db docker or --db native"
    have curl || die "curl is required to download micromamba"
    run mkdir -p "$IRONPULSE_HOME/bin"
    run curl -fSL --retry 3 -o "$mamba.part" "https://github.com/mamba-org/micromamba-releases/releases/latest/download/micromamba-$plat"
    run chmod +x "$mamba.part"
    run mv "$mamba.part" "$mamba"
  fi
  [ -x "$prefix/bin/postgres" ] || need="$need postgresql=$RUNTIME_PG_MAJOR"
  [ -f "$prefix/share/extension/vector.control" ] || need="$need pgvector"
  [ -x "$prefix/bin/redis-server" ] || need="$need redis-server"
  [ -x "$prefix/bin/tesseract" ] || need="$need tesseract"
  if [ -n "$need" ]; then
    info "installing from conda-forge:$need (first run only; a few hundred MB)"
    if [ -d "$prefix/conda-meta" ]; then
      # shellcheck disable=SC2086
      run "$mamba" install -y -r "$IRONPULSE_HOME/mamba" -p "$prefix" -c conda-forge $need
    else
      # shellcheck disable=SC2086
      run "$mamba" create -y -r "$IRONPULSE_HOME/mamba" -p "$prefix" -c conda-forge $need
    fi
  else
    ok "PostgreSQL, pgvector, Redis and Tesseract are installed in $prefix"
  fi
  TESSERACT_PATH="$prefix/bin/tesseract"
}

step_runtime_services() {
  step "Starting the user-space PostgreSQL and Redis"
  run "$ROOT/scripts/runtime_postgres.sh" start
  run "$ROOT/scripts/runtime_redis.sh" start
}

# ----------------------------------------------------------------------------- Docker
compose() { docker compose --env-file "$ROOT/.env" -f "$COMPOSE_FILE" "$@"; }

need_docker() {
  if ! have docker; then  # Docker Desktop's CLI is often not on PATH yet (a shell started before ~/.zprofile changed)
    local candidate
    for candidate in "$HOME/.docker/bin" /usr/local/bin /opt/homebrew/bin /Applications/Docker.app/Contents/Resources/bin; do
      if [ -x "$candidate/docker" ]; then PATH="$candidate:$PATH"; export PATH; break; fi
    done
  fi
  have docker || die "Docker is not installed. macOS: Docker Desktop (https://www.docker.com/products/docker-desktop/); Linux: https://docs.docker.com/engine/install/ - then re-run ./setup.sh"
  docker compose version >/dev/null 2>&1 || die "The Docker Compose v2 plugin ('docker compose') is missing - update Docker."
  if ! docker info >/dev/null 2>&1; then
    if [ "$PLATFORM" = "macos" ]; then
      info "starting Docker Desktop (this can take a minute) ..."
      open -a Docker >/dev/null 2>&1 || true
      local i=0
      while [ "$i" -lt 60 ] && ! docker info >/dev/null 2>&1; do sleep 3; i=$((i + 1)); done
    fi
    docker info >/dev/null 2>&1 || die "Docker is installed but not running (or you lack permission). Start Docker Desktop / 'sudo systemctl start docker'. On Linux, allow your user: sudo usermod -aG docker \"$USER\", then log out and back in."
  fi
  ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo 'running')"
}

step_docker_db() {
  step "PostgreSQL 17 + pgvector and Redis in Docker"
  run docker compose --env-file "$ROOT/.env" -f "$COMPOSE_FILE" up -d db redis
}

step_docker_optional() {
  if [ "$WITH_AI_DOCKER" -eq 1 ]; then
    step "Gemma failsafe in Docker (Ollama)"
    run docker compose --env-file "$ROOT/.env" -f "$COMPOSE_FILE" up -d ollama ollama-pull
    info "the model downloads in the background (~3.3 GB): docker compose --env-file .env -f docker/compose.yml logs -f ollama-pull"
  fi
  if [ "$WITH_VOICE" -eq 1 ]; then
    step "Kokoro voice in Docker"
    run docker compose --env-file "$ROOT/.env" -f "$COMPOSE_FILE" --profile voice up -d kokoro
  fi
}

# ----------------------------------------------------------------------------- native PostgreSQL
fix_pg_hba() {
  # Fedora/openSUSE default to "ident" for TCP logins; the app signs in with a password (scram-sha-256)
  local hba
  hba="$(as_postgres psql -tAqc 'SHOW hba_file' 2>/dev/null || true)"
  [ -n "$hba" ] || return 0
  if as_postgres grep -Eq '^host[[:space:]]+all[[:space:]]+all[[:space:]]+(127\.0\.0\.1/32|::1/128)[[:space:]]+ident' "$hba"; then
    info "allowing password logins from localhost in $hba (backup: $hba.ironpulse.bak)"
    as_postgres cp "$hba" "$hba.ironpulse.bak"
    as_postgres sed -i -E 's#^(host[[:space:]]+all[[:space:]]+all[[:space:]]+(127\.0\.0\.1/32|::1/128)[[:space:]]+)ident#\1scram-sha-256#' "$hba"
    as_postgres psql -qc 'SELECT pg_reload_conf()' >/dev/null
  fi
}

native_bootstrap_sql() {
  local app_pw db exists
  app_pw="$(manage env get APP_DB_PASSWORD)"
  [ -n "$app_pw" ] || die "APP_DB_PASSWORD is missing in .env"
  info "creating the role 'ironpulse' and the databases (as the postgres OS user)"
  exists="$(as_postgres psql -tAqc "SELECT 1 FROM pg_roles WHERE rolname = 'ironpulse'" || true)"
  if [ "$exists" = "1" ]; then
    printf "ALTER ROLE ironpulse WITH LOGIN PASSWORD '%s';\n" "$app_pw" | as_postgres psql -q -v ON_ERROR_STOP=1 >/dev/null
  else
    printf "CREATE ROLE ironpulse WITH LOGIN PASSWORD '%s';\n" "$app_pw" | as_postgres psql -q -v ON_ERROR_STOP=1 >/dev/null
  fi
  for db in ironpulse ironpulse_test; do
    exists="$(as_postgres psql -tAqc "SELECT 1 FROM pg_database WHERE datname = '$db'" || true)"
    if [ "$exists" != "1" ]; then
      as_postgres psql -q -v ON_ERROR_STOP=1 -c "CREATE DATABASE $db OWNER ironpulse" >/dev/null
    fi
    as_postgres psql -q -v ON_ERROR_STOP=1 -d "$db" -c 'CREATE EXTENSION IF NOT EXISTS vector' \
      -c 'CREATE EXTENSION IF NOT EXISTS citext' -c 'CREATE EXTENSION IF NOT EXISTS pg_trgm' >/dev/null \
      || die "could not create the extensions in $db - is pgvector installed for this PostgreSQL version? (https://github.com/pgvector/pgvector#installation)"
  done
  fix_pg_hba
  ok "role 'ironpulse' and databases ironpulse / ironpulse_test are ready"
}

start_linux_postgres() {
  if have systemctl && [ -d /run/systemd/system ]; then
    run $SUDO systemctl enable --now postgresql
  else
    run $SUDO service postgresql start
  fi
}

step_native_db() {
  step "PostgreSQL + pgvector from ${PKG:-your package manager}"
  case "$PKG" in
    brew)
      pkg_install postgresql@17 pgvector
      run brew services start postgresql@17
      local pgbin i=0
      pgbin="$(brew --prefix postgresql@17)/bin"
      while [ "$i" -lt 30 ] && ! "$pgbin/pg_isready" -q -h localhost -p 5432; do sleep 1; i=$((i + 1)); done
      # Homebrew's cluster trusts local connections and its superuser is your macOS account
      NATIVE_ADMIN_URL="postgresql+asyncpg://$(id -un)@localhost:5432/postgres"
      ;;
    apt-get)
      pkg_install postgresql postgresql-contrib
      local major
      major="$(find /usr/lib/postgresql -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort -n | tail -n 1 || true)"
      [ -n "$major" ] || die "PostgreSQL was installed but /usr/lib/postgresql is empty"
      if [ ! -f "/usr/share/postgresql/$major/extension/vector.control" ]; then
        pkg_install "postgresql-$major-pgvector" || die "pgvector for PostgreSQL $major is not in your apt sources. Add the PostgreSQL apt repository (https://wiki.postgresql.org/wiki/Apt) and re-run, or use --db runtime / --db docker."
      fi
      start_linux_postgres
      ;;
    dnf)
      pkg_install postgresql-server postgresql-contrib
      pkg_install pgvector || pkg_install postgresql-pgvector || warn "no pgvector package found - see https://github.com/pgvector/pgvector#installation"
      if ! $SUDO test -f /var/lib/pgsql/data/PG_VERSION; then run $SUDO postgresql-setup --initdb; fi
      start_linux_postgres
      ;;
    pacman)
      pkg_install postgresql
      pkg_install pgvector || warn "pgvector is not in your repositories - install it from the AUR, or use --db runtime / --db docker"
      if ! $SUDO test -f /var/lib/postgres/data/PG_VERSION; then
        as_postgres initdb -D /var/lib/postgres/data --auth-local=peer --auth-host=scram-sha-256
      fi
      start_linux_postgres
      ;;
    zypper)
      pkg_install postgresql-server postgresql-contrib
      start_linux_postgres
      local major
      major="$(as_postgres psql -tAqc 'SHOW server_version_num' 2>/dev/null | cut -c1-2 || true)"
      pkg_install "postgresql${major}-pgvector" || warn "no pgvector package for PostgreSQL $major - see https://github.com/pgvector/pgvector#installation"
      ;;
    *) die "No supported package manager found (brew, apt, dnf, pacman, zypper). Use --db runtime (no root needed) or --db docker." ;;
  esac
}

# ----------------------------------------------------------------------------- main
main() {
  printf '%sIronPulse setup%s - %s/%s, database mode: %s\n' "$BOLD" "$RESET" "$PLATFORM" "$ARCH" "$DB_MODE"
  chmod +x "$ROOT/run.sh" "$ROOT/setup.sh" "$ROOT"/scripts/*.sh 2>/dev/null || true

  if [ "$DB_MODE" = "docker" ] || [ "$WITH_AI_DOCKER" -eq 1 ] || [ "$WITH_VOICE" -eq 1 ]; then
    step "Docker"
    need_docker
  fi

  step_python
  [ "$DB_MODE" = "runtime" ] && step_runtime_install
  step_tesseract
  step_venv

  step "Configuration (.env)"
  run "$VENV_PY" "$ROOT/scripts/make_env.py"
  case "$DB_MODE" in
    runtime) run "$VENV_PY" "$MANAGE" configure --db runtime --tesseract "$TESSERACT_PATH" ;;
    docker)
      if [ -n "$TESSERACT_PATH" ]; then run "$VENV_PY" "$MANAGE" configure --db docker --tesseract "$TESSERACT_PATH"
      else run "$VENV_PY" "$MANAGE" configure --db docker; fi ;;
    native)
      run "$VENV_PY" "$MANAGE" configure --db native ${TESSERACT_PATH:+--tesseract "$TESSERACT_PATH"} ;;
    existing)
      local url="${DATABASE_URL_ARG:-$(dotenv_get DATABASE_URL)}"
      if [ -n "$ADMIN_URL_ARG" ]; then
        run "$VENV_PY" "$MANAGE" configure --db existing --database-url "$url" --admin-url "$ADMIN_URL_ARG" ${TESSERACT_PATH:+--tesseract "$TESSERACT_PATH"}
      else
        run "$VENV_PY" "$MANAGE" configure --db existing --database-url "$url" ${TESSERACT_PATH:+--tesseract "$TESSERACT_PATH"}
      fi ;;
  esac

  case "$DB_MODE" in
    runtime) step_runtime_services ;;
    docker) step_docker_db ;;
    native)
      step_native_db
      if [ -n "$NATIVE_ADMIN_URL" ]; then
        run "$VENV_PY" "$MANAGE" configure --db native --admin-url "$NATIVE_ADMIN_URL"
      elif [ "$PLATFORM" = "linux" ]; then
        native_bootstrap_sql
      fi ;;
    existing) ;;
  esac
  step_docker_optional

  step "Database: role, databases, extensions and tables"
  run "$VENV_PY" "$MANAGE" init-db --with-test-db --wait 120

  if [ "$SKIP_ASSETS" -eq 0 ]; then
    step "Front-end assets (Tailwind CSS + JS bundle)"
    run "$VENV_PY" "$MANAGE" build-assets || warn "the asset build failed - the prebuilt files in app/static/dist are used"
  fi
  if [ "$SKIP_VIDEOS" -eq 0 ]; then
    step "Workout videos"
    run "$VENV_PY" "$MANAGE" gen-videos || warn "videos were not generated (optional) - retry later: .venv/bin/python scripts/manage.py gen-videos"
  fi
  if [ "$SKIP_SEED" -eq 0 ]; then
    step "Demo data"
    run "$VENV_PY" "$MANAGE" seed || warn "the demo data was not loaded (optional) - retry later: .venv/bin/python scripts/manage.py seed"
  fi

  local port gateway
  port="$(manage env get --effective PORT 2>/dev/null || true)"
  gateway="$(manage env get AI_GATEWAY_BASE_URL 2>/dev/null || true)"
  printf '\n%s%sIronPulse is ready.%s\n\n' "$BOLD" "$GREEN" "$RESET"
  echo "  Start the app:     ./run.sh                (while developing: ./run.sh --reload)"
  echo "  Then open:         http://localhost:${port:-10000}"
  echo "  On your phone:     ./run.sh --lan          (HTTPS on this computer's Wi-Fi address - README 'Use it on your phone')"
  echo "  Health check:      .venv/bin/python scripts/manage.py check"
  echo "  Demo accounts:     var/demo_credentials.txt (written by the demo seed)"
  echo "  AI:                Gemma gateway ${gateway:-(not set)} -> Gemma in Docker -> Claude"
  echo "                     .venv/bin/python scripts/manage.py ai-status   |   ai-chat \"Hello Gemma\""
  if [ "$WITH_AI_DOCKER" -eq 0 ]; then
    echo "  AI failsafe:       ./setup.sh --with-ai-docker   (Gemma in Docker when the gateway is down)"
  else
    echo "  AI failsafe:       ./run.sh starts Gemma in Docker in the background (skip: ./run.sh --no-ai-docker)"
  fi
  echo ""
  echo "  Disclaimer: student-grade project. The owner, the developer and the AI are NOT responsible for anything;"
  echo "  nothing here is medical, legal or financial advice."
}

main
