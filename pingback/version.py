"""Resolve the running build's git sha for /healthz and X-Pingback-Version.

Resolution order:
1. ``PINGBACK_VERSION`` env var (deploy script sets this).
2. ``RELEASE_SHA`` file at the package root or one level up (release dir).
3. ``git rev-parse --short HEAD`` against the package directory (dev mode).
4. ``unknown``.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _read_release_sha_file() -> str | None:
    pkg_dir = Path(__file__).resolve().parent
    candidates = [
        pkg_dir.parent / "RELEASE_SHA",
        pkg_dir / "RELEASE_SHA",
    ]
    for path in candidates:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return None


def _git_short_sha() -> str | None:
    pkg_dir = Path(__file__).resolve().parent
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(pkg_dir),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    sha = out.stdout.strip()
    return sha or None


def _resolve_version() -> str:
    env = os.environ.get("PINGBACK_VERSION", "").strip()
    if env:
        return env
    file_sha = _read_release_sha_file()
    if file_sha:
        return file_sha
    git_sha = _git_short_sha()
    if git_sha:
        return git_sha
    return "unknown"


VERSION: str = _resolve_version()
