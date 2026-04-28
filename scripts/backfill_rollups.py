#!/usr/bin/env python3
"""Backfill check_results_{1m,5m,1h} from raw check_results (MAK-147).

Idempotent: every bucket uses INSERT OR REPLACE keyed on (monitor_id, window_start),
so re-running this script over a range that's already been backfilled just
overwrites with identical values. Safe to run on a live DB — the compactor in
the scheduler will keep going from where this left off.

Usage:
    python scripts/backfill_rollups.py [--db PATH] [--days N] [--tier 1m|5m|1h|all]

Defaults:
    --db    pingback.db
    --days  90       (covers Pro retention; bump for Business)
    --tier  all
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

# Allow running from repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pingback.db.rollups import backfill  # noqa: E402
from pingback.db.schema import initialize_database  # noqa: E402


async def _run(db_path: str, days: int, tier: str) -> None:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    if not os.path.exists(db_path):
        print(f"error: db not found at {db_path}", file=sys.stderr)
        sys.exit(2)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await initialize_database(db)  # ensures rollup tables exist
        tiers = ["1m", "5m", "1h"] if tier == "all" else [tier]
        for t in tiers:
            print(f"backfilling {t} from {start.isoformat()} to {end.isoformat()} …", flush=True)
            n = await backfill(db, t, start, end)
            print(f"  -> wrote {n} rollup rows", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="pingback.db", help="path to sqlite db (default: pingback.db)")
    p.add_argument("--days", type=int, default=90, help="how many days back to backfill (default: 90)")
    p.add_argument("--tier", choices=["1m", "5m", "1h", "all"], default="all")
    args = p.parse_args()
    asyncio.run(_run(args.db, args.days, args.tier))


if __name__ == "__main__":
    main()
