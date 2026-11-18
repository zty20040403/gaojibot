"""Build-time commit identity exposed to /status and self-inspection."""

from __future__ import annotations

import os
from pathlib import Path

from .handler_constants import BOT_VERSION

_COMMIT_FILE = Path(__file__).resolve().parents[3] / ".gaoji-commit"


def build_commit() -> str:
    """Return the build commit from GAOJI_COMMIT or the packaged commit marker."""
    env_commit = os.environ.get("GAOJI_COMMIT", "").strip()
    if env_commit:
        return env_commit
    try:
        file_commit = _COMMIT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        file_commit = ""
    return file_commit or "unknown"


def short_commit() -> str:
    """Return a concise commit identifier for user-facing status output."""
    commit = build_commit()
    return commit[:7] if len(commit) > 7 and not commit.endswith("-dirty") else (
        commit[:13] if commit.endswith("-dirty") and len(commit) > 13 else commit
    )


def version_label() -> str:
    """Return version + short build commit label."""
    return f"{BOT_VERSION} ({short_commit()})"
