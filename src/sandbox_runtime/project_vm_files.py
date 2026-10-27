"""Private VM file client for the durable project/CAS executor.

The host records intent before QGA delivery. Lost receipts keep maintenance
reserved; replay consults the guest journal, never an optimistic RPC cache.
"""

import base64
import hashlib
import json
import re

from src.project_content import manifest_bytes
from .errors import SandboxError
from .project_vm_file_guest import HELPER_PATH, REQUEST_BYTES, TRANSFER_BYTES, validate_parameters
from .project_vm_guest import validate_config
from .project_vm_io import _journal

BASE_ACTION = {
    **dict.fromkeys(("snapshot", "page", "read", "check"), "snapshot"),
    **dict.fromkeys(("restore_begin", "restore_manifest", "restore_file", "restore_finish", "restore_check"), "restore_begin"),
    **dict.fromkeys(("prune", "prune_check"), "prune"),
    **dict.fromkeys(("dependency_begin", "dependency_manifest", "dependency_run", "dependency_status"), "dependency_begin"),
}
OPENERS = frozenset({"snapshot", "restore_begin", "prune", "dependency_begin"})


class VmFileJournal:
    def __init__(self, disks, *, tasks):
        self.disks, self.tasks = disks, tasks

    def _binding(self, directory, record, metadata, generation, reservation, fence):
        disks = self.disks
        disks._generation(record, generation)
        disks._require_reservation(record, reservation)
        if not record or not record.get("maintenance"):
            raise SandboxError("VM files require a reserved project")
        if type(fence) is not int or not 1 <= fence < 2**53:
            raise SandboxError("Invalid VM file fence")
        retry = (record["fence"] == fence and record["intent"]["operation"] == "guest_files")
        binding = disks._guest_binding(directory, record, record.get("sandbox_id"), allow_pending=retry)
        if (metadata.get("purpose") != "project" or metadata.get("project_domain_uuid") != record.get("guest_domain_uuid")
                or validate_config({**metadata.get("project", {}), "initialize": False}) != binding.guest_config()
                or record.get("guest_initialized") is not True):
            raise SandboxError("VM file metadata differs from its initialized disk")
        workspace = record.get("workspace")
        spec = metadata.get("project_spec")
        if (workspace and disks._boot_spec(spec) != workspace["spec"]) or (not workspace and spec is not None):
            raise SandboxError("VM file resource contract changed")
        return binding, record["guest_domain_uuid"]

    def prepare(self, action, owner, project_id, *, metadata, generation, fence, reservation, parameters):
        disks = self.disks
        with disks._locked(owner, project_id) as (directory, record):
            binding, domain = self._binding(directory, record, metadata, generation, reservation, fence)
            base = BASE_ACTION[action]
            if action in OPENERS:
                frozen = dict(parameters)
                if action == "dependency_begin":
                    tasks = (metadata.get("project_spec") or {}).get("pids_limit", self.tasks)
                    if type(tasks) is not int or parameters["tasks"] != tasks:
                        raise SandboxError("Dependency task limit differs from the project contract")
                if action == "snapshot":
                    frozen.setdefault("rebuild_command", "")
                if action == "prune" and fence > record["fence"]:
                    expected = {"action": "snapshot", "fence": parameters["snapshot_fence"],
                                "manifest_sha256": parameters["manifest_sha256"]}
                    if record["pending"] or record.get("files") != expected or record["fence"] != expected["fence"]:
                        raise SandboxError("VM cleanup requires its completed source snapshot")
                disks._begin(directory, record, "guest_files", fence, {"action": base, "parameters": frozen})
            else:
                intent = record["intent"]
                if (record["fence"] != fence or intent["operation"] != "guest_files"
                        or intent["arguments"]["action"] != base):
                    raise SandboxError("VM file operation was superseded")
                expected = (intent["arguments"]["parameters"]["manifest_sha256"] if base in {"restore_begin", "dependency_begin"}
                            else record.get("files", {}).get("manifest_sha256"))
                if expected != parameters["manifest_sha256"]:
                    raise SandboxError("VM file operation belongs to another manifest")
                if action == "prune_check" and parameters != intent["arguments"]["parameters"]:
                    raise SandboxError("VM cleanup receipt belongs to another source snapshot")
                if action in {"page", "read", "check", "restore_check", "prune_check"} and record["pending"]:
                    raise SandboxError("VM file operation has not completed")
                if action in {"restore_manifest", "restore_file"} and not record["pending"]:
                    raise SandboxError("Completed VM restore cannot be modified")
            return binding, domain

    def acknowledge(self, action, owner, project_id, *, metadata, generation, fence, reservation, parameters, receipt):
        disks = self.disks
        with disks._locked(owner, project_id) as (directory, record):
            self._binding(directory, record, metadata, generation, reservation, fence)
            base = BASE_ACTION[action]
            if (record["fence"] != fence or record["intent"]["operation"] != "guest_files"
                    or record["intent"]["arguments"]["action"] != base):
                raise SandboxError("Guest file acknowledgement was superseded")
            if (not isinstance(receipt, dict)
                    or any(type(receipt.get(k)) is not int or receipt[k] != record[k]
                           for k in ("project_id", "generation", "fence"))
                    or type(receipt.get("pending")) is not bool
                    or not isinstance(receipt.get("manifest_sha256"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", receipt["manifest_sha256"])):
                raise SandboxError("Invalid guest file acknowledgement")
            if base == "dependency_begin":
                if (receipt.get("state") not in {"prepared", "starting", "running", "failed", "completed"}
                        or type(receipt.get("source_ready")) is not bool
                        or (receipt["state"] == "completed") != (receipt["pending"] is False)
                        or (receipt["state"] == "completed" and (receipt["source_ready"] is not True
                            or type(receipt.get("exit_code")) is not int or receipt["exit_code"] != 0))):
                    raise SandboxError("Dependency acknowledgement has no verified completion")
            elif action not in {"restore_begin", "restore_manifest", "restore_file"} and receipt["pending"]:
                raise SandboxError("Guest file operation did not finish")
            if action in {"restore_manifest", "restore_file"} and not receipt["pending"]:
                raise SandboxError("Guest restore chunk has no pending operation")
            if "manifest_sha256" in parameters and receipt["manifest_sha256"] != parameters["manifest_sha256"]:
                raise SandboxError("Guest acknowledgement belongs to another manifest")
            existing = record.get("files")
            if existing and existing["fence"] == fence and existing["manifest_sha256"] != receipt["manifest_sha256"]:
                raise SandboxError("Guest manifest changed within the acknowledged operation")
            record["files"] = {"action": base, "fence": fence, "manifest_sha256": receipt["manifest_sha256"]}
            if not receipt["pending"]:
                disks._finish(directory, record)
            else:
                disks._save(directory, record)


class VmProjectFiles:
    max_file_bytes = TRANSFER_BYTES

    def __init__(self, mounts):
        self.mounts, self.manager, self.disks = mounts, mounts.manager, mounts.disks
        self.journal = VmFileJournal(self.disks, tasks=self.manager.pids_limit)

    async def project_rebuild_prepare(self, owner, project_id, *, generation, fence, reservation, manifest, check_context):
        """Import only trusted version metadata; never overwrite the retained tree."""
        status = await _journal(self.disks.status, owner, project_id, generation=generation)
        sid = status.get("sandbox_id")
        if not sid:
            raise SandboxError("Dependency reconstruction needs its initialized guest")
        metadata = await self.manager._metadata(owner, sid)
        tasks = (metadata.get("project_spec") or {}).get("pids_limit", self.manager.pids_limit)
        encoded = manifest_bytes(manifest, quota_bytes=status["quota_bytes"])
        digest = hashlib.sha256(encoded).hexdigest()
        options = dict(generation=generation, fence=fence, reservation=reservation, manifest_sha256=digest)
        await check_context()
        receipt, _ = await self.project_files("dependency_begin", owner, project_id,
            manifest_size=len(encoded), tasks=tasks, **options)
        if not receipt["source_ready"]:
            for offset in range(0, len(encoded), TRANSFER_BYTES):
                await check_context()
                receipt, _ = await self.project_files("dependency_manifest", owner, project_id,
                    offset=offset, payload=encoded[offset:offset + TRANSFER_BYTES], **options)
        if receipt["source_ready"] is not True:
            raise SandboxError("Guest did not verify the dependency source")
        return receipt

    async def project_rebuild(self, action, owner, project_id, **options):
        if action not in {"run", "status"}:
            raise SandboxError("Unsupported dependency operation")
        receipt, _ = await self.project_files("dependency_" + action, owner, project_id, **options)
        return receipt

    async def project_files(self, action, owner, project_id, *, generation, fence, reservation,
                            request_id=None, payload=b"", **parameters):
        validate_parameters(action, parameters, payload)
        if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id)):
            raise SandboxError("Invalid VM file request identity")
        status = await _journal(self.disks.status, owner, project_id, generation=generation)
        sid = status.get("sandbox_id")
        if not sid:
            raise SandboxError("VM file transfer needs its initialized guest")
        async with self.manager.io_lock(sid):
            metadata = await self.manager._metadata(owner, sid)
            options = dict(metadata=metadata, generation=generation, fence=fence, reservation=reservation, parameters=parameters)
            binding, domain = await _journal(self.journal.prepare, action, owner, project_id, **options)
            await self.mounts._domain(binding, domain, spec=metadata.get("project_spec"))
            identity = {k: binding.guest_config()[k] for k in ("project_id", "generation", "filesystem_uuid", "serial")}
            request = json.dumps({"identity": identity, "action": action, "fence": fence, "reservation": reservation,
                                  "parameters": parameters, "payload": base64.b64encode(payload).decode("ascii")},
                                 separators=(",", ":")).encode()
            if len(request) > REQUEST_BYTES:
                raise SandboxError("VM file request exceeds its transport limit")
            code, output, _ = await self.manager._guest_exec_raw(
                sid, "/usr/bin/python3", ["-I", HELPER_PATH], input_bytes=request, timeout=360,
            )
            if code or len(output) > 1024 * 1024:
                raise SandboxError("Guest file operation failed; keep the existing reservation and retry")
            try:
                reply = json.loads(output)
                if (not isinstance(reply, dict) or set(reply) != {"identity", "receipt", "payload"}
                        or not isinstance(reply["identity"], dict) or set(reply["identity"]) != set(identity)
                        or any(type(reply["identity"][k]) is not type(v) or reply["identity"][k] != v for k, v in identity.items())
                        or not isinstance(reply["payload"], str) or len(reply["payload"]) > (TRANSFER_BYTES + 2) // 3 * 4):
                    raise ValueError()
                body = base64.b64decode(reply["payload"], validate=True)
                if len(body) > TRANSFER_BYTES or (body and action != "read"):
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise SandboxError("Invalid guest file response; completion remains unacknowledged") from None
            await self.mounts._domain(binding, domain, spec=metadata.get("project_spec"))
            await _journal(self.journal.acknowledge, action, owner, project_id, receipt=reply["receipt"], **options)
            return reply["receipt"], body
