from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parents[2]


def _directory_from_env(name: str, default: Path, environ: Mapping[str, str] | None = None) -> Path:
    value = (os.environ if environ is None else environ).get(name, "").strip()
    return Path(value).expanduser() if value else default


STATE_DIR = _directory_from_env("AI_STATE_DIR", PACKAGE_DIR / "assets")
CACHE_DIR = _directory_from_env("AI_CACHE_DIR", PROJECT_ROOT / ".cache")


def application_paths(environ: Mapping[str, str] | None = None) -> tuple[Path, Path, Path]:
    """Resolve one application from its configuration snapshot, without I/O."""
    environment = dict(os.environ if environ is None else environ)
    return (
        _directory_from_env("AI_STATE_DIR", PACKAGE_DIR / "assets", environment),
        _directory_from_env("AI_CACHE_DIR", PROJECT_ROOT / ".cache", environment),
        PROJECT_ROOT,
    )
