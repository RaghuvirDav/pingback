"""Nightly archive of raw check_results to S3 (MAK-149).

Capacity verdict from MAK-144: BUSINESS retention (1yr × 30s × 100 monitors)
≈ 55 GB of raw rows per user. EC2 free-tier EBS is 30 GB. We keep raw rows
locally for `ARCHIVE_AFTER_DAYS` (90d default — covers the Pro window and the
operationally interesting tail), then push everything older to S3 as gzipped
JSONL partitioned by monitor_id + month, and delete the local rows.

Rollups (1m/5m/1h) are NOT touched; dashboard reads beyond 90d continue to
work via the 1h tier. The archive is for forensic / "export raw" use only.

Idempotency
-----------
A row in `check_results_archive_log(monitor_id, year_month)` is the
authoritative record that the partition has been uploaded AND that local
rows for that range have been deleted. The archiver:

1. Picks the set of (monitor_id, year_month) pairs that have raw rows older
   than the cutoff and that DON'T already have a log row.
2. For each pair, streams matching rows into a gzipped JSONL file in memory,
   PUTs it to S3 with a Content-MD5 (S3 verifies on receipt), inserts the
   log row, then deletes the underlying rows.
3. Crash mid-flight is safe: if the log row exists but rows are still
   present, the next run takes the log-only branch and just runs the
   delete; if the upload happened but the log row didn't, we'll re-upload
   (overwriting the same key — same data → same content, no harm).

Production cutover only matters once we have BUSINESS subscribers; on
FREE/PRO the historical retention floor (7d/90d) keeps everything in scope
of the regular `purge_expired_check_results` pass and the archiver is a
no-op.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

import aiosqlite

from pingback.config import (
    ARCHIVE_AFTER_DAYS,
    ARCHIVE_S3_BUCKET,
    ARCHIVE_S3_PREFIX,
    AWS_ACCESS_KEY_ID,
    AWS_DEFAULT_REGION,
    AWS_SECRET_ACCESS_KEY,
)

logger = logging.getLogger("pingback.archiver")


@dataclass(frozen=True)
class ArchiveRunSummary:
    """What an archive pass actually did. Used by tests + ops logs."""

    partitions_uploaded: int
    rows_uploaded: int
    rows_deleted: int
    bytes_uploaded: int
    skipped_already_logged: int


def _month_floor(dt: datetime) -> datetime:
    """First instant of `dt`'s UTC month. Year/month boundaries only."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return datetime(dt.year, dt.month, 1, tzinfo=timezone.utc)


def _next_month(dt: datetime) -> datetime:
    """Start of the month after `dt`. Handles December roll-over."""
    if dt.month == 12:
        return datetime(dt.year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(dt.year, dt.month + 1, 1, tzinfo=timezone.utc)


def _partition_key(prefix: str, monitor_id: str, year: int, month: int) -> str:
    """Hive-style key so Athena/Glue can read it later without a manifest."""
    return f"{prefix.rstrip('/')}/monitor={monitor_id}/year={year:04d}/{year:04d}-{month:02d}.jsonl.gz"


def _row_to_record(row) -> dict:
    """Single raw check → archive record. Schema is intentionally flat."""
    return {
        "id": row["id"],
        "monitor_id": row["monitor_id"],
        "status": row["status"],
        "status_code": row["status_code"],
        "response_time_ms": row["response_time_ms"],
        "error": row["error"],
        "checked_at": row["checked_at"],
    }


def _build_gzip_jsonl(rows: Iterable) -> tuple[bytes, str, int]:
    """Encode rows as gzip(JSONL). Returns (payload, sha256_hex, row_count).

    Streamed via BytesIO so we never materialise the JSON in two layouts.
    """
    buf = io.BytesIO()
    count = 0
    # mtime=0 keeps the gzip header byte-stable across runs of the same data,
    # which makes idempotent re-uploads produce identical objects.
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        for r in rows:
            gz.write((json.dumps(_row_to_record(r), separators=(",", ":")) + "\n").encode("utf-8"))
            count += 1
    payload = buf.getvalue()
    return payload, hashlib.sha256(payload).hexdigest(), count


def _build_s3_client():
    """Lazy boto3 import so dev/CI without AWS creds doesn't pay the import."""
    import boto3  # noqa: WPS433 (lazy on purpose)

    return boto3.client(
        "s3",
        region_name=AWS_DEFAULT_REGION,
        aws_access_key_id=AWS_ACCESS_KEY_ID or None,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY or None,
    )


async def _candidate_partitions(
    db: aiosqlite.Connection, cutoff_month_start: datetime
) -> list[tuple[str, int, int]]:
    """Distinct (monitor_id, year, month) with raw rows strictly before cutoff.

    Cutoff is the *start* of the first month we still want to keep locally,
    so any row with `checked_at < cutoff_month_start` belongs in S3.
    """
    cutoff_iso = cutoff_month_start.isoformat()
    async with db.execute(
        """SELECT DISTINCT
                cr.monitor_id AS monitor_id,
                CAST(strftime('%Y', cr.checked_at) AS INTEGER) AS y,
                CAST(strftime('%m', cr.checked_at) AS INTEGER) AS m
            FROM check_results cr
            JOIN monitors mo ON mo.id = cr.monitor_id
            WHERE cr.checked_at < ?
            ORDER BY cr.monitor_id, y, m""",
        (cutoff_iso,),
    ) as cur:
        rows = await cur.fetchall()
    return [(r["monitor_id"], r["y"], r["m"]) for r in rows]


async def _is_already_logged(
    db: aiosqlite.Connection, monitor_id: str, year_month: str
) -> bool:
    async with db.execute(
        "SELECT 1 FROM check_results_archive_log WHERE monitor_id = ? AND year_month = ?",
        (monitor_id, year_month),
    ) as cur:
        return (await cur.fetchone()) is not None


async def _delete_partition_rows(
    db: aiosqlite.Connection,
    monitor_id: str,
    month_start_iso: str,
    month_end_iso: str,
) -> int:
    """Delete raw rows in `[month_start, month_end)` for `monitor_id`."""
    cur = await db.execute(
        """DELETE FROM check_results
            WHERE monitor_id = ? AND checked_at >= ? AND checked_at < ?""",
        (monitor_id, month_start_iso, month_end_iso),
    )
    return cur.rowcount or 0


async def _archive_one_partition(
    db: aiosqlite.Connection,
    s3_client,
    bucket: str,
    prefix: str,
    monitor_id: str,
    year: int,
    month: int,
) -> tuple[int, int, int] | None:
    """Upload + log + delete one (monitor, month) partition.

    Returns (rows_uploaded, bytes_uploaded, rows_deleted) on success, or None
    if there was nothing to upload (race with another process / empty range).
    """
    month_start = datetime(year, month, 1, tzinfo=timezone.utc)
    month_end = _next_month(month_start)
    start_iso = month_start.isoformat()
    end_iso = month_end.isoformat()
    year_month = f"{year:04d}-{month:02d}"

    # Pull rows in deterministic order so `sha256` is stable across reruns of
    # the same partition — useful for the integrity check on restore.
    async with db.execute(
        """SELECT id, monitor_id, status, status_code, response_time_ms,
                  error, checked_at
            FROM check_results
            WHERE monitor_id = ? AND checked_at >= ? AND checked_at < ?
            ORDER BY checked_at, id""",
        (monitor_id, start_iso, end_iso),
    ) as cur:
        rows = await cur.fetchall()

    if not rows:
        return None

    payload, sha256_hex, row_count = _build_gzip_jsonl(rows)
    key = _partition_key(prefix, monitor_id, year, month)
    md5_b64 = base64.b64encode(hashlib.md5(payload).digest()).decode("ascii")

    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=payload,
        ContentType="application/gzip",
        ContentEncoding="gzip",
        ContentMD5=md5_b64,
        Metadata={
            "monitor_id": monitor_id,
            "year_month": year_month,
            "row_count": str(row_count),
            "sha256": sha256_hex,
        },
    )

    # Log first, delete second. If we crash between the two on the next run
    # we'll see the log row and run the delete via the
    # `_delete_only_for_logged_partition` branch.
    await db.execute(
        """INSERT INTO check_results_archive_log
                (monitor_id, year_month, s3_bucket, s3_key, row_count,
                 bytes_uploaded, sha256, archived_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            monitor_id,
            year_month,
            bucket,
            key,
            row_count,
            len(payload),
            sha256_hex,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    deleted = await _delete_partition_rows(db, monitor_id, start_iso, end_iso)
    await db.commit()

    logger.info(
        "Archived %s/%s rows=%d bytes=%d sha256=%s",
        monitor_id,
        year_month,
        row_count,
        len(payload),
        sha256_hex[:12],
    )
    return row_count, len(payload), deleted


async def _delete_only_for_logged_partition(
    db: aiosqlite.Connection,
    monitor_id: str,
    year: int,
    month: int,
) -> int:
    """Recovery branch: log row exists but rows still present → delete.

    Triggered when a previous run uploaded + logged but crashed before the
    delete committed. We trust the log row and reclaim the local space.
    """
    month_start = datetime(year, month, 1, tzinfo=timezone.utc)
    month_end = _next_month(month_start)
    deleted = await _delete_partition_rows(
        db, monitor_id, month_start.isoformat(), month_end.isoformat()
    )
    if deleted:
        await db.commit()
        logger.warning(
            "Recovered %s/%04d-%02d: deleted %d rows already covered by archive log",
            monitor_id,
            year,
            month,
            deleted,
        )
    return deleted


async def archive_old_check_results(
    db: aiosqlite.Connection,
    *,
    now: datetime | None = None,
    bucket: str | None = None,
    prefix: str | None = None,
    archive_after_days: int | None = None,
    s3_client=None,
) -> ArchiveRunSummary:
    """Archive raw check_results older than the cutoff.

    Operates on whole UTC months. The "cutoff month" is the month containing
    `(now - archive_after_days)`; everything *strictly before* that month is
    eligible. Partial-month rows stay local until the month closes, so a
    rerun the next day picks them up cleanly.

    Returns a summary so callers (CLI / tests) can assert on counts.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    days = ARCHIVE_AFTER_DAYS if archive_after_days is None else archive_after_days
    bucket = bucket if bucket is not None else ARCHIVE_S3_BUCKET
    prefix = prefix if prefix is not None else ARCHIVE_S3_PREFIX

    summary = ArchiveRunSummary(0, 0, 0, 0, 0)
    if not bucket:
        logger.info("Archiver disabled (ARCHIVE_S3_BUCKET unset)")
        return summary

    cutoff_month_start = _month_floor(now - timedelta(days=days))
    candidates = await _candidate_partitions(db, cutoff_month_start)
    if not candidates:
        return summary

    if s3_client is None:
        s3_client = _build_s3_client()

    partitions = 0
    rows_up = 0
    rows_del = 0
    bytes_up = 0
    skipped = 0
    for monitor_id, year, month in candidates:
        year_month = f"{year:04d}-{month:02d}"
        if await _is_already_logged(db, monitor_id, year_month):
            skipped += 1
            # Crash-recovery: log exists but raw rows are still here. Delete
            # them so we converge on the documented invariant ("log row
            # present ⇒ no local rows in that range").
            recovered = await _delete_only_for_logged_partition(db, monitor_id, year, month)
            rows_del += recovered
            continue
        result = await _archive_one_partition(
            db, s3_client, bucket, prefix, monitor_id, year, month
        )
        if result is None:
            continue
        n_rows, n_bytes, n_deleted = result
        partitions += 1
        rows_up += n_rows
        bytes_up += n_bytes
        rows_del += n_deleted

    return ArchiveRunSummary(
        partitions_uploaded=partitions,
        rows_uploaded=rows_up,
        rows_deleted=rows_del,
        bytes_uploaded=bytes_up,
        skipped_already_logged=skipped,
    )
