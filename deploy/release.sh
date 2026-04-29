#!/usr/bin/env bash
# Atomic release deploy for Pingback (MAK-179, Phase 1).
#
# Layout:
#   /opt/pingback/releases/<sha>/   <- code + venv for this release
#   /opt/pingback/current           -> symlink to the active release
#   /opt/pingback/.env              <- shared, NOT inside release dir
#   /opt/pingback/data/pingback.db  <- shared, NOT inside release dir
#
# Usage:
#   sudo deploy/release.sh <tarball> <git-sha>
#
# The tarball must contain the project tree at its root (pingback/,
# requirements.txt, deploy/, ...). Build it locally with e.g.
#   git archive --format=tar.gz -o /tmp/pingback-$(git rev-parse --short HEAD).tar.gz HEAD
#
# Health check: after the symlink swap and `systemctl reload pingback` we
# poll /healthz for up to 30s and require the running version to match the
# new sha. On failure we auto-rollback the symlink.
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <tarball> <git-sha>" >&2
  exit 64
fi

TARBALL="$1"
SHA="$2"

if [[ $EUID -ne 0 ]]; then
  echo "release.sh must run as root (need to chown to pingback)." >&2
  exit 77
fi
if [[ ! -f "$TARBALL" ]]; then
  echo "tarball not found: $TARBALL" >&2
  exit 66
fi
if [[ ! "$SHA" =~ ^[0-9a-f]{7,40}$ ]]; then
  echo "sha must be a 7-40 char git short/long hash, got: $SHA" >&2
  exit 65
fi

APP_ROOT="/opt/pingback"
RELEASES_DIR="$APP_ROOT/releases"
CURRENT_LINK="$APP_ROOT/current"
RELEASE_DIR="$RELEASES_DIR/$SHA"
ENV_FILE="$APP_ROOT/.env"
DATA_DIR="$APP_ROOT/data"
APP_USER="pingback"
APP_GROUP="pingback"
HEALTHZ_URL="${HEALTHZ_URL:-http://127.0.0.1:8000/healthz}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-30}"
KEEP_RELEASES="${KEEP_RELEASES:-5}"

log() { printf '[release %s] %s\n' "$SHA" "$*"; }

mkdir -p "$RELEASES_DIR"

# Capture the release we are replacing so we can revert on health failure.
PREV_TARGET=""
if [[ -L "$CURRENT_LINK" ]]; then
  PREV_TARGET="$(readlink -f "$CURRENT_LINK" || true)"
fi

if [[ -e "$RELEASE_DIR" ]]; then
  if [[ "${FORCE:-0}" != "1" ]]; then
    echo "release dir already exists: $RELEASE_DIR (set FORCE=1 to overwrite)" >&2
    exit 73
  fi
  rm -rf "$RELEASE_DIR"
fi

log "unpacking $TARBALL -> $RELEASE_DIR"
mkdir -p "$RELEASE_DIR"
tar -xzf "$TARBALL" -C "$RELEASE_DIR"

# Stamp the sha so /healthz + X-Pingback-Version can read it without git.
echo "$SHA" > "$RELEASE_DIR/RELEASE_SHA"

# Shared paths: .env stays at /opt/pingback/.env (per project memory rule
# root:pingback 640) and the SQLite DB lives outside the release dir.
ln -sfn "$ENV_FILE" "$RELEASE_DIR/.env"
ln -sfn "$DATA_DIR" "$RELEASE_DIR/data"

# Build the release venv. Seed from previous release with hardlinks (cheap
# on the same fs) so unchanged wheels don't re-download. We deliberately do
# NOT run `python -m venv --upgrade` over the seeded venv — `cp -al`
# preserves the bin/python symlink that points at the system interpreter,
# and `--upgrade --copies` then trips over its own symlink ("source and
# destination are the same file"). pyvenv.cfg already encodes `home =
# /usr/bin`, which is unchanged by relocating the venv, so direct binary
# invocation (e.g. `venv/bin/python`) works from any release path.
PY_BIN="python3.11"
command -v "$PY_BIN" >/dev/null 2>&1 || PY_BIN="python3"
if [[ -n "$PREV_TARGET" && -d "$PREV_TARGET/venv" ]]; then
  log "seeding venv from $PREV_TARGET/venv"
  cp -al "$PREV_TARGET/venv" "$RELEASE_DIR/venv"
else
  log "creating fresh venv"
  "$PY_BIN" -m venv "$RELEASE_DIR/venv"
fi
"$RELEASE_DIR/venv/bin/pip" install --quiet --upgrade pip
"$RELEASE_DIR/venv/bin/pip" install --quiet --upgrade-strategy only-if-needed -r "$RELEASE_DIR/requirements.txt"

chown -R "$APP_USER:$APP_GROUP" "$RELEASE_DIR"

# Preflight: import the app under the service user with the release's venv.
# This catches missing deps / syntax errors BEFORE we swap the symlink.
log "preflight import"
sudo -u "$APP_USER" \
  PINGBACK_VERSION="$SHA" \
  PYTHONPATH="$RELEASE_DIR" \
  "$RELEASE_DIR/venv/bin/python" -c "import pingback.main; print('preflight ok')"

# Atomic symlink swap. `ln -sfn` calls rename(2) on Linux which is atomic
# on the same filesystem. After the swap, systemd's WorkingDirectory and
# ExecStart resolve through /opt/pingback/current to the new release.
log "atomic swap: $CURRENT_LINK -> $RELEASE_DIR"
ln -sfn "$RELEASE_DIR" "$CURRENT_LINK"

# Persist the prior target so rollback.sh can find it without scanning.
if [[ -n "$PREV_TARGET" && "$PREV_TARGET" != "$RELEASE_DIR" ]]; then
  echo "$PREV_TARGET" > "$RELEASES_DIR/.previous"
fi

# Graceful reload. SIGHUP -> gunicorn forks new workers from the new
# release dir, then drains the old workers. nginx proxy_next_upstream
# absorbs any in-flight error.
log "systemctl reload pingback"
systemctl reload pingback

# Poll /healthz until it reports the new sha or HEALTH_TIMEOUT elapses.
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
while (( $(date +%s) < deadline )); do
  body=$(curl -fsS --max-time 2 "$HEALTHZ_URL" 2>/dev/null || true)
  if [[ -n "$body" ]] && grep -q "\"version\"\s*:\s*\"$SHA\"" <<<"$body"; then
    log "healthz ok at version $SHA"
    break
  fi
  sleep 1
done
body=$(curl -fsS --max-time 2 "$HEALTHZ_URL" 2>/dev/null || true)
if ! grep -q "\"version\"\s*:\s*\"$SHA\"" <<<"$body"; then
  log "FAIL: /healthz did not flip to $SHA within ${HEALTH_TIMEOUT}s. body=$body"
  if [[ -n "$PREV_TARGET" && -d "$PREV_TARGET" ]]; then
    log "auto-rollback to $PREV_TARGET"
    ln -sfn "$PREV_TARGET" "$CURRENT_LINK"
    systemctl reload pingback || true
  fi
  exit 75
fi

# Prune old release dirs (keep the N most recent, plus whatever current
# points to even if it is older than the cutoff).
mapfile -t OLD < <(ls -1dt "$RELEASES_DIR"/*/ 2>/dev/null | tail -n +"$((KEEP_RELEASES + 1))")
for old in "${OLD[@]:-}"; do
  [[ -z "$old" ]] && continue
  resolved="$(realpath "$old")"
  if [[ "$resolved" == "$(readlink -f "$CURRENT_LINK")" ]]; then
    continue
  fi
  log "pruning old release $resolved"
  rm -rf -- "$resolved"
done

log "release done"
