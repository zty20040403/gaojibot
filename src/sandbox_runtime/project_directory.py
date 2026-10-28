"""Fenced soft-quota workspaces inside a Desktop instance's private data volume.

No loop devices, filesystem mounts or caller-supplied host paths are used.
The controller maps only the exact project subdirectory into its sandbox.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid

from src.local_runtime.capacity import allocated_bytes
from src.local_runtime.files import read_json
from src.project_content import MAX_MANIFEST_BYTES
from .project_files import ProjectFiles
from .project_usage import rebuildable_bytes
from .project_volume_journal import MIB, ProjectVolumeError
from .project_browser_volume import ProjectHostVolumeJournal
from .project_workspace_journal import ProjectWorkspaceJournal
from .project_volumes import LinuxVolumeHost


class DesktopVolumeHost(LinuxVolumeHost):
    def __init__(self, *, data_root, volume, **kwargs):
        super().__init__(**kwargs)
        if not re.fullmatch(r"gaoji-desktop-[a-f0-9]{32}-data", volume):
            raise ProjectVolumeError("Invalid Desktop data volume")
        self.data_root, self.volume = Path(data_root).resolve(), volume

    def subpath(self, target):
        value = Path(target).relative_to(self.data_root).as_posix()
        if (
            not value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(c in value for c in ",\0\r\n")
        ):
            raise ProjectVolumeError("Invalid Desktop project subpath")
        return value

    def in_use(self, target):
        prefix = (self.docker, "--host", self.docker_host)
        volume = json.loads(
            self.run(*prefix, "volume", "inspect", self.volume, timeout=20)
        )
        if (
            len(volume) != 1
            or volume[0].get("Name") != self.volume
            or volume[0].get("Driver") != "local"
            or volume[0].get("Options")
            or not Path(volume[0].get("Mountpoint", "")).is_absolute()
        ):
            raise ProjectVolumeError("Desktop data volume is not local owned storage")
        engine_target = Path(volume[0]["Mountpoint"]) / self.subpath(target)
        ids = self.run(*prefix, "ps", "-aq", "--no-trunc", timeout=20).splitlines()
        if len(ids) > 2048 or any(
            not re.fullmatch(r"[a-f0-9]{64}", value) for value in ids
        ):
            raise ProjectVolumeError("Cannot verify Desktop consumers")
        if not ids:
            return False
        rows = json.loads(
            self.run(*prefix, "inspect", "--type", "container", *ids, timeout=20)
        )
        if {row["Id"] for row in rows} != set(ids):
            raise ProjectVolumeError("Incomplete Desktop consumer inventory")
        project = Path(self.subpath(target))
        for row in rows:
            for actual in row.get("Mounts", []):
                if actual.get("Type") == "bind":
                    source = Path(actual.get("Source", ""))
                    if source.is_absolute() and (
                        source == engine_target
                        or source.is_relative_to(engine_target)
                        or engine_target.is_relative_to(source)
                    ):
                        return True
                if actual.get("Type") != "volume" or actual.get("Name") != self.volume:
                    continue
                mounts = [
                    m
                    for m in row.get("HostConfig", {}).get("Mounts", [])
                    if m.get("Target") == actual.get("Destination")
                ]
                if len(mounts) > 1:
                    raise ProjectVolumeError("Ambiguous Desktop mount inventory")
                path = Path(
                    (mounts[0] if mounts else {})
                    .get("VolumeOptions", {})
                    .get("Subpath", ".")
                )
                if path != Path(".") and (
                    path == project
                    or path.is_relative_to(project)
                    or project.is_relative_to(path)
                ):
                    return True
                # The operator-owned controller/runner sees the whole data volume.
                # Other whole-volume consumers must block destructive maintenance.
                labels = row.get("Config", {}).get("Labels") or {}
                if path == Path("."):
                    settings = row.get("Config", {})
                    if (
                        labels.get("gaoji.desktop.job") != self.volume[14:-5]
                        or actual.get("Destination") != str(self.data_root)
                        or not re.fullmatch(
                            r"gaoji-desktop-controller:[a-f0-9]{64}",
                            settings.get("Image", ""),
                        )
                    ):
                        return True
        return False


class DirectoryProjectVolumes(ProjectWorkspaceJournal, ProjectHostVolumeJournal):
    storage_kind = "desktop-directory"
    quota_mode = "soft"

    def __init__(self, root, instance_id, *, host, capacity_check=None):
        super().__init__(root, instance_id, capacity_check=capacity_check)
        self.host = host

    @staticmethod
    def _paths(directory, record):
        identifier = record.get("directory_id", "")
        if not isinstance(identifier, str) or not re.fullmatch(
            r"[a-f0-9]{32}", identifier
        ):
            raise ProjectVolumeError("Invalid project directory identity")
        return None, directory / identifier

    def _target(self, directory, record):
        target = self._paths(directory, record)[1]
        info = target.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or record.get("root_identity") != [info.st_dev, info.st_ino]
            or info.st_uid != (1000 if os.geteuid() == 0 else os.geteuid())
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ProjectVolumeError(
                "Project directory identity or permissions changed"
            )
        return target

    def _idle(self, target):
        if self.host.in_use(target):
            raise ProjectVolumeError("Project has an active filesystem consumer")

    def _status(self, directory, record):
        if not record.get("deleted"):
            self._target(directory, record)
        return {
            "project_id": record["project_id"],
            "generation": record["generation"],
            "fence": record["fence"],
            "quota_mode": "soft",
            "quota_bytes": record["quota_bytes"],
            "deleted": bool(record.get("deleted")),
            "mounted": bool(record.get("mounted")),
            "attached": False,
            "pending": record["pending"],
            "pending_operation": record["intent"]["operation"]
            if record["pending"]
            else None,
            "maintenance_active": bool(record.get("maintenance")),
            "sandbox_id": (record.get("workspace") or {}).get("sandbox_id"),
        }

    def status(self, owner, project_id, *, generation=1):
        with self._locked(owner, project_id) as (directory, record):
            if record is None:
                raise ProjectVolumeError("Project volume does not exist")
            self._generation(record, generation)
            return self._status(directory, record)

    def create(self, owner, project_id, *, quota_bytes, fence, generation=1):
        quota_bytes = self._quota(quota_bytes)
        self._generation(None, generation)
        if type(fence) is not int or not 1 <= fence < 2**53:
            raise ProjectVolumeError("Invalid project operation fence")
        with self._locked(owner, project_id) as (directory, record):
            previous = record["fence"] if record else 0
            self._require_reservation(record, None)
            replacing = record is not None and record.get("deleted")
            if replacing:
                if (
                    generation != record["generation"] + 1
                    or fence <= previous
                    or record["pending"]
                ):
                    raise ProjectVolumeError(
                        "Deleted workspace needs a newer generation and fence"
                    )
            else:
                self._generation(record, generation)
                if record is None and generation != 1:
                    raise ProjectVolumeError("Initial workspace generation must be one")
            if record is None or replacing:
                self._admit(MIB)
                if any(path.name != "volume.json" for path in directory.iterdir()):
                    raise ProjectVolumeError(
                        "Unrecognized data exists in project directory"
                    )
                record = {
                    "schema": 1,
                    "storage_kind": self.storage_kind,
                    "instance_id": self.instance_id,
                    "project_id": project_id,
                    "generation": generation,
                    "owner": owner,
                    "owner_hash": hashlib.sha256(owner.encode()).hexdigest(),
                    "directory_id": uuid.uuid4().hex,
                    "root_identity": None,
                    "quota_bytes": quota_bytes,
                    "fence": previous,
                    "pending": False,
                    "intent": None,
                    "mounted": False,
                }
            if record["quota_bytes"] != quota_bytes or (
                record["intent"] and record["intent"]["operation"] != "create"
            ):
                raise ProjectVolumeError(
                    "Project already exists with another operation or capacity"
                )
            if not self._begin(
                directory, record, "create", fence, {"quota_bytes": quota_bytes}
            ):
                return self._status(directory, record)
            target = self._paths(directory, record)[1]
            if record["root_identity"] is None:
                try:
                    target.mkdir(mode=0o700)
                except FileExistsError:
                    info = target.lstat()
                    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {
                        os.geteuid(),
                        1000,
                    }:
                        raise ProjectVolumeError(
                            "Interrupted project creation cannot be verified"
                        ) from None
                    if any(target.iterdir()):
                        raise ProjectVolumeError(
                            "Interrupted project creation contains unknown data"
                        )
                if os.geteuid() == 0:
                    os.chown(target, 1000, 1000)
                os.chmod(target, 0o700)
                info = target.lstat()
                record["root_identity"] = [info.st_dev, info.st_ino]
                self._save(directory, record)
            self._target(directory, record)
            return self._finish(directory, record)

    def _workspace_binding(self, directory, record, *, maintenance=False):
        target = self._target(directory, record)
        workspace = record.get("workspace")
        identity = ":".join(map(str, record["root_identity"]))
        boot = self.host.boot_id()
        matches = bool(record["mounted"]) and (
            not workspace
            or (workspace["root_identity"] == identity and workspace["boot_id"] == boot)
        )
        if workspace and not matches and not maintenance:
            raise ProjectVolumeError(
                "Project workspace identity changed; recovery is required"
            )
        return {
            "workspace": workspace,
            "path": str(target),
            "project_id": record["project_id"],
            "generation": record["generation"],
            "fence": record["fence"],
            "pending": record["pending"],
            "mounted": record["mounted"],
            "mount_matches": matches,
            "boot_id": boot,
            "maintenance_active": bool(record.get("maintenance")),
        }

    def binding(self, owner, project_id, *, generation=1):
        binding = self.workspace(owner, project_id, generation=generation)
        if not binding["mounted"]:
            raise ProjectVolumeError("Project directory is not activated")
        return Path(binding["path"])

    def recover_workspace(
        self, owner, project_id, *, generation, fence, reservation=None
    ):
        with self._locked(owner, project_id) as (directory, record):
            self._generation(record, generation)
            self._require_reservation(record, reservation)
            if (
                not record
                or record.get("deleted")
                or not record["pending"]
                or record["fence"] != fence
                or record["intent"]["operation"] != "workspace_start"
            ):
                raise ProjectVolumeError(
                    "No pending start authorizes workspace recovery"
                )
            binding = self._workspace_binding(directory, record, maintenance=True)
            previous = record.get("workspace")
            if not previous or previous["boot_id"] == binding["boot_id"]:
                raise ProjectVolumeError("Recovery requires a verified host reboot")
            self._idle(Path(binding["path"]))
            record["mounted"] = True
            record["workspace"] = {
                **previous,
                "sandbox_id": "s" + uuid.uuid4().hex[:6],
                "token": uuid.uuid4().hex,
                "boot_id": binding["boot_id"],
                "root_identity": ":".join(map(str, record["root_identity"])),
                "recovered_from": previous["sandbox_id"],
            }
            self._save(directory, record)
            return self._workspace_binding(directory, record)

    def mutate(
        self,
        owner,
        project_id,
        operation,
        *,
        fence,
        quota_bytes=None,
        generation=1,
        reservation=None,
    ):
        if operation not in {"mount", "unmount", "resize", "trim", "delete"}:
            raise ProjectVolumeError(
                "Soft-quota directories do not support filesystem repair"
            )
        arguments = (
            {"quota_bytes": self._quota(quota_bytes)} if operation == "resize" else {}
        )
        if operation != "resize" and quota_bytes is not None:
            raise ProjectVolumeError("Unexpected project capacity argument")
        with self._locked(owner, project_id) as (directory, record):
            if record is None:
                raise ProjectVolumeError("Project volume does not exist")
            self._generation(record, generation)
            self._require_reservation(record, reservation)
            if record.get("deleted"):
                if operation == "delete" and fence == record["fence"]:
                    return self._status(directory, record)
                raise ProjectVolumeError("Deleted project cannot be reused")
            if record.get("workspace"):
                raise ProjectVolumeError(
                    "Remove workspace before directory maintenance"
                )
            target = self._paths(directory, record)[1]
            self._idle(target)
            retry_delete = (
                record["pending"] and record["intent"]["operation"] == "delete"
            )
            if not retry_delete:
                self._target(directory, record)
            if operation == "delete" and record["mounted"]:
                raise ProjectVolumeError("Deactivate directory before deletion")
            if operation == "trim" and not record["mounted"]:
                raise ProjectVolumeError("Trim requires an activated directory")
            if operation == "resize" and quota_bytes < record["quota_bytes"]:
                if (
                    record["mounted"]
                    or allocated_bytes(target, deadline=time.monotonic() + 5)
                    >= quota_bytes
                ):
                    raise ProjectVolumeError(
                        "Shrink requires an offline project smaller than the new quota"
                    )
            if not self._begin(directory, record, operation, fence, arguments):
                return self._status(directory, record)
            if operation in {"mount", "unmount"}:
                record["mounted"] = operation == "mount"
            elif operation == "resize":
                record["quota_bytes"] = quota_bytes
            elif operation == "delete":
                self._delete(directory, record)
            # Directory deletion already releases files; trim has no physical effect.
            return self._finish(directory, record)

    def _delete(self, directory, record):
        journals = []
        for name, limit in (
            ("files.json", MAX_MANIFEST_BYTES * 2),
            ("prune.json", MAX_MANIFEST_BYTES * 2),
            ("dependency-run.json", 32768),
        ):
            path = directory / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
                or info.st_size > limit
            ):
                raise ProjectVolumeError("Unsafe retired project journal")
            data = read_json(path)
            if (
                data.get("generation") != record["generation"]
                or not 0 < data.get("fence", 0) < record["fence"]
            ):
                raise ProjectVolumeError("Retired journal belongs to another operation")
            journals.append(path)
        target = self._paths(directory, record)[1]
        if target.exists() or target.is_symlink():
            self._target(directory, record)
            if not shutil.rmtree.avoids_symlink_attacks:
                raise ProjectVolumeError("Safe directory removal is unavailable")
            shutil.rmtree(target)
        for path in journals:
            path.unlink()
        record["deleted"] = True

    def measure(
        self, owner, project_id, *, generation, rebuild_command="", reservation=None
    ):
        with self._locked(owner, project_id) as (directory, record):
            if record is None:
                raise ProjectVolumeError("Project volume does not exist")
            self._generation(record, generation)
            self._require_reservation(record, reservation)
            if record["pending"] and reservation is None:
                raise ProjectVolumeError("Project measurement waits for maintenance")
            used, rebuildable, warning = 0, 0, ""
            if not record.get("deleted"):
                target = self._target(directory, record)
                used = allocated_bytes(target, deadline=time.monotonic() + 5)
                with ProjectFiles.root(target) as root:
                    try:
                        rebuildable = rebuildable_bytes(
                            root, rebuild_command, deadline=time.monotonic() + 5
                        )
                    except (OSError, ValueError, ProjectVolumeError, RecursionError):
                        rebuildable = None
                        warning = "Rebuildable usage unknown; no cleanup authorized"
                self._target(directory, record)
            return {
                "project_id": project_id,
                "generation": generation,
                "fence": record["fence"],
                "quota_mode": "soft",
                "quota_bytes": record["quota_bytes"],
                "used_bytes": used,
                "allocated_bytes": used,
                "rebuildable_bytes": min(rebuildable, used)
                if rebuildable is not None
                else None,
                "sampled_at": int(time.time()),
                "complete": True,
                "mounted": bool(record.get("mounted")),
                "warning": warning,
            }

    def projects(self):
        """Private runner inventory, including writers surviving a runner restart."""
        candidates = list(self.root.iterdir())
        if len(candidates) > 4096:
            raise ProjectVolumeError("Project inventory exceeds the inspection limit")
        result = []
        for directory in candidates:
            if not re.fullmatch(r"p[1-9][0-9]*", directory.name):
                continue
            self._private_directory(directory)
            path = directory / "volume.json"
            try:
                info = path.lstat()
            except FileNotFoundError:
                # A lookup of a never-created project can leave an empty directory.
                continue
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
                or info.st_size > 16384
            ):
                raise ProjectVolumeError("Unsafe project inventory journal")
            row = read_json(path)
            owner, identifier = row.get("owner"), int(directory.name[1:])
            with self._locked(owner, identifier) as (_, record):
                if (
                    record.get("workspace")
                    or record.get("intent", {}).get("operation") == "dependencies"
                ) and not record.get("deleted"):
                    result.append((owner, identifier, record["generation"]))
        return result

    def guard_binding(self, owner, project_id, *, generation):
        with self._locked(owner, project_id) as (directory, record):
            self._generation(record, generation)
            if not record or record.get("deleted"):
                raise ProjectVolumeError("Project is unavailable")
            binding = self._workspace_binding(directory, record, maintenance=True)
            run = None
            if (
                not record.get("workspace")
                and record["intent"]["operation"] == "dependencies"
            ):
                from .project_dependencies import DependencyJournal

                run = DependencyJournal(
                    self,
                    owner,
                    project_id,
                    generation,
                    record["fence"],
                    None,
                    record["intent"]["arguments"]["manifest_sha256"],
                ).read(directory, record)
                binding = {**binding, "workspace": run["workspace"]}
            return binding, run

    def require_quota(
        self,
        owner,
        project_id,
        *,
        generation,
        incoming=0,
        dependencies=False,
        maintenance=False,
    ):
        if type(incoming) is not int or incoming < 0:
            raise ProjectVolumeError("Invalid incoming project byte count")
        with self._locked(owner, project_id) as (directory, record):
            self._generation(record, generation)
            if not record or record.get("deleted"):
                raise ProjectVolumeError("Project directory is unavailable")
            if not maintenance:
                self._require_reservation(record, None)
                if record["pending"]:
                    raise ProjectVolumeError("Project is undergoing maintenance")
            target = self._target(directory, record)
            try:
                used = allocated_bytes(target, deadline=time.monotonic() + 5)
            except (OSError, ValueError, RecursionError) as error:
                raise ProjectVolumeError(
                    "project_quota_unknown: cannot verify remaining space"
                ) from error
            self._target(directory, record)
            if used >= record["quota_bytes"] or used + incoming > record["quota_bytes"]:
                raise ProjectVolumeError(
                    "project_quota_full: soft limit reached; clean dependencies, remove old versions or ask an administrator to expand the quota"
                )
            if dependencies and used * 100 >= record["quota_bytes"] * 95:
                raise ProjectVolumeError(
                    "project_quota_dependencies: usage has reached 95%; new dependency installs are refused"
                )
            return used
