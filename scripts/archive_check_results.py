#!/usr/bin/env python3
"""Archive raw check_results older than --days to S3 (MAK-149).

Run from the systemd timer (`pingback-archive.timer`) after the nightly
sqlite backup. Exit code 0 = success (including "nothing to do"); non-zero
= error so the timer surfaces it in `systemctl status`.

Usage:
    python scripts/archive_check_results.py [--db PATH] [--days N] \
        [--bucket BUCKET] [--prefix PREFIX] [--dry-run]

Defaults come from config / env (`DB_PATH`, `ARCHIVE_AFTER_DAYS`,
`ARCHIVE_S3_BUCKET`, `ARCHIVE_S3_PREFIX`). `--dry-run` reports the
candidates without uploading or deleting.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import aiosqlite

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pingback.config import (  # noqa: E402
    ARCHIVE_AFTER_DAYS,
    ARCHIVE_S3_BUCKET,
    ARCHIVE_S3_PREFIX,
    DB_PATH,
)
from pingback.db.schema import initialize_database  # noqa: E402
from pingback.services.archiver import (  # noqa: E402
    _candidate_partitions,
    _month_floor,
    archive_old_check_results,
)
from datetime import datetime, timedelta, timezone  # noqa: E402


async def _run(
    db_path: str, days: int, bucket: str, prefix: str, dry_run: bool
) -> dict:
    if not os.path.exists(db_path):
        print(f"error: db not found at {db_path}", file=sys.stderr)
        sys.exit(2)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await initialize_database(db)
        if dry_run:
            now = datetime.now(timezone.utc)
            cutoff = _month_floor(now - timedelta(days=days))
            cands = await _candidate_partitions(db, cutoff)
            return {
                "dry_run": True,
                "cutoff_month_start": cutoff.isoformat(),
                "candidate_partitions": len(cands),
                "sample": cands[:5],
            }
        if not bucket:
            print(
                "error: ARCHIVE_S3_BUCKET unset (and --bucket not provided); refusing to run",
                file=sys.stderr,
            )
            sys.exit(3)
        summary = await archive_old_check_results(
            db,
            bucket=bucket,
            prefix=prefix,
            archive_after_days=days,
        )
        return {
            "dry_run": False,
            "partitions_uploaded": summary.partitions_uploaded,
            "rows_uploaded": summary.rows_uploaded,
            "rows_deleted": summary.rows_deleted,
            "bytes_uploaded": summary.bytes_uploaded,
            "skipped_already_logged": summary.skipped_already_logged,
        }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=DB_PATH, help="path to sqlite db (default from DB_PATH)")
    p.add_argument(
        "--days", type=int, default=ARCHIVE_AFTER_DAYS,
        help="archive rows older than this many days (default from ARCHIVE_AFTER_DAYS)",
    )
    p.add_argument(
        "--bucket", default=ARCHIVE_S3_BUCKET,
        help="target S3 bucket (default from ARCHIVE_S3_BUCKET)",
    )
    p.add_argument(
        "--prefix", default=ARCHIVE_S3_PREFIX,
        help="S3 key prefix (default from ARCHIVE_S3_PREFIX)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="report candidate partitions and exit without touching S3 or sqlite",
    )
    args = p.parse_args()
    out = asyncio.run(_run(args.db, args.days, args.bucket, args.prefix, args.dry_run))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
