"""Shared sandbox result types and identifiers."""
from __future__ import annotations

import re
from dataclasses import dataclass

from src.sandbox_runtime.errors import SandboxError as SandboxError

SANDBOX_ID_PATTERN = re.compile(r"^s[0-9a-f]{6}$")


@dataclass(frozen=True)
class SandboxResult:
    stdout: str
    stderr: str
    returncode: int
    manifest: "SandboxObservedManifest | None" = None


@dataclass(frozen=True)
class SandboxObservedManifest:
    command: str
    duration_ms: int
    stdout_sha256: str
    stdout_bytes: int
    stderr_sha256: str
    stderr_bytes: int
    changed_workspace_paths: tuple[str, ...]
    container_diff: tuple[str, ...]
    network_mode: str


@dataclass(frozen=True)
class SandboxExecutionActivity:
    activity_id: str
    sandbox_id: str
    owner: str
    command: str
    started_at: int
    status: str = "running"
    finished_at: int | None = None
    returncode: int | None = None
    error: str = ""
