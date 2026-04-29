#!/usr/bin/env bash
# Roll the /opt/pingback/current symlink back one release and reload the
# service (MAK-179). Designed to complete in well under 5s.
#
# Usage:
#   sudo deploy/rollback.sh                  # roll back to the prior release
#   sudo deploy/rollback.sh <release-sha>    # roll back to a specific release
#
# Strategy:
#   1. Determine target release dir.
#      - explicit arg wins
#      - else read /opt/pingback/releases/.previous
#      - else pick the second-most-recent dir under releases/
#   2. Atomic symlink swap.
#   3. systemctl reload pingback (gunicorn graceful worker reload).
#   4. Poll /healthz until version flips back.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "rollback.sh must run as root." >&2
  exit 77
fi

APP_ROOT="/opt/pingback"
RELEASES_DIR="$APP_ROOT/releases"
CURRENT_LINK="$APP_ROOT/current"
PREV_FILE="$RELEASES_DIR/.previous"
HEALTHZ_URL="${HEALTHZ_URL:-http://127.0.0.1:8000/healthz}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-15}"

log() { printf '[rollback] %s\n' "$*"; }

CURRENT_TARGET=""
if [[ -L "$CURRENT_LINK" ]]; then
  CURRENT_TARGET="$(readlink -f "$CURRENT_LINK" || true)"
fi

TARGET=""
if [[ $# -ge 1 ]]; then
  arg="$1"
  if [[ -d "$arg" ]]; then
    TARGET="$(readlink -f "$arg")"
  elif [[ -d "$RELEASES_DIR/$arg" ]]; then
    TARGET="$(readlink -f "$RELEASES_DIR/$arg")"
  else
    echo "no such release: $arg" >&2
    exit 66
  fi
elif [[ -r "$PREV_FILE" ]]; then
  TARGET="$(<"$PREV_FILE")"
fi

if [[ -z "$TARGET" || ! -d "$TARGET" || "$TARGET" == "$CURRENT_TARGET" ]]; then
  # Fallback: pick the most recent release dir that isn't current.
  while IFS= read -r dir; do
    resolved="$(realpath "$dir")"
    if [[ "$resolved" != "$CURRENT_TARGET" ]]; then
      TARGET="$resolved"
      break
    fi
  done < <(ls -1dt "$RELEASES_DIR"/*/ 2>/dev/null)
fi

if [[ -z "$TARGET" || ! -d "$TARGET" ]]; then
  echo "no rollback target available under $RELEASES_DIR" >&2
  exit 70
fi

if [[ "$TARGET" == "$CURRENT_TARGET" ]]; then
  echo "rollback target equals current ($TARGET); nothing to do" >&2
  exit 0
fi

TARGET_SHA="$(basename "$TARGET")"
log "swapping current -> $TARGET"
ln -sfn "$TARGET" "$CURRENT_LINK"

# Record the swap so a re-deploy can find what we just rolled away from.
if [[ -n "$CURRENT_TARGET" && "$CURRENT_TARGET" != "$TARGET" ]]; then
  echo "$CURRENT_TARGET" > "$PREV_FILE"
fi

# See release.sh for why this is `restart` not `reload`. Phase 1 single-
# process gunicorn cannot re-resolve its WorkingDirectory symlink on
# SIGHUP, so we fully re-exec the master to pick up the rolled-back code.
log "systemctl restart pingback"
systemctl restart pingback

deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
while (( $(date +%s) < deadline )); do
  body=$(curl -fsS --max-time 2 "$HEALTHZ_URL" 2>/dev/null || true)
  if [[ -n "$body" ]] && grep -q "\"version\"\s*:\s*\"$TARGET_SHA\"" <<<"$body"; then
    log "healthz ok at version $TARGET_SHA"
    exit 0
  fi
  sleep 1
done

body=$(curl -fsS --max-time 2 "$HEALTHZ_URL" 2>/dev/null || true)
log "FAIL: /healthz did not flip to $TARGET_SHA within ${HEALTH_TIMEOUT}s. body=$body"
exit 75
