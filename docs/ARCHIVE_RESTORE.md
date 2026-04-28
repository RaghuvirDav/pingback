# check_results S3 archive — operations + restore (MAK-149)

The archiver moves raw `check_results` rows older than 90 days off the EC2
SQLite database and into S3. Rollups (1m / 5m / 1h) keep dashboards fast for
the cold tail; raw rows are only needed for forensic export. This runbook
covers the operational shape of the system and how to restore.

## What gets stored where

- Bucket: `$ARCHIVE_S3_BUCKET` (typically `pingback-backups-prod`,
  `us-east-1`).
- Key layout (Hive-style — Athena/Glue can read it directly):

  ```
  archive/check_results/monitor=<monitor_id>/year=<YYYY>/<YYYY>-<MM>.jsonl.gz
  ```

- One object per `(monitor_id, year_month)`. Object body is gzipped JSONL,
  one record per line:

  ```json
  {"id":"…","monitor_id":"…","status":"up","status_code":200,"response_time_ms":142,"error":null,"checked_at":"2026-01-12T08:14:33+00:00"}
  ```

- Object metadata holds `monitor_id`, `year_month`, `row_count`, `sha256`.
- Local table `check_results_archive_log(monitor_id, year_month, …)` is the
  authoritative record of what has been uploaded; presence of a row means
  the partition is in S3 **and** the local raw rows have been deleted.

## Daily operation

- `pingback-archive.timer` fires at `04:15 UTC` (after the `03:30 UTC`
  sqlite backup), running `scripts/archive_check_results.py`.
- The job is a no-op when `ARCHIVE_S3_BUCKET` is empty (default for dev /
  staging) or when no rows are old enough.
- Crash safety: if a run uploads + logs but crashes before deleting local
  rows, the next run sees the log row and just runs the delete. If a run
  uploads but never logs, the next run re-uploads to the same key — content
  is byte-stable for the same input (gzip `mtime=0`), so this is harmless.

## Cost estimate (per BUSINESS user)

Rough numbers for sizing the line item — confirm with CloudWatch billing
once we have real subscribers.

- Workload: 100 monitors × 30 s cadence × 1 yr → ~9 months of cold data
  after the 90-day local window.
- ~86k checks / monitor / month. Each gzipped JSONL record ≈ 25 bytes →
  ~2.1 MB / partition.
- 100 monitors × 9 months ≈ 900 partitions ≈ ~1.9 GB / BUSINESS user.
- S3 Standard storage: 1.9 GB × $0.023 = **~$0.05 / mo** (plus ~3000
  PUT / month at $0.005 / 1000 = **~$0.015 / mo**).
- Aggregate budget assumed in [MAK-144]: **~$2 / mo / BUSINESS user**,
  which leaves headroom for retrieval traffic and occasional Athena scans.

## How to restore one partition

1. List partitions for a monitor:

   ```bash
   aws s3 ls "s3://$ARCHIVE_S3_BUCKET/archive/check_results/monitor=<monitor_id>/" \
     --recursive
   ```

2. Pick the file you want (e.g. `…/2026-01.jsonl.gz`) and run the restore
   helper. It downloads the object, verifies the SHA-256 in the object
   metadata, and re-inserts rows with `INSERT OR IGNORE` so a partial
   restore can be re-run safely.

   ```bash
   /opt/pingback/venv/bin/python \
     /opt/pingback/scripts/restore_check_results.py \
     --key "archive/check_results/monitor=<monitor_id>/year=2026/2026-01.jsonl.gz"
   ```

3. (Optional) drop the matching `check_results_archive_log` row if you
   want subsequent archive runs to re-process the now-restored rows:

   ```sql
   DELETE FROM check_results_archive_log
       WHERE monitor_id = '<monitor_id>' AND year_month = '2026-01';
   ```

   Skip this if you only want a temporary read-back — the rows will sit
   alongside the archive and the next purge / archive cycle will leave them
   alone unless they re-enter the eligible window.

## Restore from a local file (no S3 round-trip)

Useful when AWS creds are unavailable or you copied a file manually:

```bash
python scripts/restore_check_results.py --file /tmp/2026-01.jsonl.gz
```

You can pass `--expected-sha256 <hex>` to verify against a digest you
already trust (e.g. from the archive_log row).

## Verifying integrity without restoring

```bash
aws s3api head-object \
  --bucket "$ARCHIVE_S3_BUCKET" \
  --key "archive/check_results/monitor=<monitor_id>/year=2026/2026-01.jsonl.gz"
```

The `Metadata.sha256` and `Metadata.row_count` fields match what we wrote
at archive time. The dashboard "Export raw" feature (future work) will
verify these before serving a signed URL.

## Disabling the archiver

Leave `ARCHIVE_S3_BUCKET` empty in `/opt/pingback/.env`. The CLI prints
"Archiver disabled" and exits 0 without touching S3 or sqlite.
