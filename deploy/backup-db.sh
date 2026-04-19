#!/usr/bin/env bash
# SQLite backup script for Pingback
# Uses SQLite online backup API (safe for WAL mode)
# Keeps last 7 daily backups
set -euo pipefail

DB_PATH="/opt/pingback/data/pingback.db"
BACKUP_DIR="/opt/pingback/backups"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
MAX_BACKUPS=7

mkdir -p "$BACKUP_DIR"

# Use SQLite .backup command for a consistent snapshot
sqlite3 "$DB_PATH" ".backup '$BACKUP_DIR/pingback-$TIMESTAMP.db'"

# Compress the backup
gzip "$BACKUP_DIR/pingback-$TIMESTAMP.db"

# Remove old backups beyond retention
ls -1t "$BACKUP_DIR"/pingback-*.db.gz 2>/dev/null | tail -n +$((MAX_BACKUPS + 1)) | xargs -r rm -f

echo "Backup complete: $BACKUP_DIR/pingback-$TIMESTAMP.db.gz"
