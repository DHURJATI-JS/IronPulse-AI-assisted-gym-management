"""Development asset watcher (Windows / macOS / Linux):

* runs the Tailwind CLI in --watch mode (rebuilds app/static/dist/app.min.css when templates/JS change)
* re-bundles + minifies app/static/src/js/*.js into app/static/dist/app.min.js whenever a JS file changes

    python scripts/dev_watch.py          (Ctrl+C to stop)
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_assets  # noqa: E402


def js_snapshot() -> dict[str, float]:
    return {str(p): p.stat().st_mtime for p in build_assets.SRC_JS.glob("*.js")}


def main() -> int:
    tailwind = build_assets.find_tailwind() or build_assets.download_tailwind()
    css_proc = subprocess.Popen(
        [str(tailwind), "-i", str(build_assets.CSS_IN), "-o", str(build_assets.DIST / "app.min.css"), "--minify", "--watch=always"],
        cwd=str(ROOT),
    )
    print("[watch] Tailwind watching templates; JS bundler polling every 1.5 s (Ctrl+C to stop)", flush=True)
    last = js_snapshot()
    build_assets.build_js()
    try:
        while True:
            time.sleep(1.5)
            if css_proc.poll() is not None:  # restart Tailwind if it exits
                css_proc = subprocess.Popen(
                    [str(tailwind), "-i", str(build_assets.CSS_IN), "-o", str(build_assets.DIST / "app.min.css"), "--minify", "--watch=always"],
                    cwd=str(ROOT),
                )
            snap = js_snapshot()
            if snap != last:
                last = snap
                try:
                    build_assets.build_js()
                except Exception as exc:  # keep watching even if a file is mid-write
                    print(f"[watch] JS bundle failed: {exc}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        css_proc.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
