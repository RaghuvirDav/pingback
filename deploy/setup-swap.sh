#!/usr/bin/env bash
# MAK-169: provision a 1 GB swap file on the EC2 host.
#
# 914 MB instance with 0 swap → a single allocation spike OOM-kills the app.
# Swap is the cheap insurance that converts a kill into thrashing. EBS gp3
# is fast enough that 1 GB rarely-paged swap is fine for our load profile.
#
# Idempotent: skips creation if /swapfile already exists and is on, and only
# appends to /etc/fstab if a /swapfile entry isn't already present.
#
# Run as root:  sudo ./deploy/setup-swap.sh
set -euo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "setup-swap: must run as root (sudo)" >&2
  exit 1
fi

SWAPFILE="${SWAPFILE:-/swapfile}"
SIZE_MB="${SIZE_MB:-1024}"
SWAPPINESS="${SWAPPINESS:-10}"

if swapon --show=NAME --noheadings | grep -qx "$SWAPFILE"; then
  echo "setup-swap: $SWAPFILE already active — skipping creation"
else
  if [[ -e "$SWAPFILE" ]]; then
    echo "setup-swap: $SWAPFILE exists but isn't enabled — re-enabling"
  else
    echo "setup-swap: creating $SWAPFILE (${SIZE_MB} MB)"
    if command -v fallocate >/dev/null 2>&1; then
      fallocate -l "${SIZE_MB}M" "$SWAPFILE"
    else
      dd if=/dev/zero of="$SWAPFILE" bs=1M count="$SIZE_MB" status=progress
    fi
    chmod 600 "$SWAPFILE"
    mkswap "$SWAPFILE"
  fi
  swapon "$SWAPFILE"
fi

# Persist across reboots.
if ! grep -qE "^\s*$SWAPFILE\s+" /etc/fstab; then
  echo "$SWAPFILE  none  swap  sw  0  0" >> /etc/fstab
  echo "setup-swap: appended /etc/fstab entry"
else
  echo "setup-swap: /etc/fstab entry already present"
fi

# Bias toward keeping the working set in RAM.
sysctl -w "vm.swappiness=${SWAPPINESS}" >/dev/null
if ! grep -qE '^vm\.swappiness' /etc/sysctl.conf 2>/dev/null; then
  echo "vm.swappiness=${SWAPPINESS}" >> /etc/sysctl.conf
fi

echo "==> Done."
free -m
swapon --show
