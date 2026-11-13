"""Create (or complete) the .env file from .env.example with freshly generated secrets.

    python scripts/make_env.py
    python scripts/make_env.py --set DATABASE_URL=postgresql+asyncpg://u:p@localhost:5432/ironpulse

Works the same on Windows, macOS and Linux. Existing non-placeholder values are never overwritten
(unless passed explicitly with --set), so it is safe to run repeatedly.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import secrets
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDERS = {"", "change-me-run-setup-to-generate-a-64-char-secret"}
LINE_RE = re.compile(r"^(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")


def fernet_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        m = LINE_RE.match(line.strip())
        if not m:
            continue
        raw = m.group("value")
        # strip an inline comment (whitespace + #) the same way python-dotenv does for unquoted values
        raw = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
        values[m.group("key")] = raw.strip('"').strip("'")
    return values


def set_values(text: str, updates: dict[str, str]) -> str:
    lines = text.splitlines()
    seen: set[str] = set()
    for i, line in enumerate(lines):
        m = LINE_RE.match(line.strip())
        if not m or m.group("key") not in updates:
            continue
        key = m.group("key")
        comment = ""
        parts = re.split(r"(\s+#.*)$", m.group("value"), maxsplit=1)
        if len(parts) > 1:
            comment = parts[1]
        lines[i] = f"{key}={updates[key]}{comment}"
        seen.add(key)
    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="force a value")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    env_path = ROOT / ".env"
    example_path = ROOT / ".env.example"
    if not example_path.exists():
        print("error: .env.example is missing", file=sys.stderr)
        return 1
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else example_path.read_text(encoding="utf-8")
    current = parse(text)

    updates: dict[str, str] = {}
    generators = {
        "SECRET_KEY": lambda: secrets.token_urlsafe(48),
        "DATA_ENCRYPTION_KEY": fernet_key,
        "ID_HASH_PEPPER": lambda: secrets.token_urlsafe(32),
        "SUPERADMIN_PASSWORD": lambda: secrets.token_urlsafe(12),
    }
    for key, factory in generators.items():
        if current.get(key, "") in PLACEHOLDERS:
            updates[key] = factory()

    for item in args.set:
        if "=" not in item:
            print(f"error: --set expects KEY=VALUE, got {item!r}", file=sys.stderr)
            return 2
        key, value = item.split("=", 1)
        updates[key.strip()] = value.strip()

    env_path.write_text(set_values(text, updates), encoding="utf-8")
    if os.name != "nt":
        env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600 - secrets live here

    if not args.quiet:
        changed = ", ".join(sorted(updates)) or "nothing (already complete)"
        print(f"[make_env] {env_path} updated: {changed}")
        if "SUPERADMIN_PASSWORD" in updates:
            print("[make_env] a super-admin password was generated - see SUPERADMIN_PASSWORD in .env")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
