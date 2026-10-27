from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

from .auth import CredentialFileAuthenticator
from .execution_contracts import (
    OperationProposal,
    WorkerJobProposal,
    content_hash,
    new_handle,
    bounded_object,
)
from .job_kinds import WORKER_JOB_KINDS
from .execution_storage import ClusterExecutionStore
from .resource_policy import ResourcePolicyStore


@dataclass(frozen=True)
class WriteBackendBinding:
    backend_ref: str = "not-configured"
    binding_version: int = 1
    available: bool = False
    reason: str = "尚未接入经维护者批准的唯一写后端"


WorkerAuthenticator = CredentialFileAuthenticator


class ClusterExecutionService:
    def __init__(
        self,
        store: ClusterExecutionStore,
        *,
        inventory: tuple[dict[str, object], ...],
        diagnostic_targets: tuple[dict[str, str], ...],
        worker_hosts: Mapping[str, str],
        worker_owners: Mapping[str, str] | None = None,
        worker_owner_aliases: Mapping[str, tuple[str, ...]] | None = None,
        resource_policies: ResourcePolicyStore | None = None,
        write_backend: WriteBackendBinding | None = None,
    ) -> None:
        self.store = store
        self.inventory = {str(item["host_id"]): item for item in inventory}
        self.diagnostic_targets = {
            str(item["target_id"]): dict(item) for item in diagnostic_targets
        }
        self.worker_hosts = dict(worker_hosts)
        self.worker_owners = dict(worker_owners or {})
        self.worker_owner_aliases = dict(worker_owner_aliases or {})
        self.resource_policies = resource_policies
        self.write_backend = write_backend or WriteBackendBinding()

    def capabilities(self) -> dict[str, Any]:
        compute_hosts = sorted(
            host_id for host_id, item in self.inventory.items() if item.get("compute")
        )
        return {
            "operations": {
                "available": self.write_backend.available,
                "backend": self.write_backend.backend_ref,
                "reason": "" if self.write_backend.available else self.write_backend.reason,
                "actions": ["service.start", "service.stop", "service.restart"],
            },
            "worker": {
                "available": bool(self.worker_hosts),
                "workers": sorted(self.worker_hosts),
                "compute_hosts": compute_hosts,
                "job_kinds": sorted(WORKER_JOB_KINDS),
                "borrow_scheduling": self.resource_policies is not None,
                "checkpoint_format": "gaoji-result-v1",
            },
            "guardians": {
                "available": bool(self.diagnostic_targets),
                "targets": sorted(self.diagnostic_targets),
                "minimum_interval_seconds": 15,
                "normal_checks_use_llm": False,
            },
        }

    def _operation_capability(
        self, proposal: OperationProposal
    ) -> tuple[str, str]:
        host = self.inventory.get(proposal.host_id)
        if host is None or not host.get("operate"):
            return "forbidden", "该主机没有授予 gaoji 宿主操作权限"
        if proposal.resource_ref not in set(host.get("operable_units", [])):
            return "forbidden", "该服务不在可操作白名单中"
        if not self.write_backend.available:
            return "not_configured", self.write_backend.reason
        return "available", ""

    def prepare_operation(
        self, raw: Mapping[str, Any], *, actor_id: str, origin_scope: str
    ) -> dict[str, Any]:
        now = int(time.time())
        proposal = OperationProposal.parse(raw, now=now)
        capability_status, reason = self._operation_capability(proposal)
        contract = {
            "task_ref": proposal.task_ref,
            "step_ref": proposal.step_ref,
            "actor_id": actor_id,
            "origin_scope": origin_scope,
            "host_id": proposal.host_id,
            "resource_ref": proposal.resource_ref,
            "operation": proposal.operation,
            "operation_version": 1,
            "arguments": proposal.arguments,
            "backend_ref": self.write_backend.backend_ref,
            "backend_binding_version": self.write_backend.binding_version,
            "expected_state": proposal.expected_state,
            "resource_version": 1,
            "policy_version": 1,
            "deadline_at": proposal.deadline_at,
            "resource_budget": {"wall_seconds": min(proposal.deadline_at - now, 300)},
            "idempotency_key": proposal.idempotency_key,
            "verification": proposal.verification,
            "compensation": proposal.compensation,
        }
        record = {
            **contract,
            "operation_id": new_handle("op"),
            "contract_hash": content_hash(contract),
            "status": "awaiting_approval" if capability_status == "available" else "planned",
            "capability_status": capability_status,
            "created_at": now,
        }
        result = self.store.prepare_operation(record)
        result["executable"] = capability_status == "available"
        result["disabled_reason"] = reason
        return result

    def approve_operation(
        self, operation_id: str, *, actor_id: str, expected_hash: str,
        expected_version: int,
    ) -> dict[str, Any]:
        if not actor_id.startswith("admin:"):
            raise PermissionError("only an authenticated administrator can approve operations")
        return self.store.approve_operation(
            operation_id,
            actor_id=actor_id,
            expected_hash=expected_hash,
            expected_version=expected_version,
            expires_at=int(time.time()) + 300,
        )

    def submit_guardian_operation(
        self, raw: Mapping[str, Any], *, actor_id: str, origin_scope: str
    ) -> dict[str, Any]:
        """Materialize and consume an administrator's bounded guardian grant."""
        if not actor_id.startswith("admin:"):
            raise PermissionError("guardian operations require an administrator")
        proposal = OperationProposal.parse(raw)
        capability_status, reason = self._operation_capability(proposal)
        if capability_status != "available":
            return {
                "status": "not_started",
                "capability_status": capability_status,
                "executable": False,
                "disabled_reason": reason,
            }
        operation = self.prepare_operation(
            raw, actor_id=actor_id, origin_scope=origin_scope
        )
        if not operation.get("executable"):
            return operation
        return self.approve_operation(
            str(operation["operation_id"]),
            actor_id=actor_id,
            expected_hash=str(operation["contract_hash"]),
            expected_version=int(operation["resource_version"]),
        )

    @staticmethod
    def _visible(item: dict[str, Any] | None, actor_id: str, origin_scope: str) -> dict[str, Any] | None:
        if item is None or actor_id.startswith("admin:"):
            return item
        if item.get("actor_id") != actor_id or item.get("origin_scope") != origin_scope:
            raise PermissionError("resource belongs to another scope")
        return item

    def operation_status(
        self, operation_id: str, *, actor_id: str, origin_scope: str
    ) -> dict[str, Any] | None:
        return self._visible(self.store.get_operation(operation_id), actor_id, origin_scope)

    def job_status(
        self, job_id: str, *, actor_id: str, origin_scope: str
    ) -> dict[str, Any] | None:
        return self._visible(self.store.get_job(job_id), actor_id, origin_scope)

    def submit_job(
        self, raw: Mapping[str, Any], *, actor_id: str, origin_scope: str
    ) -> dict[str, Any]:
        now = int(time.time())
        proposal = WorkerJobProposal.parse(raw, now=now)
        worker_id = str(proposal.constraints.get("worker_id") or "")
        if worker_id:
            host = self.inventory.get(self.worker_hosts.get(worker_id, ""), {})
            if not host.get("compute"):
                raise PermissionError("requested worker is not registered for compute")
        payload = dict(proposal.payload)
        if proposal.kind == "probe.http":
            target = self.diagnostic_targets.get(str(payload["target_id"]))
            if target is None:
                raise PermissionError("probe target is not configured")
            payload = {
                "target_id": target["target_id"],
                "url": target["url"],
                "expected_host": urlsplit(target["url"]).hostname,
            }
        artifact_id = str(payload.get("artifact_id") or "")
        if artifact_id:
            artifact = self.store.artifact(artifact_id)
            if artifact is None:
                raise LookupError("artifact not found")
            if (
                artifact.get("actor_id") != actor_id
                or artifact.get("origin_scope") != origin_scope
            ):
                raise PermissionError("artifact belongs to another scope")
        job_id = new_handle("job")
        if proposal.kind == "preview.static":
            preview_id = new_handle("preview")
            payload["preview_id"] = preview_id
            payload["expires_at"] = now + int(payload["ttl_seconds"])
        record = {
            "job_id": job_id,
            "actor_id": actor_id,
            "origin_scope": origin_scope,
            "kind": proposal.kind,
            "payload": payload,
            "constraints": proposal.constraints,
            "policy_derived": True,
            "deadline_at": proposal.deadline_at,
            "deadline_explicit": bool(raw.get("deadline_at")),
            "idempotency_key": proposal.idempotency_key,
            "created_at": now,
        }
        job = self.store.submit_job(record)
        if proposal.kind == "preview.static":
            job["preview_id"] = str(job.get("payload", {}).get("preview_id") or "")
        return job

    def claim_job(self, worker_id: str) -> dict[str, Any] | None:
        host_id = self.worker_hosts.get(worker_id)
        host = self.inventory.get(str(host_id or ""), {})
        return self.store.claim_job(
            worker_id, host=dict(host),
            owner_aliases=self.worker_owner_aliases.get(worker_id, ()),
        )

    def heartbeat(self, worker_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        configured_host = self.worker_hosts.get(worker_id)
        if configured_host is None:
            raise PermissionError("worker identity is not configured")
        host = self.inventory.get(configured_host)
        if host is None or not host.get("compute"):
            raise PermissionError("worker host is not authorized for compute")
        boot_id = str(raw.get("boot_id") or "").strip()
        availability = str(raw.get("availability") or "available").strip()
        protocol_version = int(raw.get("protocol_version") or 0)
        capabilities = raw.get("capabilities", [])
        runtime = raw.get("runtime", {})
        capacity = raw.get("capacity", {})
        public_base_url = str(raw.get("public_base_url") or "").strip().rstrip("/")
        if not 8 <= len(boot_id) <= 128 or protocol_version != 1:
            raise ValueError("incompatible worker identity or protocol")
        if availability not in {"available", "draining", "unavailable"}:
            raise ValueError("invalid worker availability")
        if not isinstance(capabilities, list) or any(
            item not in WORKER_JOB_KINDS for item in capabilities
        ):
            raise ValueError("invalid worker capabilities")
        if not isinstance(runtime, dict) or not isinstance(capacity, dict):
            raise ValueError("invalid worker runtime or capacity")
        for field in ("cpu_millis", "memory_bytes", "gpu_slots"):
            if not isinstance(capacity.get(field), int) or int(capacity[field]) < 0:
                raise ValueError("invalid worker capacity")
        if public_base_url:
            parsed = urlsplit(public_base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.query or parsed.fragment:
                raise ValueError("invalid public_base_url")
        worker = self.store.upsert_worker(
            worker_id,
            {
                "boot_id": boot_id,
                "protocol_version": protocol_version,
                "availability": availability,
                "capabilities": list(dict.fromkeys(capabilities)),
                "runtime": runtime,
                "capacity": capacity,
                "public_base_url": public_base_url,
            },
            host_id=configured_host,
        )
        if self.resource_policies is not None:
            worker["owner_policy"] = self.resource_policies.ensure_worker_policy(
                worker_id,
                owner_actor_id=self.worker_owners.get(worker_id, "admin:kenneth"),
                capacity=capacity,
            )
        return worker

    def save_checkpoint(
        self, job_id: str, *, worker_id: str, fence: int, phase: str,
        format_version: int, executor_version: str, state: Mapping[str, Any],
    ) -> dict[str, Any]:
        checked = bounded_object(state, field="checkpoint state", max_bytes=64_000)
        return self.store.save_checkpoint(
            job_id, worker_id=worker_id, fence=fence, phase=phase,
            format_version=format_version, executor_version=executor_version,
            state=checked,
        )

    def complete_job(
        self, job_id: str, *, worker_id: str, fence: int, ok: bool,
        result: dict[str, Any], error_code: str,
    ) -> dict[str, Any]:
        result = bounded_object(result, field="worker result", max_bytes=64_000)
        job = self.store.get_job(job_id)
        if job is None:
            raise LookupError("job not found")
        artifact_id = str(job.get("payload", {}).get("artifact_id") or "")
        cancelled = job.get("status") == "cancelling"
        if (
            not cancelled
            and job["kind"] in {"artifact.inspect", "document.verify", "media.inspect"}
            and artifact_id
        ):
            self.store.validate_artifact(artifact_id, ok=ok)
        if job["kind"] == "preview.static" and ok and not cancelled:
            worker = self.store.worker(worker_id)
            public_url = str(result.get("public_url") or "")
            expected_base = str((worker or {}).get("public_base_url") or "").rstrip("/")
            expected_url = f"{expected_base}/previews/{job['payload']['preview_id']}/"
            if not expected_base or public_url != expected_url:
                raise PermissionError("worker returned a preview URL that does not match this job")
            if int(job["payload"]["expires_at"]) <= int(time.time()):
                ok, error_code = False, "preview_expired"
                result = {"message": "Preview expired before its receipt was confirmed"}
        return self.store.complete_job(
            job_id, worker_id=worker_id, fence=fence, ok=ok,
            result=result, error_code=error_code,
        )

    @staticmethod
    def artifact_etag(item: Mapping[str, Any]) -> str:
        return f'"sha256:{item["sha256"]}"'
