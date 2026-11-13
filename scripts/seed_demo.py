"""Load the demo gyms, accounts and shops into the database:   python scripts/seed_demo.py [--gym SLUG ...]

The demo data is *data*: scripts/demo/dataset.json, written to PostgreSQL through the app's models and services.
Nothing in the app is hardcoded - once loaded these are ordinary database rows that you can edit, hide or delete
in the app like anything else. Idempotent and additive: re-running only adds what is missing. Every demo account
shares one password, saved with the account list in var/demo_credentials.txt.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--gym", action="append", metavar="SLUG", help="only load this gym from the dataset (repeatable)")
    args = parser.parse_args(argv)

    from app.config import get_settings
    from app.db import create_all, dispose_engine, session_scope
    from app.services.bootstrap import bootstrap
    from scripts.demo.loader import CREDENTIALS_FILE, seed_demo

    await create_all()
    async with session_scope() as db:
        await bootstrap(db)
    async with session_scope() as db:  # one transaction: all or nothing
        result = await seed_demo(db, gyms=args.gym)
    await dispose_engine()

    added = ", ".join(f"{n} {what}" for what, n in result["stats"].items() if n)
    print(f"[seed] {'added ' + added if added else 'the demo data is already in the database - nothing to add'}.")
    creds = get_settings().var_dir / CREDENTIALS_FILE
    try:
        print(creds.read_text(encoding="utf-8"))
    except OSError:
        print(f"[seed] password for every demo account: {result['password']}")
    print(f"[seed] saved to {creds}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
