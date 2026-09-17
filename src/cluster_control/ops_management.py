"""Catalog-backed operations with a separate credential and host-owned approval."""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from typing import Any, Awaitable, Callable

from jsonschema import Draft202012Validator, ValidationError

from .adapters.ops import OpsError
from .adapters.backend import OperationsBackend
from .execution_contracts import canonical_json, content_hash, new_handle
from .execution_storage import ClusterExecutionStore
from .host_operations import REBOOT_DEFINITION, execution_params, host_contract, parse_helpers
from .service_verification import SERVICE_ACTIONS, verify_service


class OpsManagementService:
    def __init__(self, client: OperationsBackend, store: ClusterExecutionStore, *,
                 hosts: tuple[str, ...], actors: tuple[str, ...], host_helpers: dict[str, str] | None = None) -> None:
        self.client = client
        self.store = store
        self.hosts = frozenset(hosts)
        self.actors = frozenset(actors)
        self.host_helpers = parse_helpers(canonical_json(host_helpers or {}))
        if set(self.host_helpers) - self.hosts:
            raise ValueError("Host helpers must be inside the management host grant")
        self.owner = uuid.uuid4().hex
        self.closed = False
        self.guardian_validator: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self.guardian_approver = None
        self.deployment_validator: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    def authorize(self, actor: str) -> None:
        if actor not in self.actors:
            raise PermissionError("This identity is not an operations administrator")

    async def definitions(self) -> list[dict[str, Any]]:
        data = await self.client.catalog()
        if not isinstance(data, dict) or data.get("version") != 2:
            raise OpsError("incompatible_catalog", "Management requires Ops protocol 2")
        definitions = data.get("operations")
        if not isinstance(definitions, list) or any(
            not isinstance(d, dict) or not isinstance(d.get("name"), str)
            or not isinstance(d.get("read_only"), bool)
            or not isinstance(d.get("params_schema"), dict)
            or d.get("idempotency") not in {"none", "required"}
            for d in definitions
        ):
            raise OpsError("invalid_catalog", "Invalid management operation catalog")
        definitions = [dict(d) for d in definitions]
        execute = next((d for d in definitions if d["name"] == "exec.run"), None)
        if execute and self.host_helpers:
            if execute["idempotency"] != "required" or execute["read_only"] or execute.get("kind") != "job_submission":
                raise OpsError("incompatible_execution", "Checked host execution requires durable upstream jobs")
            if any(d["name"] == "host.reboot" for d in definitions):
                raise OpsError("conflicting_operation", "Upstream host.reboot conflicts with the configured target helper")
            definitions.append({**REBOOT_DEFINITION, "execution_definition": execute})
        return definitions

    async def catalog(self, actor: str, operation: str = "") -> dict[str, Any]:
        self.authorize(actor)
        definitions = await self.definitions()
        if operation:
            definitions = [d for d in definitions if d["name"] == operation]
            if not definitions:
                raise LookupError("Operation is not granted by the upstream catalog")
        else:
            definitions = [{k: d.get(k) for k in
                ("name", "summary", "read_only", "kind", "idempotency")} for d in definitions]
        return {"version": 2, "hosts": sorted(self.hosts), "operations": definitions,
                "backend": self.client.backend_name,
                "writes_require_approval": True, "checked_execution_hosts": sorted(self.host_helpers)}

    async def receipt(self, intent_key: str, *, actor: str, origin: str) -> dict[str, Any]:
        self.authorize(actor)
        if not isinstance(intent_key, str) or re.fullmatch(r"subagent:[a-f0-9]{64}", intent_key) is None:
            raise ValueError("Invalid task intent key")
        proof = await asyncio.to_thread(self.store.operation_provenance, actor, origin, intent_key)
        if proof is None or proof["host_id"] not in self.hosts:
            raise LookupError("No matching operation receipt in this identity and conversation")
        return proof

    def binding_hash(self, definition: dict[str, Any], *, legacy: bool = False) -> str:
        # Credential rotation or a changed schema/scope invalidates an old approval.
        binding = {"definition": definition, "hosts": sorted(self.hosts),
            "actors": sorted(self.actors), **self.client.authorization_binding()}
        if not legacy and definition["name"] in {"exec.run", "host.reboot"}:
            binding["host_helpers"] = self.host_helpers
        return content_hash(binding)

    async def call(self, operation: str, params: dict[str, Any], *, actor: str,
                   origin: str, idempotency_key: str = "", guardian_id: str = "",
                   deployment: dict[str, str] | None = None) -> dict[str, Any]:
        self.authorize(actor)
        definition = next((d for d in await self.definitions() if d["name"] == operation), None)
        if definition is None:
            raise PermissionError("Operation is not granted by the upstream catalog")
        if len(canonical_json(params).encode()) > 256 * 1024:
            raise ValueError("Operation parameters exceed 256 KiB")
        try:
            Draft202012Validator(definition["params_schema"]).validate(params)
        except ValidationError as exc:
            raise ValueError(f"Invalid operation parameters: {exc.message[:500]}") from None
        if operation in SERVICE_ACTIONS and (
            definition.get("kind") != "job_submission" or definition.get("idempotency") != "required"
            or not isinstance(params.get("unit"), str) or not params["unit"].endswith(".service")
        ):
            raise ValueError("Service actions require an explicit service and a durable native job")
        for field in ("host", "target_host"):
            if params.get(field) and params[field] not in self.hosts:
                raise PermissionError("Host is outside the management grant")
        if definition["read_only"]:
            response = await self.client.call(operation, params)
            return {"ok": True, "operation": operation, "result": response.data}
        if not 8 <= len(idempotency_key) <= 160:
            raise ValueError("Writes require a stable 8-160 character idempotency key")
        arguments = {"op": operation, "params": params,
                     "binding_hash": self.binding_hash(definition),
                     "upstream_idempotency": definition["idempotency"]}
        if operation in {"exec.run", "host.reboot"}:
            arguments["host_control"] = host_contract(operation, params, self.host_helpers)
        if guardian_id:
            arguments["guardian_id"] = guardian_id
        if deployment:
            if guardian_id:
                raise ValueError("An operation cannot have two authorizing parents")
            if deployment.get("host_id") not in self.hosts:
                raise PermissionError("Deployment host is outside the management grant")
            arguments["deployment"] = deployment
        intent_hash = content_hash({"actor": actor, "origin": origin, "arguments": arguments})
        existing = await asyncio.to_thread(self.store.find_operation, actor, origin, idempotency_key)
        if existing is not None:
            if existing.get("contract_hash") != intent_hash:
                raise ValueError("Idempotency key belongs to a different approved intent")
            return self.proposal_result(existing)
        now = int(time.time())
        record = {"operation_id": new_handle("op"), "task_ref": "", "step_ref": "",
            "actor_id": actor, "origin_scope": origin,
            "host_id": params.get("host") or params.get("target_host") or (deployment or {}).get("host_id", "scoped-resource"),
            "resource_ref": operation, "operation": "ssh.execute" if self.client.backend_name == "ssh" else "maxops.execute", "operation_version": 1,
            "arguments": arguments, "backend_ref": "ssh-management-v1" if self.client.backend_name == "ssh" else "ops-management-v2", "backend_binding_version": 1,
            "expected_state": {}, "resource_version": 1, "policy_version": 1,
            "deadline_at": now + 1800, "resource_budget": {"wall_seconds": 1800},
            "idempotency_key": idempotency_key, "verification": {}, "compensation": {},
            "contract_hash": intent_hash, "status": "awaiting_approval", "capability_status": "available",
            "created_at": now}
        if "host_control" in arguments:
            execution_params(record)
        saved = await asyncio.to_thread(self.store.prepare_operation, record)
        return self.proposal_result(saved)

    @staticmethod
    def proposal_result(record: dict[str, Any]) -> dict[str, Any]:
        awaiting = record["status"] == "awaiting_approval"
        return {"ok": True, "executed": record["status"] == "succeeded", "approval_required": awaiting, "operation": record,
                "effect_verified": (record["status"] == "succeeded"
                                    and (record.get("result") or {}).get("verification", {}).get("verified") is True),
                "next_action": (
                    "Administrator must review the exact parameters and confirm the one-time code in private QQ. Do not claim execution."
                    if awaiting else "Inspect this existing operation; do not submit the same effect under a new key."
                )}

    async def approve(self, operation_id: str, *, actor: str, expected_hash: str,
                      expected_version: int) -> dict[str, Any]:
        self.authorize(actor)
        record = await asyncio.to_thread(self.store.get_operation, operation_id)
        if not record or record["operation"] not in {"maxops.execute", "ssh.execute"}:
            raise LookupError("Management proposal not found")
        if record["arguments"].get("guardian_id"):
            if self.guardian_approver is None:
                raise PermissionError("Guardian phone approval service unavailable")
            return await self.guardian_approver(record, actor=actor, expected_hash=expected_hash,
                                                expected_version=expected_version)
        if record["arguments"].get("deployment"):
            raise PermissionError("Deployment actions require the matching two-phase deployment contract")
        await self.validate_binding(record)
        return await asyncio.to_thread(self.store.approve_operation, operation_id, actor_id=actor,
            expected_hash=expected_hash, expected_version=expected_version, expires_at=int(time.time()) + 300)

    async def validate_binding(self, record: dict[str, Any]) -> dict[str, Any]:
        self.authorize(record["actor_id"])
        if self.client.backend_name == "ssh" and record.get("backend_ref") != "ssh-management-v1":
            raise PermissionError("Legacy MaxOps task retained for inspection; it cannot be replayed through SSH")
        arguments = record["arguments"]
        definition = next((d for d in await self.definitions() if d["name"] == arguments["op"]), None)
        valid_hashes = {self.binding_hash(definition)} if definition else set()
        # Already accepted legacy commands may still be observed, but cannot be
        # submitted again without the new helper contract.
        if definition and arguments["op"] == "exec.run" and "host_control" not in arguments and record.get("backend_operation_id"):
            valid_hashes.add(self.binding_hash(definition, legacy=True))
        if definition is None or arguments["binding_hash"] not in valid_hashes:
            raise PermissionError("Management scope or operation schema changed; prepare a new request")
        if "host_control" in arguments and arguments["host_control"] != host_contract(arguments["op"], arguments["params"], self.host_helpers):
            raise PermissionError("Target execution binding no longer matches the approved intent")
        return definition

    async def _validate_submission(self, record: dict[str, Any]) -> dict[str, Any]:
        # Only read-only checks may retry; leave time in the lease for the write receipt.
        budget = min(45, record["deadline_at"] - time.time(),
                     record.get("lease_expires_at", time.time() + 90) - time.time() - 30)
        if budget <= 0:
            raise OpsError("validation_timeout", "No time remains for submission validation")
        try:
            async with asyncio.timeout(budget):
                for attempt in range(3):
                    try:
                        definition = await self.validate_binding(record)
                        for key, validator in (("guardian_id", self.guardian_validator),
                                               ("deployment", self.deployment_validator)):
                            if record["arguments"].get(key):
                                if validator is None:
                                    raise PermissionError("Parent authorization backend is unavailable")
                                await validator(record)
                        current = await asyncio.to_thread(self.store.get_operation, record["operation_id"])
                        if (not current or current["status"] != "running"
                                or current["fence"] != record["fence"]
                                or current["contract_hash"] != record["contract_hash"]
                                or current["deadline_at"] <= int(time.time())):
                            raise PermissionError("Operation changed or was cancelled before submission")
                        return definition
                    except OpsError as exc:
                        if not exc.retryable or attempt == 2:
                            raise
                        await asyncio.sleep(2 ** attempt)
        except TimeoutError:
            raise OpsError("validation_timeout", "Read-only submission checks timed out") from None
        raise AssertionError("Submission validation did not return")

    async def _observe_job(self, record: dict[str, Any], backend_id: str) -> dict[str, Any]:
        # The hub can briefly reject a status read while reconciling an accepted job.
        # Only repeat observations of that handle, never the original submission.
        for attempt in range(3):
            await self.validate_binding(record)
            if record["deadline_at"] <= int(time.time()):
                raise OpsError("observation_deadline", "Job observation deadline expired")
            try:
                response = await self.client.call("jobs.status", {"job_id": backend_id})
                if not isinstance(response.data, dict):
                    raise ValueError("Upstream job observation is not an object")
                return response.data
            except OpsError as exc:
                if exc.code != "invalid_request" or attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)
        raise AssertionError("Job observation did not return")

    async def run_once(self) -> bool:
        record = await asyncio.to_thread(self.store.claim_managed_operation, self.owner)
        if record is None:
            return False
        if "host_control" in record["arguments"]:
            from .host_operation_runner import run_host_operation
            await run_host_operation(self, record)
            return True
        status, result, error, backend_id = "needs_attention", {}, "", record.get("backend_operation_id")
        saved = dict(record.get("result") or {})
        submission_started = bool(backend_id or saved.get("submission_started"))
        try:
            if self.client.backend_name == "ssh" and not backend_id and submission_started:
                # The deterministic handle is known before dispatch. Takeover only
                # observes it; a missing receipt never triggers another submission.
                from src.ssh_ops_protocol import handle
                backend_id = handle(record["host_id"], record["operation_id"])
                record = {**record, "backend_operation_id": backend_id}
            if backend_id:
                saved = record.get("result") or {}
                if (record["arguments"]["op"] in SERVICE_ACTIONS and saved.get("verification_started_at")
                        and saved.get("handle", {}).get("state") == "succeeded"):
                    # The completed native receipt is durable. Subsequent polls only
                    # need live unit state, even if the upstream job is later retired.
                    await self.validate_binding(record)
                    result = dict(saved)
                else:
                    result = await self._observe_job(record, backend_id)
                handle = result.get("handle", {})
                if not isinstance(handle, dict) or handle.get("job_id") != backend_id:
                    raise ValueError("Upstream returned an unrelated job handle")
                state = handle.get("state")
                if state not in {"queued", "dispatching", "running", "reconciling", "succeeded", "failed", "cancelled", "timed_out", "outcome_unknown"}:
                    raise ValueError("Upstream returned an unknown job state")
                status = {"succeeded": "succeeded", "failed": "failed", "cancelled": "cancelled",
                          "timed_out": "failed", "outcome_unknown": "needs_attention"}.get(state, "running")
                if record["arguments"]["op"] in SERVICE_ACTIONS:
                    if status == "succeeded":
                        status = "needs_attention"
                        status, result, error = await verify_service(self, record, result)
                    elif status in {"failed", "needs_attention"}:
                        result.update(phase="service_failed" if status == "failed" else "outcome_unknown",
                            summary=f"{record['host_id']} 的 {record['arguments']['params'].get('unit', '服务')}操作未确认成功，上游任务状态为 {state}。",
                            instruction="不得声称服务已恢复正常；报告任务状态，必要时读取同一服务的状态和日志，不要重复执行。")
                if record["status"] == "cancelling" and status == "running":
                    cancelled = await self.client.call("jobs.cancel", {"job_id": backend_id,
                        "expected_revision": handle["revision"], "reason": "Administrator cancellation"})
                    result = cancelled.data
                    status = "cancelling"
            else:
                definition = await self._validate_submission(record)
                if record["arguments"]["op"] == "exec.run":
                    raise PermissionError("Legacy unchecked command must be prepared again with target-side checks")
                # Persist the claim before any effect; lost submissions are never blindly replayed.
                submission_started = True
                if self.client.backend_name == "ssh":
                    checkpoint = {**saved, "phase": "submitting", "submission_started": True,
                                  "submitted_at": int(time.time())}
                    await asyncio.to_thread(self.store.checkpoint_managed_operation, record["operation_id"],
                        owner=self.owner, fence=record["fence"], result=checkpoint)
                    result = checkpoint
                response = await self.client.call(record["arguments"]["op"], record["arguments"]["params"],
                    idempotency_key=(record["operation_id"] if definition["idempotency"] == "required" else None))
                result = response.data
                if definition.get("kind") == "job_submission":
                    backend_id = result.get("job_id") if isinstance(result, dict) else None
                    if not backend_id:
                        raise ValueError("Upstream accepted a submission without a durable job handle")
                    status = "running"
                else:
                    status = "succeeded"
        except (OpsError, PermissionError, ValueError, KeyError, TypeError) as exc:
            status = "needs_attention"
            error = getattr(exc, "code", type(exc).__name__)
            result = {**(record.get("result") or {}), **(result if isinstance(result, dict) else {}),
                      "error": str(exc)[:1000], "upstream_idempotency_key": record["operation_id"],
                      "submission_started": submission_started,
                      "instruction": "Check upstream jobs before retrying; the effect may already have happened."}
            if isinstance(result.get("verification"), dict):
                result["verification"] = {**result["verification"], "verified": False}
            result.update(phase="outcome_unknown", summary="无法确认操作最终结果：" + str(exc)[:400])
            # An unavailable observation must not turn an existing remote job into a failure.
            recoverable = backend_id or (submission_started and self.client.backend_name == "ssh")
            if recoverable and isinstance(exc, OpsError) and exc.retryable and record["deadline_at"] > int(time.time()):
                status = record["status"] if record["status"] == "cancelling" else "reconciling"
            elif isinstance(exc, PermissionError):
                status = "needs_attention"
            if not submission_started:
                status = "needs_attention" if isinstance(exc, PermissionError) else "failed"
                result["instruction"] = "Read-only validation failed. No write request was sent; review a new operation."
        await asyncio.to_thread(self.store.finish_managed_operation, record["operation_id"],
            owner=self.owner, fence=record["fence"], status=status, result=result,
            backend_id=backend_id, error=error)
        return True

    async def run(self) -> None:
        while not self.closed:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).exception("Management reconciliation failed")
            await asyncio.sleep(2)

    async def close(self) -> None:
        self.closed = True
        await self.client.close()
