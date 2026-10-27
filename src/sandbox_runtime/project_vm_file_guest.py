"""Guest-side retained-file protocol. Uses the same file engine as OCI.

Only root can invoke the bundled entry point. No request can select a host or
guest path: the verified data disk is always /workspace and journals live on
the private system disk. File IO holds the execution gate's maintenance lock;
dependency status can inspect its durable worker while that lock is held.
"""

import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from src.project_content import MAX_FILE_BYTES, MAX_MANIFEST_BYTES, MAX_VERSION_FILES
from .project_files import ProjectFiles
from .project_volume_journal import ProjectVolumeError, ProjectVolumeJournal
from .project_vm_gate import ProjectGuestGate
from .project_vm_guest import WORKSPACE, load_config

HELPER_PATH = "/usr/local/lib/gaoji/project_files.pyz"
STATE_ROOT = Path("/var/lib/gaoji-project-files")
# QGA input is base64 nested in a virsh command argument. Stay below ARG_MAX.
TRANSFER_BYTES = 48 * 1024
REQUEST_BYTES = 80 * 1024
UPLOADS = frozenset({"restore_manifest", "restore_file", "dependency_manifest"})
FIELDS = {
    "snapshot": (set(), {"rebuild_command"}),
    "page": ({"manifest_sha256", "index"}, set()),
    "read": ({"manifest_sha256", "index", "offset"}, {"max_bytes"}),
    "check": ({"manifest_sha256"}, set()),
    "restore_begin": ({"manifest_sha256", "manifest_size"}, set()),
    "restore_manifest": ({"manifest_sha256", "offset"}, set()),
    "restore_file": ({"manifest_sha256", "index", "offset"}, set()),
    "restore_finish": ({"manifest_sha256"}, set()),
    "restore_check": ({"manifest_sha256"}, set()),
    "prune": ({"manifest_sha256", "snapshot_fence"}, set()),
    "prune_check": ({"manifest_sha256", "snapshot_fence"}, set()),
    "dependency_begin": ({"manifest_sha256", "manifest_size", "tasks"}, set()),
    "dependency_manifest": ({"manifest_sha256", "offset"}, set()),
    "dependency_run": ({"manifest_sha256"}, set()),
    "dependency_status": ({"manifest_sha256"}, set()),
}


def validate_parameters(action, parameters, payload=b""):
    if not isinstance(action, str) or action not in FIELDS or not isinstance(parameters, dict):
        raise ProjectVolumeError("Unsupported guest file operation")
    required, optional = FIELDS[action]
    if not required <= set(parameters) or set(parameters) - required - optional:
        raise ProjectVolumeError("Invalid guest file parameters")
    if not isinstance(payload, bytes) or len(payload) > TRANSFER_BYTES or (payload and action not in UPLOADS):
        raise ProjectVolumeError("Invalid guest file payload")
    for key, minimum, maximum in (
        ("index", 0, MAX_VERSION_FILES), ("offset", 0, MAX_FILE_BYTES),
        ("max_bytes", 1, TRANSFER_BYTES), ("manifest_size", 1, MAX_MANIFEST_BYTES),
        ("snapshot_fence", 1, 2**53 - 1),
        ("tasks", 32, 4096),
    ):
        if key in parameters and (type(parameters[key]) is not int or not minimum <= parameters[key] <= maximum):
            raise ProjectVolumeError("Invalid guest file range")
    digest = parameters.get("manifest_sha256")
    if "manifest_sha256" in parameters and (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)):
        raise ProjectVolumeError("Invalid guest manifest digest")
    command = parameters.get("rebuild_command", "")
    if not isinstance(command, str) or len(command) > 4000 or "\0" in command:
        raise ProjectVolumeError("Invalid guest rebuild command")


class GuestFileVolumes(ProjectVolumeJournal):
    storage_kind = "guest-files"

    def __init__(self, gate, *, root=STATE_ROOT, workspace=WORKSPACE):
        self.gate, self.workspace_path = gate, Path(workspace)
        self.owner = gate.config["filesystem_uuid"]
        super().__init__(Path(root), self.owner.replace("-", ""))

    @contextmanager
    def _locked(self, owner, project_id):
        with super()._locked(owner, project_id) as (directory, record):
            config = self.gate.config
            if owner != self.owner or type(project_id) is not int or project_id != config["project_id"]:
                raise ProjectVolumeError("Guest file identity differs from its disk")
            if record is None:
                record = {
                    "schema": 1, "storage_kind": self.storage_kind,
                    "instance_id": self.instance_id, "owner_hash": hashlib.sha256(owner.encode()).hexdigest(),
                    "project_id": project_id, "generation": config["generation"],
                    "quota_bytes": config["quota_bytes"], "filesystem_uuid": self.owner,
                    "fence": 0, "pending": False, "intent": None, "maintenance": None,
                }
                self._save(directory, record)
            if any(type(record.get(key)) is not type(config[key]) or record[key] != config[key]
                   for key in ("generation", "quota_bytes", "filesystem_uuid")):
                raise ProjectVolumeError("Guest file journal differs from its disk")
            yield directory, record

    def admit(self, fence, reservation):
        digest = self._reservation_hash(reservation)
        with self._locked(self.owner, self.gate.config["project_id"]) as (directory, record):
            if fence < record["fence"]:
                raise ProjectVolumeError("Stale guest file fence")
            if digest != record["maintenance"]:
                if record["pending"] or fence <= record["fence"]:
                    raise ProjectVolumeError("Guest files remain reserved by another operation")
                record["maintenance"] = digest
                self._save(directory, record)

    def _workspace_binding(self, directory, record, *, maintenance):
        self.gate.workspace.verify()
        return {"path": self.workspace_path, "mounted": True, "workspace": None, "mount_matches": True}

    @staticmethod
    def _status(directory, record):
        return {key: record[key] for key in ("project_id", "generation", "fence", "pending")}


class ProjectGuestFiles:
    def __init__(self, gate, *, root=STATE_ROOT, workspace=WORKSPACE, dependency_runtime=None):
        from .project_vm_dependency_guest import GuestDependencies
        self.gate = gate
        self.volumes = GuestFileVolumes(gate, root=root, workspace=workspace)
        self.dependencies = GuestDependencies(gate, self.volumes, runtime=dependency_runtime)

    def operate(self, request):
        if not isinstance(request, dict) or set(request) != {
            "identity", "action", "fence", "reservation", "parameters", "payload",
        }:
            raise ProjectVolumeError("Invalid guest file envelope")
        identity = request["identity"]
        if (not isinstance(identity, dict) or set(identity) != set(self.gate.identity)
                or any(type(identity[k]) is not type(v) or identity[k] != v for k, v in self.gate.identity.items())):
            raise ProjectVolumeError("File request belongs to another project disk")
        self.gate._epoch(request["fence"])
        self.volumes._reservation_hash(request["reservation"])
        encoded = request["payload"]
        if not isinstance(encoded, str) or len(encoded) > (TRANSFER_BYTES + 2) // 3 * 4:
            raise ProjectVolumeError("Guest file payload is too large")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise ProjectVolumeError("Invalid guest file encoding") from None
        action, parameters = request["action"], request["parameters"]
        validate_parameters(action, parameters, payload)
        parameters = dict(parameters)
        if action == "read":
            parameters.setdefault("max_bytes", TRANSFER_BYTES)
        if action.startswith("dependency_"):
            receipt = self.dependencies.operate(action, request["fence"], request["reservation"], parameters, payload)
            return {"identity": self.gate.identity, "receipt": receipt, "payload": ""}
        with self.gate.maintenance(request["fence"]):
            self.volumes.admit(request["fence"], request["reservation"])
            receipt, body = ProjectFiles(self.volumes).operate(
                action, self.volumes.owner, self.gate.config["project_id"],
                generation=self.gate.config["generation"], fence=request["fence"],
                reservation=request["reservation"], payload=payload, **parameters,
            )
            return {"identity": self.gate.identity, "receipt": receipt,
                    "payload": base64.b64encode(body).decode("ascii")}


def main():
    if os.geteuid() != 0:
        raise ProjectVolumeError("Project file helper requires the guest administrator")
    if len(sys.argv) == 3 and sys.argv[1] == "--dependency-worker" and sys.argv[2].isdecimal():
        ProjectGuestFiles(ProjectGuestGate(load_config())).dependencies.worker(int(sys.argv[2]))
        return
    usage = sys.argv[1:] == ["--usage"]
    if len(sys.argv) != 1 and not usage:
        raise ProjectVolumeError("Unsupported project helper arguments")
    data = sys.stdin.buffer.read(REQUEST_BYTES + 1)
    if len(data) > REQUEST_BYTES:
        raise ProjectVolumeError("Guest file request exceeds its limit")
    request = json.loads(data)
    gate = ProjectGuestGate(load_config())
    if usage:
        from .project_vm_usage_guest import measure
        result = measure(gate, request)
    else:
        result = ProjectGuestFiles(gate).operate(request)
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))


if __name__ == "__main__":
    main()
