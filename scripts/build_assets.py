"""Build front-end assets (Windows / macOS / Linux):

* CSS: Tailwind CSS v4 standalone CLI  tailwind/input.css -> app/static/dist/app.min.css (--minify)
* JS : app/static/src/js/*.js (filename order) -> app/static/dist/app.min.js (rjsmin)

    python scripts/build_assets.py                 # build (downloads the Tailwind CLI if missing)
    python scripts/build_assets.py --no-download   # never download; skip CSS if no CLI is found
    python scripts/build_assets.py --watch-css     # rebuild CSS on template changes (dev)
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAILWIND_VERSION = os.environ.get("TAILWIND_VERSION", "4.3.3")
TOOLS = ROOT / ".tools"
SRC_JS = ROOT / "app" / "static" / "src" / "js"
DIST = ROOT / "app" / "static" / "dist"
CSS_IN = ROOT / "tailwind" / "input.css"


def say(msg: str) -> None:
    print(f"[assets] {msg}", flush=True)


def tailwind_asset_name() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    if system == "windows":
        return f"tailwindcss-windows-{arch}.exe"
    if system == "darwin":
        return f"tailwindcss-macos-{arch}"
    return f"tailwindcss-linux-{arch}"


def find_tailwind() -> Path | None:
    env = os.environ.get("TAILWIND_BIN")
    candidates = [Path(env)] if env else []
    exe = ".exe" if os.name == "nt" else ""
    candidates += [
        TOOLS / f"tailwindcss-{TAILWIND_VERSION}{exe}",
        Path.home() / ".ironpulse" / "bin" / f"tailwindcss-{TAILWIND_VERSION}{exe}",
    ]
    which = shutil.which("tailwindcss")
    if which:
        candidates.append(Path(which))
    for c in candidates:
        if c and c.is_file():
            return c
    return None


def download_tailwind() -> Path:
    TOOLS.mkdir(parents=True, exist_ok=True)
    exe = ".exe" if os.name == "nt" else ""
    target = TOOLS / f"tailwindcss-{TAILWIND_VERSION}{exe}"
    url = f"https://github.com/tailwindlabs/tailwindcss/releases/download/v{TAILWIND_VERSION}/{tailwind_asset_name()}"
    say(f"downloading Tailwind CLI v{TAILWIND_VERSION}: {url}")
    tmp = target.with_suffix(target.suffix + ".part")
    with urllib.request.urlopen(url, timeout=180) as resp, open(tmp, "wb") as fh:  # noqa: S310 - fixed https URL
        shutil.copyfileobj(resp, fh)
    tmp.replace(target)
    if os.name != "nt":
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def build_css(tailwind: Path, watch: bool = False) -> None:
    DIST.mkdir(parents=True, exist_ok=True)
    out = DIST / "app.min.css"
    cmd = [str(tailwind), "-i", str(CSS_IN), "-o", str(out), "--minify"]
    if watch:
        cmd.append("--watch")
    say("building CSS with " + tailwind.name)
    subprocess.run(cmd, cwd=str(ROOT), check=True)
    say(f"wrote {out.relative_to(ROOT)} ({out.stat().st_size / 1024:.1f} KiB)")


def build_js() -> None:
    import rjsmin

    DIST.mkdir(parents=True, exist_ok=True)
    files = sorted(SRC_JS.glob("*.js"))
    parts = []
    for f in files:
        code = f.read_text(encoding="utf-8")
        parts.append(f"/* {f.name} */\n{code}\n;")
    bundle = "\n".join(parts)
    (DIST / "app.js").write_text(bundle, encoding="utf-8")
    minified = rjsmin.jsmin(bundle, keep_bang_comments=False)
    (DIST / "app.min.js").write_text(minified, encoding="utf-8")
    say(f"bundled {len(files)} JS files -> app/static/dist/app.min.js ({len(minified) / 1024:.1f} KiB)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--watch-css", action="store_true")
    ap.add_argument("--js-only", action="store_true")
    args = ap.parse_args()

    build_js()
    if args.js_only:
        return 0
    tailwind = find_tailwind()
    if tailwind is None and not args.no_download:
        try:
            tailwind = download_tailwind()
        except Exception as exc:  # offline machine: keep the committed CSS
            say(f"WARNING: could not download the Tailwind CLI ({exc}); keeping the existing app.min.css")
    if tailwind is None:
        if (DIST / "app.min.css").exists():
            say("Tailwind CLI not found - using the prebuilt app/static/dist/app.min.css")
            return 0
        say("ERROR: no Tailwind CLI and no prebuilt CSS. Install it or set TAILWIND_BIN.")
        return 1
    try:
        build_css(tailwind, watch=args.watch_css)
    except subprocess.CalledProcessError as exc:
        say(f"ERROR: Tailwind build failed ({exc})")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
