"""MAK-149 — archive raw check_results to S3 after 90 days.

Direct DB tests with an in-memory fake S3 client. We assert on the
invariants the runbook promises: idempotent uploads, delete-after-upload,
crash-recovery, and restorability of the gzipped JSONL.
"""
from __future__ import annotations

import asyncio
import gzip
import io
import json
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from pingback.db.schema import initialize_database
from pingback.services import archiver


class FakeS3:
    """Minimal in-memory stand-in for boto3 s3 client.

    Records every PUT and exposes a `get_object`-shaped method so the
    restore script can be exercised without hitting real S3.
    """

    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.put_calls: list[dict] = []

    def put_object(self, **kwargs):
        # Mirror the boto3 contract: ContentMD5 is a base64-encoded MD5
        # of the body; if it doesn't match, real S3 would 400. We assert
        # equality so a regression in the upload path fails loudly.
        import base64
        import hashlib as _hashlib

        body = kwargs["Body"]
        expected = base64.b64encode(_hashlib.md5(body).digest()).decode("ascii")
        assert kwargs["ContentMD5"] == expected, "Content-MD5 must match body"
        key = (kwargs["Bucket"], kwargs["Key"])
        self.objects[key] = {
            "Body": body,
            "Metadata": kwargs.get("Metadata", {}),
            "ContentType": kwargs.get("ContentType"),
        }
        self.put_calls.append(kwargs)
        return {"ETag": '"%s"' % _hashlib.md5(body).hexdigest()}

    def get_object(self, **kwargs):
        key = (kwargs["Bucket"], kwargs["Key"])
        obj = self.objects[key]
        return {
            "Body": io.BytesIO(obj["Body"]),
            "Metadata": obj["Metadata"],
        }


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "archiver.db"


async def _open_db(path):
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await initialize_database(db)
    return db


async def _seed_user_and_monitor(db) -> tuple[str, str]:
    user_id = str(uuid.uuid4())
    monitor_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO users (id, email, plan) VALUES (?, ?, 'business')",
        (user_id, f"u-{user_id[:8]}@example.com"),
    )
    await db.execute(
        """INSERT INTO monitors (id, user_id, name, url, interval_seconds, status)
           VALUES (?, ?, 'M', 'https://example.com', 60, 'active')""",
        (monitor_id, user_id),
    )
    await db.commit()
    return user_id, monitor_id


async def _seed_check(db, monitor_id, *, when: datetime, status="up", latency=120):
    await db.execute(
        """INSERT INTO check_results
            (id, monitor_id, status, status_code, response_time_ms, error, checked_at)
           VALUES (?, ?, ?, ?, ?, NULL, ?)""",
        (str(uuid.uuid4()), monitor_id, status, 200 if status == "up" else 500, latency, when.isoformat()),
    )


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Helpers under test
# ---------------------------------------------------------------------------


def test_month_floor_handles_naive_and_aware():
    aware = datetime(2026, 4, 17, 12, 30, tzinfo=timezone.utc)
    assert archiver._month_floor(aware) == datetime(2026, 4, 1, tzinfo=timezone.utc)
    naive = datetime(2026, 4, 17, 12, 30)
    assert archiver._month_floor(naive) == datetime(2026, 4, 1, tzinfo=timezone.utc)


def test_next_month_rolls_over_december():
    assert archiver._next_month(datetime(2026, 12, 1, tzinfo=timezone.utc)) == datetime(
        2027, 1, 1, tzinfo=timezone.utc
    )
    assert archiver._next_month(datetime(2026, 4, 1, tzinfo=timezone.utc)) == datetime(
        2026, 5, 1, tzinfo=timezone.utc
    )


def test_partition_key_is_hive_style():
    key = archiver._partition_key("archive/check_results", "mon-1", 2026, 1)
    assert key == "archive/check_results/monitor=mon-1/year=2026/2026-01.jsonl.gz"


# ---------------------------------------------------------------------------
# End-to-end: upload + delete
# ---------------------------------------------------------------------------


def test_archiver_uploads_and_deletes_old_rows(db_path):
    async def _go():
        db = await _open_db(db_path)
        _, monitor_id = await _seed_user_and_monitor(db)
        # Pin "now" so the cutoff math is deterministic. now - 90d = Jan 29
        # → cutoff month = January 2026 → eligible = strictly before Jan 1.
        now = datetime(2026, 4, 29, tzinfo=timezone.utc)
        # Two old rows in Nov 2025, one old row in Dec 2025, one boundary row
        # inside Jan (cutoff month — must NOT be archived), one fresh April row.
        await _seed_check(db, monitor_id, when=datetime(2025, 11, 5, 8, 0, tzinfo=timezone.utc))
        await _seed_check(db, monitor_id, when=datetime(2025, 11, 20, 8, 0, tzinfo=timezone.utc))
        await _seed_check(db, monitor_id, when=datetime(2025, 12, 14, 8, 0, tzinfo=timezone.utc))
        await _seed_check(db, monitor_id, when=datetime(2026, 1, 10, 8, 0, tzinfo=timezone.utc))
        await _seed_check(db, monitor_id, when=datetime(2026, 4, 28, 8, 0, tzinfo=timezone.utc))
        await db.commit()

        s3 = FakeS3()
        summary = await archiver.archive_old_check_results(
            db, now=now, bucket="b", prefix="archive/check_results",
            archive_after_days=90, s3_client=s3,
        )

        assert summary.partitions_uploaded == 2
        assert summary.rows_uploaded == 3
        assert summary.rows_deleted == 3
        assert summary.skipped_already_logged == 0
        assert ("b", f"archive/check_results/monitor={monitor_id}/year=2025/2025-11.jsonl.gz") in s3.objects
        assert ("b", f"archive/check_results/monitor={monitor_id}/year=2025/2025-12.jsonl.gz") in s3.objects

        async with db.execute(
            "SELECT COUNT(*) AS n FROM check_results WHERE monitor_id = ?",
            (monitor_id,),
        ) as cur:
            row = await cur.fetchone()
        assert row["n"] == 2, "January (cutoff month) and April (fresh) rows must remain"

        async with db.execute(
            "SELECT year_month, row_count FROM check_results_archive_log "
            "WHERE monitor_id = ? ORDER BY year_month",
            (monitor_id,),
        ) as cur:
            log = await cur.fetchall()
        assert [(r["year_month"], r["row_count"]) for r in log] == [
            ("2025-11", 2), ("2025-12", 1),
        ]
        await db.close()

    _run(_go())


def test_archiver_is_idempotent_on_rerun(db_path):
    async def _go():
        db = await _open_db(db_path)
        _, monitor_id = await _seed_user_and_monitor(db)
        now = datetime(2026, 4, 29, tzinfo=timezone.utc)
        await _seed_check(db, monitor_id, when=datetime(2025, 12, 5, tzinfo=timezone.utc))
        await db.commit()

        s3 = FakeS3()
        first = await archiver.archive_old_check_results(
            db, now=now, bucket="b", prefix="p",
            archive_after_days=90, s3_client=s3,
        )
        assert first.partitions_uploaded == 1
        assert len(s3.put_calls) == 1

        # Second pass: nothing eligible, no new PUTs, no skipped rows
        # because the rows are already gone.
        second = await archiver.archive_old_check_results(
            db, now=now, bucket="b", prefix="p",
            archive_after_days=90, s3_client=s3,
        )
        assert second.partitions_uploaded == 0
        assert second.rows_uploaded == 0
        assert second.skipped_already_logged == 0
        assert len(s3.put_calls) == 1
        await db.close()

    _run(_go())


def test_archiver_recovers_when_log_row_exists_but_rows_remain(db_path):
    """Crash-recovery branch: log committed, delete didn't.

    We simulate by inserting the archive_log row by hand and leaving the
    raw rows in place. The next archive run should not re-upload (the log
    is authoritative) and should reclaim the local rows.
    """

    async def _go():
        db = await _open_db(db_path)
        _, monitor_id = await _seed_user_and_monitor(db)
        now = datetime(2026, 4, 29, tzinfo=timezone.utc)
        await _seed_check(db, monitor_id, when=datetime(2025, 12, 5, tzinfo=timezone.utc))
        await _seed_check(db, monitor_id, when=datetime(2025, 12, 9, tzinfo=timezone.utc))
        await db.execute(
            """INSERT INTO check_results_archive_log
                    (monitor_id, year_month, s3_bucket, s3_key, row_count,
                     bytes_uploaded, sha256, archived_at)
                VALUES (?, '2025-12', 'b', 'k', 2, 1, 'deadbeef', '2026-04-29T00:00:00+00:00')""",
            (monitor_id,),
        )
        await db.commit()

        s3 = FakeS3()
        summary = await archiver.archive_old_check_results(
            db, now=now, bucket="b", prefix="p",
            archive_after_days=90, s3_client=s3,
        )
        assert s3.put_calls == [], "log row exists; archiver must not re-upload"
        assert summary.skipped_already_logged == 1
        assert summary.rows_deleted == 2

        async with db.execute(
            "SELECT COUNT(*) AS n FROM check_results WHERE monitor_id = ?",
            (monitor_id,),
        ) as cur:
            row = await cur.fetchone()
        assert row["n"] == 0, "leftover rows from crashed run must be cleaned up"
        await db.close()

    _run(_go())


def test_archiver_noop_without_bucket(db_path):
    async def _go():
        db = await _open_db(db_path)
        _, monitor_id = await _seed_user_and_monitor(db)
        await _seed_check(db, monitor_id, when=datetime(2025, 12, 5, tzinfo=timezone.utc))
        await db.commit()

        summary = await archiver.archive_old_check_results(
            db, now=datetime(2026, 4, 29, tzinfo=timezone.utc),
            bucket="", prefix="p", archive_after_days=90, s3_client=FakeS3(),
        )
        assert summary == archiver.ArchiveRunSummary(0, 0, 0, 0, 0)

        async with db.execute("SELECT COUNT(*) AS n FROM check_results") as cur:
            row = await cur.fetchone()
        assert row["n"] == 1, "rows must NOT be deleted when archive is disabled"
        await db.close()

    _run(_go())


def test_archive_payload_round_trips_through_gzip_jsonl(db_path):
    """End-to-end: upload, then decode the bytes the same way restore does."""

    async def _go():
        db = await _open_db(db_path)
        _, monitor_id = await _seed_user_and_monitor(db)
        now = datetime(2026, 4, 29, tzinfo=timezone.utc)
        when = datetime(2025, 12, 5, 8, 0, tzinfo=timezone.utc)
        await _seed_check(db, monitor_id, when=when, status="up", latency=120)
        await _seed_check(db, monitor_id, when=when + timedelta(seconds=60), status="down", latency=999)
        await db.commit()

        s3 = FakeS3()
        await archiver.archive_old_check_results(
            db, now=now, bucket="b", prefix="archive/check_results",
            archive_after_days=90, s3_client=s3,
        )
        ((_, key), obj), = ((k, v) for k, v in s3.objects.items())
        decoded = []
        with gzip.GzipFile(fileobj=io.BytesIO(obj["Body"]), mode="rb") as gz:
            for line in gz:
                decoded.append(json.loads(line))
        assert {r["status"] for r in decoded} == {"up", "down"}
        assert all(r["monitor_id"] == monitor_id for r in decoded)
        # sha256 stored as metadata must match the bytes we got back —
        # this is what `restore_check_results.py` verifies before insert.
        import hashlib
        assert obj["Metadata"]["sha256"] == hashlib.sha256(obj["Body"]).hexdigest()
        assert obj["Metadata"]["row_count"] == "2"
        await db.close()

    _run(_go())
