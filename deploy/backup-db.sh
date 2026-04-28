#!/usr/bin/env bash
# Online sqlite3 .backup of the Pingback prod DB.
#
#   /opt/pingback/backups/daily/pingback-YYYYMMDD-HHMMSS.db.gz
#   /opt/pingback/backups/daily/pingback-YYYYMMDD-HHMMSS.sha256
#
# Sunday runs are also hard-linked into weekly/.
#
# Retention: 14 dailies, 8 weeklies. Older files are pruned.
#
# Optional: set BACKUP_HEARTBEAT_URL in /opt/pingback/.env (or the
# environment) to a UptimeRobot heartbeat URL. The script POSTs to it on
# success so a missing nightly run will page us.
#
# Optional off-box copy: set BACKUP_S3_BUCKET (and optional
# BACKUP_S3_PREFIX, default "backups/daily") in /opt/pingback/.env to also
# upload the .db.gz + .sha256 to S3. Requires AWS_ACCESS_KEY_ID /
# AWS_SECRET_ACCESS_KEY / AWS_DEFAULT_REGION in the same .env (or in the
# service environment). S3 upload failure is non-fatal — the local copy
# remains the canonical artifact.
set -euo pipefail

DB_PATH="${DB_PATH:-/opt/pingback/data/pingback.db}"
BACKUP_ROOT="${BACKUP_ROOT:-/opt/pingback/backups}"
DAILY_DIR="$BACKUP_ROOT/daily"
WEEKLY_DIR="$BACKUP_ROOT/weekly"
DAILY_RETENTION="${DAILY_RETENTION:-14}"
WEEKLY_RETENTION="${WEEKLY_RETENTION:-8}"

read_env() {
  # read_env KEY → echoes the last-defined KEY=value from /opt/pingback/.env
  local key="$1"
  [[ -r /opt/pingback/.env ]] || return 0
  grep -E "^${key}=" /opt/pingback/.env 2>/dev/null \
    | tail -n1 | cut -d= -f2- | tr -d '"' || true
}

HEARTBEAT_URL="${BACKUP_HEARTBEAT_URL:-$(read_env BACKUP_HEARTBEAT_URL)}"
S3_BUCKET="${BACKUP_S3_BUCKET:-$(read_env BACKUP_S3_BUCKET)}"
S3_PREFIX="${BACKUP_S3_PREFIX:-$(read_env BACKUP_S3_PREFIX)}"
S3_PREFIX="${S3_PREFIX:-backups/daily}"

mkdir -p "$DAILY_DIR" "$WEEKLY_DIR"

TIMESTAMP=$(date -u +%Y%m%d-%H%M%S)
TARGET_DB="$DAILY_DIR/pingback-$TIMESTAMP.db"
TARGET_GZ="$TARGET_DB.gz"
TARGET_SHA="$DAILY_DIR/pingback-$TIMESTAMP.sha256"

# Online consistent snapshot (safe for WAL).
sqlite3 "$DB_PATH" ".backup '$TARGET_DB'"

# Quick integrity check before we keep the file.
if ! sqlite3 "$TARGET_DB" 'PRAGMA integrity_check;' | grep -qx 'ok'; then
  rm -f "$TARGET_DB"
  echo "backup-db: integrity_check failed for $TARGET_DB" >&2
  exit 2
fi

gzip -n "$TARGET_DB"
( cd "$DAILY_DIR" && sha256sum "$(basename "$TARGET_GZ")" > "$(basename "$TARGET_SHA")" )

# Sunday → also publish into weekly tier (hardlink, cheap).
if [[ "$(date -u +%u)" == "7" ]]; then
  ln -f "$TARGET_GZ"  "$WEEKLY_DIR/$(basename "$TARGET_GZ")"
  ln -f "$TARGET_SHA" "$WEEKLY_DIR/$(basename "$TARGET_SHA")"
fi

prune() {
  local dir="$1" keep="$2"
  shopt -s nullglob
  local files=("$dir"/pingback-*.db.gz)
  shopt -u nullglob
  (( ${#files[@]} > keep )) || return 0
  printf '%s\n' "${files[@]}" \
    | xargs -r ls -1t \
    | tail -n +"$((keep + 1))" \
    | while read -r gz; do
        rm -f "$gz" "${gz%.db.gz}.sha256"
      done
}
prune "$DAILY_DIR"  "$DAILY_RETENTION"
prune "$WEEKLY_DIR" "$WEEKLY_RETENTION"

SIZE=$(stat -c%s "$TARGET_GZ")
HASH=$(awk '{print $1}' "$TARGET_SHA")

S3_OK="false"
S3_URI=""
if [[ -n "$S3_BUCKET" ]]; then
  if [[ -z "${AWS_ACCESS_KEY_ID:-}" && -r /opt/pingback/.env ]]; then
    set -a
    # shellcheck disable=SC1090
    . <(grep -E '^(AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_DEFAULT_REGION|AWS_REGION)=' /opt/pingback/.env)
    set +a
  fi
  S3_URI="s3://$S3_BUCKET/$S3_PREFIX/$(basename "$TARGET_GZ")"
  S3_SHA_URI="s3://$S3_BUCKET/$S3_PREFIX/$(basename "$TARGET_SHA")"
  if aws s3 cp --only-show-errors "$TARGET_GZ"  "$S3_URI" \
     && aws s3 cp --only-show-errors "$TARGET_SHA" "$S3_SHA_URI"; then
    S3_OK="true"
    echo "backup-db: s3 ok $S3_URI"
  else
    echo "backup-db: s3 upload failed (non-fatal — local copy retained)" >&2
  fi
fi

cat > "$BACKUP_ROOT/last_run.json" <<JSON
{"status":"ok","ts":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","file":"$(basename "$TARGET_GZ")","bytes":$SIZE,"sha256":"$HASH","s3_uploaded":$S3_OK,"s3_uri":"$S3_URI"}
JSON
rm -f "$BACKUP_ROOT/last_run.failed.json"

if [[ -n "$HEARTBEAT_URL" ]]; then
  curl -fsS --max-time 10 -X POST "$HEARTBEAT_URL" -o /dev/null \
    || echo "backup-db: heartbeat POST failed (non-fatal)" >&2
fi

echo "backup-db: ok $TARGET_GZ ($SIZE bytes)"
