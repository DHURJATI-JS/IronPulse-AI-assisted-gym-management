"""Run IronPulse (host/port from .env - default 127.0.0.1:10000). All of these work:

    python -m app                      (recommended, from the project folder)
    python app/__main__.py             (e.g. VS Code "Run Python File")
    .venv/bin/python -m app            (macOS / Linux)    .venv\\Scripts\\python -m app   (Windows)

If it is started with an interpreter that lacks the dependencies (e.g. the system Python), it re-launches
itself with the project's .venv automatically. For several worker processes use
``python -m uvicorn app.main:app --workers 4`` and set REALTIME_PG_BRIDGE=true.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKERS_ENV = "IRONPULSE_WORKERS"  # = app.db.WORKERS_ENV: the database connection-budget check reads it


def _worker_count(reload: bool) -> int:
    """How many processes ``uvicorn.run`` below starts: one with reload, else WEB_CONCURRENCY (uvicorn's own default
    for its ``workers``) or one. Handed to the workers so each one's pool check knows how many pools share PostgreSQL."""
    raw = os.environ.get("WEB_CONCURRENCY", "").strip()
    return 1 if reload or not raw.isdigit() or int(raw) < 1 else int(raw)


def _venv_python() -> Path | None:
    for rel in (".venv/bin/python", ".venv/Scripts/python.exe", "venv/bin/python", "venv/Scripts/python.exe"):
        candidate = ROOT / rel
        if candidate.exists():
            return candidate
    return None


def _ensure_environment() -> None:
    # 1) running as a plain script puts app/ (not the project root) on sys.path -> fix it
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    # 2) wrong interpreter (no dependencies) -> hop into the project virtualenv once
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ModuleNotFoundError as exc:
        venv = _venv_python()
        if venv is not None and not os.environ.get("IRONPULSE_RELAUNCHED"):
            print(f"[ironpulse] '{exc.name}' is not installed for {sys.executable}; re-launching with {venv}", flush=True)
            env = dict(os.environ, IRONPULSE_RELAUNCHED="1")
            raise SystemExit(subprocess.call([str(venv), "-m", "app", *sys.argv[1:]], cwd=str(ROOT), env=env))
        print(
            "[ironpulse] Dependencies are missing. Run the setup script first:\n"
            "    Windows:      powershell -ExecutionPolicy Bypass -File setup.ps1\n"
            "    macOS/Linux:  ./setup.sh\n"
            "or: python -m venv .venv && .venv/bin/python -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    if not (ROOT / ".env").exists():
        print("[ironpulse] No .env file yet - creating one with fresh secrets (scripts/make_env.py) ...", flush=True)
        subprocess.call([sys.executable, str(ROOT / "scripts" / "make_env.py")], cwd=str(ROOT))


def main() -> None:
    _ensure_environment()
    os.chdir(ROOT)

    import uvicorn

    from app.config import get_settings, uvicorn_forwarded_allow_ips

    s = get_settings()  # unusable settings end the process here with one line per problem (ConfigError, exit 3)

    print(f"[ironpulse] starting {s.app_name} on http://{s.host}:{s.port}  (Ctrl+C to stop)", flush=True)
    os.environ[WORKERS_ENV] = str(_worker_count(s.reload))  # inherited by every worker process (pool budget check)
    uvicorn.run(
        "app.main:app",
        host=s.host,
        port=s.port,
        app_dir=str(ROOT),
        reload=s.reload,
        reload_dirs=[str(ROOT / "app")] if s.reload else None,
        proxy_headers=s.trust_proxy_headers,  # never trust X-Forwarded-For unless explicitly enabled
        # ...and only from our own proxies: addresses / networks only ('*' or a name would mean "trust everyone")
        forwarded_allow_ips=uvicorn_forwarded_allow_ips(s),
        server_header=False,
        log_level="debug" if s.debug else "info",
    )


if __name__ == "__main__":
    main()
