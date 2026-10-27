"""Fenced container bindings shared by hard and soft project storage."""

from pathlib import Path
import re
import secrets

from .project_volume_journal import ProjectVolumeError


class ProjectWorkspaceJournal:
    def workspace(
        self,
        owner,
        project_id,
        *,
        generation,
        allow_pending=False,
        maintenance=False,
        reservation=None,
    ):
        """Private local driver data. Never send this binding over the RPC socket."""
        with self._locked(owner, project_id) as (directory, record):
            self._generation(record, generation)
            if record is None or record.get("deleted"):
                raise ProjectVolumeError("Project workspace is unavailable")
            if record["pending"] and not allow_pending:
                raise ProjectVolumeError("Project workspace is undergoing maintenance")
            if not maintenance:
                self._require_reservation(record, reservation)
            return self._workspace_binding(directory, record, maintenance=maintenance)

    def begin_workspace(
        self, owner, project_id, *, generation, fence, action, image, reservation=None
    ):
        if type(fence) is not int or not 1 <= fence < 2**53:
            raise ProjectVolumeError("Invalid project operation fence")
        if action not in {"start", "stop", "remove"}:
            raise ProjectVolumeError("Unsupported project workspace operation")
        if not isinstance(image, str) or not re.fullmatch(
            r"(?:gaoji-app-sandbox:[a-f0-9]{64}|[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64})",
            image,
        ):
            raise ProjectVolumeError("A pinned project image is required")
        with self._locked(owner, project_id) as (directory, record):
            self._generation(record, generation)
            if record is None or record.get("deleted"):
                raise ProjectVolumeError("Project volume does not exist")
            self._require_reservation(record, reservation)
            binding = self._workspace_binding(directory, record, maintenance=True)
            workspace = record.get("workspace")
            recoverable = (
                workspace
                and workspace.get("boot_id")
                and workspace["boot_id"] != binding["boot_id"]
            )
            if action == "start" and not binding["mount_matches"] and not recoverable:
                raise ProjectVolumeError(
                    "Mount the project before starting a workspace"
                )
            operation = "workspace_" + action
            if fence == record["fence"]:
                intent = record["intent"]
                if intent["operation"] != operation:
                    raise ProjectVolumeError("Project fence was already used")
                # A restart may have a newer configured image; the persisted
                # operation still reconciles its original immutable image.
                self._begin(directory, record, operation, fence, intent["arguments"])
                return self._workspace_binding(directory, record, maintenance=True)
            if record["pending"] or fence < record["fence"]:
                raise ProjectVolumeError(
                    "Previous project operation must be reconciled first"
                )
            if action == "start" and not record.get("workspace"):
                info = Path(binding["path"]).stat()
                record["workspace"] = {
                    "sandbox_id": "s" + secrets.token_hex(3),
                    "token": secrets.token_hex(16),
                    "image": image,
                    "root_identity": f"{info.st_dev}:{info.st_ino}",
                    "boot_id": binding["boot_id"],
                }
            self._begin(
                directory,
                record,
                operation,
                fence,
                {"workspace": record.get("workspace")},
            )
            return self._workspace_binding(directory, record, maintenance=True)

    def finish_workspace(
        self, owner, project_id, *, generation, fence, action, reservation=None
    ):
        if (
            action not in {"start", "stop", "remove"}
            or type(fence) is not int
            or not 1 <= fence < 2**53
        ):
            raise ProjectVolumeError("Invalid workspace completion")
        with self._locked(owner, project_id) as (directory, record):
            self._generation(record, generation)
            self._require_reservation(record, reservation)
            if record is None or record.get("deleted") or record["fence"] != fence:
                raise ProjectVolumeError("Workspace operation was superseded")
            if record["intent"]["operation"] != "workspace_" + action:
                raise ProjectVolumeError("Workspace operation does not match journal")
            if not record["pending"]:
                return self._status(directory, record)
            if action == "remove":
                # The driver independently verified container absence. Also
                # reject any remaining (including stopped) external consumer.
                self._idle(self._paths(directory, record)[1])
                record["workspace"] = None
            return self._finish(directory, record)
