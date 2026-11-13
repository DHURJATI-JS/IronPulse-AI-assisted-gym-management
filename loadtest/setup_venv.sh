#!/usr/bin/env bash
# Create ../.venv-loadtest with Locust (separate from the app's .venv so the load generator never changes the app's
# packages). Tries the project's Python first (3.15: Locust/gevent ship binary wheels for it), then 3.13 / 3.12.
# With no usable interpreter it can build one with the runtime's micromamba (conda-forge):
#   loadtest/setup_venv.sh --conda      -> loadtest/.env-conda (python 3.12) + .venv-loadtest on top of it
set -Eeuo pipefail
LT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$LT/.." && pwd)"
VENV="$ROOT/.venv-loadtest"
say() { printf '[lt-setup] %s\n' "$*"; }

pick_python() {
  for c in "/Library/Frameworks/Python.framework/Versions/3.15/bin/python3" python3.15 python3.13 python3.12 python3; do
    if command -v "$c" >/dev/null 2>&1; then
      v="$("$c" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
      case "$v" in 3.1[0-9]) echo "$c"; return 0 ;; esac  # 3.10+; the system 3.9 is too old
    fi
  done
  return 1
}

PYBIN=""
if [ "${1:-}" = "--conda" ]; then
  MM="${IRONPULSE_HOME:-$HOME/.ironpulse}/bin/micromamba"
  [ -x "$MM" ] || { say "no micromamba at $MM"; exit 1; }
  "$MM" create -y -p "$LT/.env-conda" -c conda-forge python=3.12
  PYBIN="$LT/.env-conda/bin/python"
else
  PYBIN="$(pick_python)" || { say "no Python 3.10+ found - rerun with --conda"; exit 1; }
fi
say "using $PYBIN ($("$PYBIN" --version))"
[ -d "$VENV" ] || "$PYBIN" -m venv "$VENV"
"$VENV/bin/python" -m pip install -q --upgrade pip
# binary wheels only: never compile gevent & co. on this shared machine (fails loudly instead if a wheel is missing)
"$VENV/bin/python" -m pip install --only-binary=:all: -r "$LT/requirements.txt"
"$VENV/bin/locust" --version
say "ready: $VENV"
