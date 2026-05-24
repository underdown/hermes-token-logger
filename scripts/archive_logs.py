#!/usr/bin/env python3
"""Nightly archival: gzip yesterday's CSV, vacuum SQLite, prune old files.

Runs as a cron job. Safe to run multiple times (idempotent).
"""
import gzip
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path.home() / ".hermes" / "token_logs"
DB_PATH = LOG_DIR / "token_logs.db"
RETENTION_DAYS = 90  # keep uncompressed CSVs for this many days

def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 1. Gzip yesterday's CSV (if it exists and isn't today's)
    for csv_path in sorted(LOG_DIR.glob("*.csv")):
        date_tag = csv_path.stem  # "2026-05-24"
        if date_tag >= today:
            continue  # don't touch today's active file

        gz_path = csv_path.with_suffix(".csv.gz")
        if gz_path.exists():
            # Already archived — skip
            csv_path.unlink(missing_ok=True)
            continue

        # Compress
        with open(csv_path, "rb") as f_in:
            with gzip.open(gz_path, "wb", compresslevel=6) as f_out:
                shutil.copyfileobj(f_in, f_out)

        # Verify gzip is valid before removing original
        try:
            with gzip.open(gz_path, "rb") as f:
                f.read(1)  # test readability
            csv_path.unlink()
        except Exception:
            gz_path.unlink(missing_ok=True)  # remove broken gzip
            print(f"WARNING: gzip compression failed for {csv_path.name}, keeping original")

    # 2. Remove uncompressed CSVs older than retention
    cutoff = time.time() - RETENTION_DAYS * 86400
    for csv_path in LOG_DIR.glob("*.csv"):
        if csv_path.stat().st_mtime < cutoff:
            csv_path.unlink(missing_ok=True)

    # 3. Vacuum SQLite (reclaim space after deletes)
    if DB_PATH.exists():
        db = sqlite3.connect(str(DB_PATH))
        db.execute("PRAGMA optimize")
        db.close()


if __name__ == "__main__":
    main()
