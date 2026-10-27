from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerJobKind:
    safe_rerun: bool
    checkpointable: bool


WORKER_JOB_CATALOG = {
    "probe.http": WorkerJobKind(safe_rerun=True, checkpointable=True),
    "artifact.inspect": WorkerJobKind(safe_rerun=True, checkpointable=True),
    "document.verify": WorkerJobKind(safe_rerun=True, checkpointable=True),
    "media.inspect": WorkerJobKind(safe_rerun=True, checkpointable=True),
    "preview.static": WorkerJobKind(safe_rerun=True, checkpointable=True),
}

WORKER_JOB_KINDS = frozenset(WORKER_JOB_CATALOG)
