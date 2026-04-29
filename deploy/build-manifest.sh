#!/usr/bin/env bash
# Emit a deploy MANIFEST listing every file shipped to a Pingback host
# from this git checkout, with md5, source path, and the deploy commit SHA.
#
# Usage:
#   deploy/build-manifest.sh                  # prints to stdout
#   deploy/build-manifest.sh > MANIFEST.txt   # capture for scp to prod
#
# Run from the repo root. /opt/pingback is NOT a git checkout, so prod has
# no in-tree audit trail; this MANIFEST is the source of truth for what's
# on the box. Copy the result to /opt/pingback/deploy/MANIFEST.txt at every
# deploy.

set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

commit=$(git rev-parse --short=7 HEAD)
commit_full=$(git rev-parse HEAD)
clean="clean"
if ! git diff --quiet HEAD --; then
    clean="DIRTY"
fi
ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
host=${DEPLOY_HOST:-unknown-host}
deployer=${DEPLOY_USER:-$(git config user.email 2>/dev/null || echo unknown)}

md5_of() {
    if command -v md5sum >/dev/null 2>&1; then
        md5sum "$1" | awk '{print $1}'
    else
        md5 -q "$1"
    fi
}

emit() {
    local src="$1" dest="$2"
    if [ ! -f "$src" ]; then
        echo "MISSING-SOURCE $dest <- $src" >&2
        return
    fi
    printf "%s  %s  %s\n" "$(md5_of "$src")" "$dest" "$src"
}

# Header
cat <<HDR
# Pingback deploy MANIFEST
# Generated: $ts
# Commit:    $commit_full ($commit, $clean)
# Host:      $host
# Deployer:  $deployer
#
# Format: <md5>  <dest_path_on_host>  <source_path_in_repo>
# Sorted by destination path.
HDR

{
    # 1. App package: pingback/** -> /opt/pingback/pingback/**
    while IFS= read -r f; do
        emit "$f" "/opt/pingback/$f"
    done < <(git ls-files pingback)

    # 2. Deploy artifacts mirrored into /opt/pingback/deploy/
    while IFS= read -r f; do
        emit "$f" "/opt/pingback/$f"
    done < <(git ls-files deploy | grep -Ev '\.md$')

    # 3. One-off scripts copied to /opt/pingback/scripts/
    while IFS= read -r f; do
        emit "$f" "/opt/pingback/$f"
    done < <(git ls-files scripts | grep -Ev '\.md$')

    # 4. System files installed outside /opt/pingback.
    emit deploy/pingback.service          /etc/systemd/system/pingback.service
    emit deploy/pingback-backup.service   /etc/systemd/system/pingback-backup.service
    emit deploy/pingback-backup.timer     /etc/systemd/system/pingback-backup.timer
    emit deploy/backup-db.sh              /usr/local/bin/pingback-backup.sh
    emit deploy/restore-db.sh             /usr/local/bin/pingback-restore.sh

    # nginx config is rendered from a template at setup time, so the on-host
    # /etc/nginx/conf.d/pingback.conf cannot be md5-matched against a single
    # source. We list only the template here so audits can confirm the
    # rendered file derives from this revision.
    emit deploy/nginx-pingback.conf.template /etc/nginx/conf.d/pingback.conf.template
} | sort -k2,2
