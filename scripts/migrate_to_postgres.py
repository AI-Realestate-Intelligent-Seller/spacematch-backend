"""One-time copy of the local SQLite database into Postgres (e.g. Supabase).

    python scripts/migrate_to_postgres.py            # SQLite at DB_PATH  ->  DATABASE_URL from .env
    python scripts/migrate_to_postgres.py --dry-run  # only show what would be copied

- Creates the schema in Postgres (same as the app does on start).
- Refuses to run if the Postgres tables already contain data (so it can't duplicate or overwrite).
- Copies every table in foreign-key order, in the original insertion order, in one transaction.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # run from anywhere: python scripts/<name>.py

from app.core import settings
from app.db import storage
ORDER = ("users", "properties", "accounts", "reels", "property_reels", "metric_snapshots", "oauth_states",
         "property_brokers", "invites", "api_keys", "link_history", "refreshes", "read_logs",
         "idempotency_keys", "app_state", "profile_scans", "password_resets", "email_codes", "account_events")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="count rows only, write nothing")
    args = parser.parse_args()

    target = settings.DATABASE_URL
    if not target:
        sys.exit("DATABASE_URL is not set in .env - nothing to migrate to.")

    # 1) Bring the SQLite file up to the current schema (same upgrade the app runs on start).
    storage.DATABASE_URL = ""
    storage.init_db()
    src = sqlite3.connect(storage.DB_PATH)
    src.row_factory = sqlite3.Row
    counts = {t: src.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ORDER}
    print(f"Source: {storage.DB_PATH}")
    for t in ORDER:
        print(f"  {t:<18} {counts[t]:>6} rows")
    if args.dry_run:
        return

    # 2) Create the schema in Postgres and make sure it's empty.
    storage.DATABASE_URL = target
    storage.init_db()
    with storage.db() as c:
        busy = [t for t in ORDER if c.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]]
    if busy:
        sys.exit(f"Postgres already has data in: {', '.join(busy)}. Refusing to copy (nothing was changed).")

    # 3) Copy, parents before children, oldest rows first.
    with storage.db() as c:
        for t in ORDER:
            if not counts[t]:
                continue
            pg_cols = storage._columns(c, t) - {"rowid"}
            src_cols = [r["name"] for r in src.execute(f"PRAGMA table_info({t})")]
            cols = [col for col in src_cols if col in pg_cols]
            rows = [tuple(r[col] for col in cols) for r in src.execute(f"SELECT * FROM {t} ORDER BY rowid")]
            sql = f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
            with c._conn.cursor() as cur:
                cur.executemany(sql, rows)
            print(f"  copied {t:<18} {len(rows):>6}")
    # 4) Verify.
    with storage.db() as c:
        bad = {t: c.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in ORDER}
    mismatched = [t for t in ORDER if bad[t] != counts[t]]
    if mismatched:
        sys.exit(f"Row counts differ for: {mismatched}")
    print("Done - all row counts match.")
    storage.close_pool()


if __name__ == "__main__":
    main()
