#!/usr/bin/env bash
# Restore a Pingback DB backup. Verifies sha256 then decompresses to a
# target path. Does NOT touch the live DB unless --target points at it
# and you have already stopped the service.
#
#   ./restore-db.sh <backup.db.gz> [--target /tmp/restore-test.db]
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <backup.db.gz> [--target PATH]" >&2
  exit 64
fi

SRC="$1"
TARGET="/tmp/pingback-restore-$(date -u +%Y%m%d-%H%M%S).db"
shift

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac
done

if [[ ! -f "$SRC" ]]; then
  echo "restore: backup file not found: $SRC" >&2
  exit 66
fi

SHA_FILE="${SRC%.db.gz}.sha256"
if [[ -f "$SHA_FILE" ]]; then
  ( cd "$(dirname "$SRC")" && sha256sum -c "$(basename "$SHA_FILE")" )
else
  echo "restore: no sha256 sidecar at $SHA_FILE — skipping checksum" >&2
fi

gunzip -c "$SRC" > "$TARGET"

if ! sqlite3 "$TARGET" 'PRAGMA integrity_check;' | grep -qx 'ok'; then
  echo "restore: integrity_check failed on $TARGET" >&2
  exit 2
fi

ROW_COUNT=$(sqlite3 "$TARGET" "SELECT COUNT(*) FROM sqlite_master WHERE type='table';")
echo "restore: ok target=$TARGET tables=$ROW_COUNT"
