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
set -euo pipefail

DB_PATH="${DB_PATH:-/opt/pingback/data/pingback.db}"
BACKUP_ROOT="${BACKUP_ROOT:-/opt/pingback/backups}"
DAILY_DIR="$BACKUP_ROOT/daily"
WEEKLY_DIR="$BACKUP_ROOT/weekly"
DAILY_RETENTION="${DAILY_RETENTION:-14}"
WEEKLY_RETENTION="${WEEKLY_RETENTION:-8}"

if [[ -r /opt/pingback/.env ]]; then
  HEARTBEAT_URL=$(grep -E '^BACKUP_HEARTBEAT_URL=' /opt/pingback/.env 2>/dev/null \
    | tail -n1 | cut -d= -f2- | tr -d '"' || true)
fi
HEARTBEAT_URL="${BACKUP_HEARTBEAT_URL:-${HEARTBEAT_URL:-}}"

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
cat > "$BACKUP_ROOT/last_run.json" <<JSON
{"status":"ok","ts":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","file":"$(basename "$TARGET_GZ")","bytes":$SIZE,"sha256":"$HASH"}
JSON
rm -f "$BACKUP_ROOT/last_run.failed.json"

if [[ -n "$HEARTBEAT_URL" ]]; then
  curl -fsS --max-time 10 -X POST "$HEARTBEAT_URL" -o /dev/null \
    || echo "backup-db: heartbeat POST failed (non-fatal)" >&2
fi

echo "backup-db: ok $TARGET_GZ ($SIZE bytes)"
