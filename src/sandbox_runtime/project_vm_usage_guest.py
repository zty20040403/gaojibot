"""Read-only guest usage; no file reservations or dependency execution."""

import os
from pathlib import Path
import time

from .project_files import ProjectFiles
from .project_usage import rebuildable_bytes
from .project_volume_journal import ProjectVolumeError


def measure(gate, request, *, workspace=Path("/workspace")):
    if (
        not isinstance(request, dict)
        or set(request) != {"identity", "epoch", "rebuild_command"}
        or not isinstance(request["identity"], dict)
        or set(request["identity"]) != set(gate.identity)
        or any(
            type(request["identity"][k]) is not type(v) or request["identity"][k] != v
            for k, v in gate.identity.items()
        )
        or not isinstance(request["rebuild_command"], str)
        or len(request["rebuild_command"]) > 4000
    ):
        raise ProjectVolumeError("Invalid guest accounting identity or request")
    with gate.observation(request["epoch"]) as receipt:
        rebuildable, warning = None, ""
        if receipt["busy"]:
            warning = "Guest writers are active; rebuildable usage is unknown"
        else:
            with ProjectFiles.root(workspace) as root:
                original = os.fstat(root)
                try:
                    rebuildable = rebuildable_bytes(
                        root, request["rebuild_command"], deadline=time.monotonic() + 5
                    )
                except (OSError, ValueError, ProjectVolumeError, RecursionError):
                    warning = "Rebuildable usage is unknown; this estimate authorizes no cleanup"
                final = workspace.stat()
                if (original.st_dev, original.st_ino) != (final.st_dev, final.st_ino):
                    raise ProjectVolumeError(
                        "Guest project mount changed during accounting"
                    )
        mounted = gate.workspace.verify()
        return {
            **receipt,
            "used_bytes": mounted["used_bytes"],
            "rebuildable_bytes": min(rebuildable, mounted["used_bytes"])
            if rebuildable is not None
            else None,
            "warning": warning,
        }
