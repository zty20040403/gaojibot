"""Resumable P7 orchestration; all host writes pass through the shared Ops ledger."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Mapping

from .adapters.ops import OpsError
from .deployment_contracts import approval_contract_hash
from .deployment_ops_protocol import DeploymentTargetBinding, OpsDeploymentProtocol, read_job_result
from .deployment_service import DeploymentService
from .execution_contracts import content_hash
from .ops_management import OpsManagementService


WRITES = ("workspace.create", "deploy.prepare", "deploy.run", "deploy.rollback")
IN_FLIGHT = {"awaiting_approval", "queued", "running", "reconciling", "cancelling"}


class PendingStep(Exception):
    pass


class FailedStep(Exception):
    def __init__(self, record: dict[str, Any]):
        self.record = record
        super().__init__(record.get("error_code") or record["status"])


class OpsDeploymentRunner:
    def __init__(self, service: DeploymentService, management: OpsManagementService) -> None:
        self.service, self.management = service, management
        self.store = service.store
        self.owner = "ops-" + uuid.uuid4().hex
        self.repositories = tuple(key for key, value in service.repositories.items() if value.get("backend") == "ops")
        service.ops_available = bool(self.repositories)
        management.deployment_validator = self.validate_dispatch

    def _policy(self, item: Mapping[str, Any]) -> Mapping[str, Any]:
        self.management.authorize(str(item["actor_id"]))
        repository = self.service.repositories.get(str(item["repository_id"]), {})
        if (item["contract"].get("backend") != "ops" or repository.get("backend") != "ops"
                or item["contract"].get("execution_policy_hash") != content_hash(repository)
                or not str(item["actor_id"]).startswith("admin:")
                or set(item["target_hosts"]) - self.management.hosts):
            raise PermissionError("Deployment policy or management scope changed")
        return repository

    def _protocol(self, item: Mapping[str, Any], host: str) -> OpsDeploymentProtocol:
        repository = self._policy(item)
        target = next(target for target in repository["targets"] if target["host_id"] == host)
        async def read(operation: str, params: dict[str, Any]) -> dict[str, Any]:
            result = await self.management.call(operation, params, actor=str(item["actor_id"]),
                origin=f"deployment:{item['deployment_id']}")
            if not isinstance(result.get("result"), dict):
                raise OpsError("invalid_response", "Ops deployment observation is not an object")
            return result["result"]
        return OpsDeploymentProtocol(read, DeploymentTargetBinding(host,
            str(target["ops_repository"]), str(target["ops_profile"]), str(item["source_revision"]),
            str(item["expected_remote_revision"]), f"nixosConfigurations.{target['flake_host']}.config.system.build.toplevel"))

    async def _catalog_hash(self) -> str:
        definitions = {item["name"]: item for item in await self.management.definitions()}
        if any(name not in definitions or definitions[name]["read_only"]
               or definitions[name]["idempotency"] != "required" for name in WRITES):
            raise PermissionError("Ops lacks idempotent deployment operations")
        return content_hash({name: self.management.binding_hash(definitions[name]) for name in WRITES})

    def _expected(self, item: Mapping[str, Any], target: Mapping[str, Any], stage: str) -> tuple[str, dict[str, Any]]:
        protocol = self._protocol(item, str(target["host_id"]))
        checkpoint = target["verification"].get("ops", {})
        if stage == "workspace":
            return "workspace.create", protocol.create_workspace_params()
        if stage == "prepare":
            return "deploy.prepare", protocol.prepare_params(checkpoint["workspace"])
        if stage == "build":
            return "deploy.run", {"change_id": checkpoint["change_id"],
                "expected_revision": checkpoint["prepared_revision"], "until": "built"}
        if stage == "activate":
            evidence = checkpoint["evidence"]
            return "deploy.run", {"change_id": evidence["change_id"],
                "expected_revision": evidence["change_revision"], "until": "verified"}
        if stage == "rollback":
            return "deploy.rollback", {"change_id": checkpoint["change_id"],
                "expected_revision": checkpoint["rollback_revision"]}
        raise PermissionError("Unsupported deployment step")

    def _authorize(self, item: Mapping[str, Any], operation: Mapping[str, Any], *, now: int) -> None:
        self._policy(item)
        arguments = operation["arguments"]
        binding = arguments["deployment"]
        host, stage = binding["host_id"], binding["stage"]
        targets = item["targets"]
        target = next(target for target in targets if target["host_id"] == host)
        expected_op, expected_params = self._expected(item, target, stage)
        if (binding["deployment_id"] != item["deployment_id"] or item.get("cancel_requested")
                or int(item["deadline_at"]) <= now or int(item["lease_expires_at"] or 0) <= now
                or item["status"] not in {"preflighting", "deploying", "verifying", "rolling_back"}
                or operation["actor_id"] != item["actor_id"]
                or operation["origin_scope"] != f"deployment:{item['deployment_id']}"
                or operation["idempotency_key"] != f"{item['deployment_id']}:{host}:{stage}"
                or arguments["op"] != expected_op or arguments["params"] != expected_params):
            raise PermissionError("Deployment operation differs from its active contract")
        if stage in {"workspace", "prepare", "build"}:
            if item["phase"] != "preflight" or target["status"] != "preflighting":
                raise PermissionError("Deployment preparation is no longer authorized")
        else:
            if (item["phase"] != "apply" or not item["approval_ref"]
                    or item["contract_hash"] != approval_contract_hash(item["contract"], item["preflight"])):
                raise PermissionError("Activation requires the reviewed preflight contract")
            approved = next(entry for entry in item["preflight"]["targets"] if entry["host_id"] == host)
            if target["verification"]["ops"]["evidence"] != approved:
                raise PermissionError("Target artifact differs from the reviewed preflight")
            if stage == "activate":
                earlier = targets[:targets.index(target)]
                if target["status"] != "deploying" or any(row["status"] != "succeeded" for row in earlier):
                    raise PermissionError("Prior deployment targets are not verified")
            elif item["failure_policy"] != "rollback_deployed" or target["status"] != "rolling_back":
                raise PermissionError("This deployment did not authorize rollback of this target")

    def _reserve(self, cursor: Any, raw: Mapping[str, Any], now: int, expected: Mapping[str, Any]) -> None:
        cursor.execute("SELECT deployment_id FROM fleet_deployments WHERE deployment_id=? FOR UPDATE",
                       (expected["deployment_id"],)).fetchone()
        item = self.store._get(cursor, str(expected["deployment_id"]))
        self.store._assert_lease(item, deployer_id=self.owner, fence=int(expected["fence"]), now=now)
        operation = {**raw, "arguments": json.loads(raw["arguments_json"])}
        self._authorize(item, operation, now=now)
        if item["phase"] == "apply":
            approval = cursor.execute("SELECT * FROM fleet_deployment_approvals WHERE approval_id=?",
                                      (item["approval_ref"],)).fetchone()
            if (approval is None or approval["consumed_at"] is None
                    or approval["contract_hash"] != item["contract_hash"]):
                raise PermissionError("Deployment approval was not consumed by this execution")

    async def validate_dispatch(self, operation: dict[str, Any]) -> None:
        item = await asyncio.to_thread(self.store.get, operation["arguments"]["deployment"]["deployment_id"])
        if item is None:
            raise PermissionError("Deployment was removed")
        self._authorize(item, operation, now=int(time.time()))
        target = next(row for row in item["targets"] if row["host_id"] == operation["arguments"]["deployment"]["host_id"])
        if target["verification"]["ops"]["catalog_hash"] != await self._catalog_hash():
            raise PermissionError("Deployment backend changed after preparation")
        if not operation.get("approval_ref"):
            raise PermissionError("Deployment child was not authorized")
        if operation["arguments"]["deployment"]["stage"] == "activate":
            await self._protocol(item, str(target["host_id"])).activation_params(target["verification"]["ops"]["evidence"])

    async def _effect(self, item: dict[str, Any], target: dict[str, Any], stage: str) -> dict[str, Any]:
        operation, params = self._expected(item, target, stage)
        if target["verification"]["ops"]["catalog_hash"] != await self._catalog_hash():
            raise PermissionError("Deployment backend changed after preparation")
        result = await self.management.call(operation, params, actor=item["actor_id"],
            origin=f"deployment:{item['deployment_id']}",
            idempotency_key=f"{item['deployment_id']}:{target['host_id']}:{stage}",
            deployment={"deployment_id": item["deployment_id"], "host_id": target["host_id"], "stage": stage})
        record = result["operation"]
        if record["status"] == "awaiting_approval":
            await self.management.validate_binding(record)
            record = await asyncio.to_thread(self.management.store.approve_operation, record["operation_id"],
                actor_id=item["actor_id"], expected_hash=record["contract_hash"],
                expected_version=int(record["resource_version"]), expires_at=min(item["deadline_at"], int(time.time()) + 300),
                before_approve=lambda cursor, row, now: self._reserve(cursor, row, now, item))
        if record["status"] in IN_FLIGHT:
            raise PendingStep()
        if record["status"] != "succeeded":
            raise FailedStep(record)
        return record

    async def _update(self, item: dict[str, Any], target: dict[str, Any], status: str, step: str,
                      **fields: Any) -> None:
        saved = await asyncio.to_thread(self.service.update_target, item["deployment_id"], target["host_id"],
            deployer_id=self.owner, fence=item["fence"], status=status, step=step,
            verification=target["verification"], **fields)
        item.update(saved)

    async def _preflight(self, item: dict[str, Any]) -> None:
        for target in item["targets"]:
            if target["status"] == "build_ready":
                continue
            protocol = self._protocol(item, target["host_id"])
            checkpoint = target["verification"].setdefault("ops", {})
            if not checkpoint:
                checkpoint["catalog_hash"] = await self._catalog_hash()
                await self._update(item, target, "preflighting", "workspace")
                return
            if "workspace" not in checkpoint:
                record = await self._effect(item, target, "workspace")
                result = await read_job_result(protocol.read, record["backend_operation_id"])
                workspace = result.get("workspace", {})
                checkpoint["workspace"] = await protocol.workspace(str(workspace.get("workspace_id") or ""))
                await self._update(item, target, "preflighting", "prepare")
                return
            if "change_id" not in checkpoint:
                record = await self._effect(item, target, "prepare")
                change = await protocol.change(record["backend_operation_id"])
                if (change["state"] != "prepared" or change["plan"].get("workspace_id") != checkpoint["workspace"]["workspace_id"]
                        or change["plan"].get("workspace_revision") != checkpoint["workspace"]["revision"]):
                    raise OpsError("invalid_preflight", "Ops did not produce a prepared change")
                checkpoint.update(change_id=change["plan"]["change_id"], prepared_revision=change["revision"])
                await self._update(item, target, "preflighting", "build")
                return
            await self._effect(item, target, "build")
            checkpoint["evidence"] = await protocol.preflight(checkpoint["change_id"])
            await self._update(item, target, "build_ready", "ready",
                current_toplevel=checkpoint["evidence"]["current_toplevel"],
                target_toplevel=checkpoint["evidence"]["target_toplevel"])
            return
        evidence = [target["verification"]["ops"]["evidence"] for target in item["targets"]]
        hashes = {entry["flake_lock_sha256"] for entry in evidence}
        if len(hashes) != 1:
            raise ValueError("Deployment hosts built different flake.lock contents")
        await self._complete(item, True, {"backend": "ops-management-v2", "targets": evidence,
            "resolved_revision": item["source_revision"], "remote_revision": item["expected_remote_revision"],
            "flake_lock_sha256": evidence[0]["flake_lock_sha256"]})

    async def _apply(self, item: dict[str, Any]) -> None:
        if any(target["status"] in {"failed", "unknown", "rollback_pending", "rolling_back", "rolled_back", "rollback_failed"}
               for target in item["targets"]):
            await self._recover(item)
            return
        for target in item["targets"]:
            if target["status"] == "succeeded":
                continue
            evidence = target["verification"]["ops"]["evidence"]
            protocol = self._protocol(item, target["host_id"])
            if target["status"] == "build_ready":
                await protocol.activation_params(evidence)
                await self._update(item, target, "deploying", "activate")
                return
            try:
                await self._effect(item, target, "activate")
            except FailedStep as exc:
                if exc.record.get("result", {}).get("submission_started") is False:
                    await self._update(item, target, "failed", "validation_failed",
                                       error_code="activation_not_submitted")
                    return
                change = await protocol.change(evidence["change_id"])
                if change["state"] == "succeeded":
                    report = await protocol.verification(evidence)
                    await self._update(item, target, "verifying", "reconciled")
                    await self._update(item, target, "succeeded", "verified", actual_toplevel=report["actual_toplevel"])
                elif change["state"] == "ready" and exc.record["status"] in {"failed", "cancelled"} and exc.record.get("backend_operation_id"):
                    await self._update(item, target, "failed", "activation_rejected", error_code="activation_rejected")
                elif change["state"] == "rolled_back":
                    report = await protocol.verification(evidence, rollback=True)
                    await self._update(item, target, "unknown", "upstream_recovered")
                    target = next(row for row in item["targets"] if row["host_id"] == target["host_id"])
                    await self._update(item, target, "rollback_pending", "upstream_recovered")
                    await self._update(item, target, "rolling_back", "upstream_recovered")
                    await self._update(item, target, "rolled_back", "upstream_recovered", actual_toplevel=report["actual_toplevel"])
                else:
                    await self._update(item, target, "unknown", "activation_unknown", error_code="activation_outcome_unknown")
                return
            report = await protocol.verification(evidence)
            if target["status"] == "deploying":
                await self._update(item, target, "verifying", "verify")
            await self._update(item, target, "succeeded", "verified", actual_toplevel=report["actual_toplevel"])
            return
        await self._complete(item, True, {"backend": "ops-management-v2", "verified_hosts": item["target_hosts"]})

    async def _recover(self, item: dict[str, Any]) -> None:
        for target in item["targets"]:
            if target["status"] == "build_ready":
                await self._update(item, target, "skipped", "prior_target_failed")
        if any(target["status"] in {"unknown", "rollback_failed"} for target in item["targets"]):
            await self._complete(item, False, {"error_code": "deployment_outcome_unknown"})
            return
        if item["failure_policy"] == "rollback_deployed":
            for target in reversed(item["targets"]):
                if target["status"] not in {"succeeded", "rollback_pending", "rolling_back"}:
                    continue
                checkpoint = target["verification"]["ops"]
                protocol = self._protocol(item, target["host_id"])
                if target["status"] == "succeeded":
                    await self._update(item, target, "rollback_pending", "rollback")
                    return
                if target["status"] == "rollback_pending":
                    change = await protocol.change(checkpoint["change_id"])
                    if change["plan"] != checkpoint["evidence"]["plan"] or change["state"] != "succeeded":
                        await self._update(item, target, "rollback_failed", "runtime_changed", error_code="rollback_baseline_changed")
                        return
                    checkpoint["rollback_revision"] = change["revision"]
                    await self._update(item, target, "rolling_back", "rollback")
                    return
                try:
                    await self._effect(item, target, "rollback")
                    report = await protocol.verification(checkpoint["evidence"], rollback=True)
                except FailedStep:
                    await self._update(item, target, "rollback_failed", "rollback_failed", error_code="rollback_failed")
                    return
                await self._update(item, target, "rolled_back", "rolled_back", actual_toplevel=report["actual_toplevel"])
                return
        await self._complete(item, False, {"error_code": "target_failed", "failure_policy": item["failure_policy"]})

    async def _cancel(self, item: dict[str, Any]) -> None:
        pending = False
        for target in item["targets"]:
            for stage in ("workspace", "prepare", "build", "activate", "rollback"):
                record = await asyncio.to_thread(self.management.store.find_operation, item["actor_id"],
                    f"deployment:{item['deployment_id']}", f"{item['deployment_id']}:{target['host_id']}:{stage}")
                if record and record["status"] in IN_FLIGHT:
                    if record["status"] in {"awaiting_approval", "queued"}:
                        await asyncio.to_thread(self.management.store.cancel_operation, record["operation_id"],
                            actor_id=item["actor_id"], origin_scope=f"deployment:{item['deployment_id']}")
                    else:
                        pending = True
            if target["status"] in {"deploying", "verifying", "rolling_back"} and not pending:
                await self._update(item, target, "unknown" if target["status"] != "rolling_back" else "rollback_failed",
                    "cancelled_needs_reconciliation", error_code="cancelled_during_effect")
        if not pending:
            for target in item["targets"]:
                if target["status"] in {"pending", "preflighting", "build_ready"}:
                    await self._update(item, target, "skipped", "execution_cancelled")
            await self._complete(item, False, {"error_code": "cancelled_or_deadline"})

    async def _complete(self, item: dict[str, Any], ok: bool, result: dict[str, Any]) -> None:
        await asyncio.to_thread(self.service.complete, item["deployment_id"],
            deployer_id=self.owner, fence=item["fence"], ok=ok, result=result)

    async def _stop_after_error(self, item: dict[str, Any], exc: Exception) -> None:
        error = {"error_code": getattr(exc, "code", type(exc).__name__), "message": str(exc)[:500]}
        if item["phase"] == "preflight":
            for target in item["targets"]:
                if target["status"] == "preflighting":
                    await self._update(item, target, "failed", "preflight_failed", error_code=error["error_code"])
                elif target["status"] == "pending":
                    await self._update(item, target, "skipped", "preflight_aborted")
            await self._complete(item, False, error)
            return
        saved = await asyncio.to_thread(self.store.cancel, item["deployment_id"],
            actor_id=item["actor_id"], origin_scope=item["origin_scope"],
            reason=f"Execution stopped: {error['error_code']}: {error['message']}")
        await self._cancel(saved)

    async def _tick(self, item: dict[str, Any]) -> None:
        if item["cancel_requested"] or int(item["deadline_at"]) <= int(time.time()):
            await self._cancel(item)
            return
        self._policy(item)
        if item["phase"] == "preflight":
            await self._preflight(item)
        else:
            await self._apply(item)

    async def run_once(self) -> bool:
        item = await asyncio.to_thread(self.store.claim, self.owner, repository_ids=self.repositories,
            lease_seconds=180, backend="ops")
        if item is None:
            return False
        try:
            await asyncio.wait_for(self._tick(item), timeout=120)
        except (PendingStep, asyncio.TimeoutError):
            pass
        except OpsError as exc:
            if not exc.retryable:
                await self._stop_after_error(item, exc)
        except (FailedStep, PermissionError, ValueError, KeyError, StopIteration) as exc:
            await self._stop_after_error(item, exc)
        return True

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).exception("Ops deployment reconciliation failed")
            await asyncio.sleep(2)
