"""Private, fenced operation journals shared by native project disk backends."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat

from src.local_runtime.files import file_lock, read_json, write_json
from .errors import SandboxError

MIB = 1024**2
MIN_QUOTA = 64 * MIB
MAX_QUOTA = 100 * 1024**3


class ProjectVolumeError(SandboxError):
    pass


class ProjectVolumeJournal:
    storage_kind = "ext4"

    def __init__(self, root: Path, instance_id: str, *, capacity_check=None):
        if not re.fullmatch(r"[a-f0-9]{32}", instance_id):
            raise ValueError("Invalid project-volume instance")
        self.root, self.instance_id = root.absolute(), instance_id
        self.capacity_check = capacity_check
        self._private_directory(self.root)
        self.root = self.root.resolve()

    @staticmethod
    def _private_directory(path):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ProjectVolumeError(
                "Project-volume directory must be private and runner-owned"
            )
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _quota(value):
        if type(value) is not int or not MIN_QUOTA <= value <= MAX_QUOTA or value % MIB:
            raise ProjectVolumeError(
                "Project quota must be 64 MiB to 100 GiB, aligned to MiB"
            )
        return value

    @staticmethod
    def _generation(record, generation):
        if type(generation) is not int or not 1 <= generation < 2**53:
            raise ProjectVolumeError("Invalid workspace generation")
        if record is not None and record.get("generation", 1) != generation:
            raise ProjectVolumeError("Workspace generation was superseded")

    @staticmethod
    def _reservation_hash(reservation):
        if not isinstance(reservation, str) or not re.fullmatch(
            r"[a-f0-9]{32}", reservation
        ):
            raise ProjectVolumeError("Invalid project maintenance reservation")
        return hashlib.sha256(reservation.encode()).hexdigest()

    @classmethod
    def _require_reservation(cls, record, reservation):
        expected = record.get("maintenance") if record else None
        supplied = (
            cls._reservation_hash(reservation) if reservation is not None else None
        )
        if expected != supplied:
            raise ProjectVolumeError("Project is reserved by a lifecycle operation")

    @contextmanager
    def _locked(self, owner, project_id):
        if (
            not isinstance(owner, str)
            or not owner.strip()
            or len(owner) > 200
            or "\0" in owner
            or type(project_id) is not int
            or not 1 <= project_id < 2**53
        ):
            raise ProjectVolumeError("Invalid project-volume owner or ID")
        with file_lock(self.root / ".volumes.lock", timeout=15):
            directory = self.root / f"p{project_id}"
            self._private_directory(directory)
            record_path = directory / "volume.json"
            record = None
            if record_path.exists() or record_path.is_symlink():
                info = record_path.lstat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_nlink != 1
                    or info.st_mode & 0o077
                    or info.st_size > 16384
                ):
                    raise ProjectVolumeError("Unsafe project-volume journal")
                record = read_json(record_path)
                if (
                    record.get("schema") != 1
                    or record.get("storage_kind", "ext4") != self.storage_kind
                    or record.get("instance_id") != self.instance_id
                    or record.get("project_id") != project_id
                    or record.get("owner_hash")
                    != hashlib.sha256(owner.encode()).hexdigest()
                ):
                    raise ProjectVolumeError(
                        "Project volume belongs to another owner, instance or backend"
                    )
            yield directory, record

    def _save(self, directory, record):
        write_json(directory / "volume.json", record)

    def _admit(self, size):
        if self.capacity_check:
            self.capacity_check(size)

    def _begin(self, directory, record, operation, fence, arguments):
        if type(fence) is not int or not 1 <= fence < 2**53:
            raise ProjectVolumeError("Invalid project operation fence")
        intent = {"operation": operation, "arguments": arguments}
        if fence < record["fence"]:
            raise ProjectVolumeError("Stale project operation fence")
        if fence == record["fence"]:
            if record.get("intent") != intent:
                raise ProjectVolumeError(
                    "Project fence was already used for different arguments"
                )
            return record["pending"]
        if record["pending"]:
            raise ProjectVolumeError(
                "Previous project-volume operation must be reconciled first"
            )
        record.update(fence=fence, intent=intent, pending=True)
        self._save(directory, record)
        return True

    def _finish(self, directory, record):
        record["pending"] = False
        result = self._status(directory, record)
        self._save(directory, record)
        return result

    def reserve(self, owner, project_id, *, generation, fence, action, reservation):
        """Cross-step exclusion; the driver must first exclude active IO."""
        digest = self._reservation_hash(reservation)
        if action not in {"reserve", "release"}:
            raise ProjectVolumeError("Unsupported project reservation operation")
        with self._locked(owner, project_id) as (directory, record):
            self._generation(record, generation)
            if record is None or (record.get("deleted") and action == "reserve"):
                raise ProjectVolumeError("Project volume does not exist")
            operation = "workspace_" + action
            arguments = {"reservation_hash": digest}
            held = record.get("maintenance")
            if held is not None and held != digest:
                raise ProjectVolumeError("Another lifecycle operation owns the project")
            replay = fence == record["fence"] and record["intent"] == {
                "operation": operation,
                "arguments": arguments,
            }
            if not replay and (
                (action == "reserve" and held) or (action == "release" and held is None)
            ):
                raise ProjectVolumeError("Reservation ownership cannot be inferred")
            if not replay:
                if record["pending"]:
                    raise ProjectVolumeError("Reconcile the pending operation first")
                record["maintenance"] = digest if action == "reserve" else None
            if self._begin(directory, record, operation, fence, arguments):
                return self._finish(directory, record)
            return self._status(directory, record)
