#!/usr/bin/env python3
"""Restore one or more archive partitions from S3 back into check_results (MAK-149).

This is the v1 restore path: download a partition file (`*.jsonl.gz`),
verify SHA-256 against the metadata stored on the S3 object (or
`--expected-sha256`), and re-insert rows. Re-inserts use INSERT OR IGNORE on
the existing `id` PRIMARY KEY so a partial restore can be re-run.

Usage:
    python scripts/restore_check_results.py --key <s3-key> [--bucket B] [--db PATH]
    python scripts/restore_check_results.py --file <local.jsonl.gz> [--db PATH]

If neither --key nor --file is given the script aborts. Provide
`--expected-sha256` to match a known-good digest; otherwise we compare
against the `sha256` metadata stored on the S3 object at upload time.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import os
import sys
from pathlib import Path

import aiosqlite

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pingback.config import (  # noqa: E402
    ARCHIVE_S3_BUCKET,
    AWS_ACCESS_KEY_ID,
    AWS_DEFAULT_REGION,
    AWS_SECRET_ACCESS_KEY,
    DB_PATH,
)
from pingback.db.schema import initialize_database  # noqa: E402


def _build_s3():
    import boto3

    return boto3.client(
        "s3",
        region_name=AWS_DEFAULT_REGION,
        aws_access_key_id=AWS_ACCESS_KEY_ID or None,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY or None,
    )


def _fetch_payload(bucket: str, key: str) -> tuple[bytes, str | None]:
    s3 = _build_s3()
    obj = s3.get_object(Bucket=bucket, Key=key)
    payload = obj["Body"].read()
    sha = (obj.get("Metadata") or {}).get("sha256")
    return payload, sha


def _decode_rows(payload: bytes) -> list[dict]:
    rows: list[dict] = []
    with gzip.GzipFile(fileobj=__import__("io").BytesIO(payload), mode="rb") as gz:
        for line in gz:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


async def _insert_rows(db_path: str, rows: list[dict]) -> int:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await initialize_database(db)
        inserted = 0
        for r in rows:
            cur = await db.execute(
                """INSERT OR IGNORE INTO check_results
                        (id, monitor_id, status, status_code, response_time_ms, error, checked_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["id"], r["monitor_id"], r["status"], r["status_code"],
                    r["response_time_ms"], r["error"], r["checked_at"],
                ),
            )
            inserted += cur.rowcount or 0
        await db.commit()
        return inserted


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--key", help="S3 key to restore (e.g. archive/check_results/...)")
    p.add_argument("--bucket", default=ARCHIVE_S3_BUCKET, help="bucket (default ARCHIVE_S3_BUCKET)")
    p.add_argument("--file", help="local .jsonl.gz to restore instead of fetching from S3")
    p.add_argument("--db", default=DB_PATH, help="sqlite path (default DB_PATH)")
    p.add_argument("--expected-sha256", help="override sha256 to verify against")
    args = p.parse_args()

    if not args.file and not args.key:
        print("error: provide --key or --file", file=sys.stderr)
        sys.exit(2)

    if args.file:
        payload = Path(args.file).read_bytes()
        meta_sha = None
    else:
        if not args.bucket:
            print("error: --bucket required (and ARCHIVE_S3_BUCKET unset)", file=sys.stderr)
            sys.exit(2)
        payload, meta_sha = _fetch_payload(args.bucket, args.key)

    actual_sha = hashlib.sha256(payload).hexdigest()
    expected = args.expected_sha256 or meta_sha
    if expected and expected != actual_sha:
        print(
            f"error: sha256 mismatch (expected={expected} actual={actual_sha})",
            file=sys.stderr,
        )
        sys.exit(4)

    rows = _decode_rows(payload)
    inserted = asyncio.run(_insert_rows(args.db, rows))
    print(json.dumps({
        "rows_in_file": len(rows),
        "rows_inserted": inserted,
        "rows_already_present": len(rows) - inserted,
        "sha256": actual_sha,
    }, indent=2))


if __name__ == "__main__":
    main()
