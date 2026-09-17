from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from prometheus_client import make_asgi_app
from pydantic import BaseModel, ConfigDict, Field

from .api_deployments import build_deployment_router
from .api_reliability import build_reliability_router
from .api_resources import build_resource_router
from .auth import CredentialFileAuthenticator
from .deployment_service import DeploymentService
from .ops_deployment_runner import OpsDeploymentRunner
from .diagnostics import IncidentDiagnosticService
from .execution_service import ClusterExecutionService, WorkerAuthenticator
from .guardian import GuardianService
from .guardian import guardian_target_snapshot
from .execution_contracts import content_hash
from .guardian_ops import GuardianOpsBridge
from .reliability import ReliabilityStore
from .resource_policy import ResourcePolicyStore
from .service import FleetControlService
from .ops_management import OpsManagementService
from .adapters.ops import OpsError


_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_UNIT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,119}\.service")
_TARGET_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")


class DiagnosticRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: str = Field(min_length=1, max_length=64)
    host_id: str = Field(default="h610", min_length=1, max_length=64)
    target_id: str = Field(default="", max_length=64)
    subject: str = Field(default="", max_length=1000)
    requested_by: str = Field(default="gaoji", min_length=1, max_length=200)


class OperationPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host_id: str
    resource_ref: str
    operation: str
    arguments: dict[str, object] = Field(default_factory=dict)
    expected_state: dict[str, object] = Field(default_factory=dict)
    verification: dict[str, object] = Field(default_factory=dict)
    compensation: dict[str, object] = Field(default_factory=dict)
    deadline_at: int | None = None
    idempotency_key: str
    task_ref: str = ""
    step_ref: str = ""


class OperationApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_hash: str = Field(pattern="^[a-f0-9]{64}$")
    resource_version: int = Field(ge=1)


class OpsCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str = Field(min_length=1, max_length=80)
    params: dict[str, object] = Field(default_factory=dict)
    idempotency_key: str = Field(default="", max_length=160)


class WorkerHeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    boot_id: str
    protocol_version: int
    availability: str
    capabilities: list[str]
    runtime: dict[str, object]
    capacity: dict[str, int]
    public_base_url: str = ""


class WorkerCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fence: int = Field(ge=1)
    ok: bool
    result: dict[str, object] = Field(default_factory=dict)
    error_code: str = Field(default="", max_length=80)


class WorkerLeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fence: int = Field(ge=1)


class WorkerCheckpointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fence: int = Field(ge=1)
    phase: str
    format_version: int = Field(default=1, ge=1, le=10)
    executor_version: str = Field(min_length=1, max_length=80)
    state: dict[str, object] = Field(default_factory=dict)


class WorkerJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    payload: dict[str, object] = Field(default_factory=dict)
    constraints: dict[str, object] = Field(default_factory=dict)
    deadline_at: int | None = None
    idempotency_key: str


class ArtifactUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    media_type: str = Field(default="application/octet-stream", max_length=120)
    content_base64: str


def _read_api_token(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Control credential unavailable") from exc
    token = raw.rstrip(b"\r\n")
    if (
        len(raw) >= 515
        or not 32 <= len(token) <= 512
        or any(byte < 33 or byte > 126 for byte in token)
    ):
        raise HTTPException(status_code=503, detail="Control credential invalid")
    return token.decode("ascii")


def create_app(
    service: FleetControlService,
    *,
    api_token_file: str | Path,
    diagnostics: IncidentDiagnosticService | None = None,
    execution: ClusterExecutionService | None = None,
    worker_authenticator: WorkerAuthenticator | None = None,
    resource_policies: ResourcePolicyStore | None = None,
    reliability: ReliabilityStore | None = None,
    guardian: GuardianService | None = None,
    deployments: DeploymentService | None = None,
    deployer_authenticator: CredentialFileAuthenticator | None = None,
    management: OpsManagementService | None = None,
    guardian_ops: GuardianOpsBridge | None = None,
    ops_deployer: OpsDeploymentRunner | None = None,
) -> FastAPI:
    token_path = Path(api_token_file)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        guardian_task = asyncio.create_task(guardian.run()) if guardian is not None else None
        management_task = asyncio.create_task(management.run()) if management is not None else None
        deployment_task = asyncio.create_task(ops_deployer.run()) if ops_deployer is not None else None
        try:
            yield
        finally:
            if deployment_task is not None:
                deployment_task.cancel()
                await asyncio.gather(deployment_task, return_exceptions=True)
            if management_task is not None:
                management_task.cancel()
                await asyncio.gather(management_task, return_exceptions=True)
                await management.close()
            if guardian is not None:
                await guardian.close()
            if guardian_task is not None:
                guardian_task.cancel()
                await asyncio.gather(guardian_task, return_exceptions=True)
            if diagnostics is not None:
                await diagnostics.close()
            await service.close()

    app = FastAPI(
        title="gaoji Cluster Control",
        version="1",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.mount("/metrics", make_asgi_app(registry=service.metrics_registry))

    def authenticate(authorization: str = Header(default="")) -> None:
        expected = _read_api_token(token_path)
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(value, expected):
            raise HTTPException(status_code=401, detail="Unauthorized")

    async def signed_principal(
        request: Request,
        authorization: str = Header(default=""),
        actor: str = Header(default="", alias="X-KC-Actor"),
        origin: str = Header(default="", alias="X-KC-Origin"),
        timestamp: str = Header(default="", alias="X-KC-Time"),
        signature: str = Header(default="", alias="X-KC-Signature"),
    ) -> tuple[str, str]:
        authenticate(authorization)
        if not actor or len(actor) > 200 or not origin or len(origin) > 240:
            raise HTTPException(status_code=401, detail="Signed actor is required")
        try:
            request_time = int(timestamp)
        except ValueError:
            raise HTTPException(status_code=401, detail="Invalid request time") from None
        if abs(int(time.time()) - request_time) > 60:
            raise HTTPException(status_code=401, detail="Expired signed request")
        body = await request.body()
        message = "\n".join(
            (
                request.method.upper(), request.url.path, actor, origin,
                timestamp, hashlib.sha256(body).hexdigest(),
            )
        ).encode("utf-8")
        expected = hmac.new(
            _read_api_token(token_path).encode("ascii"), message, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=401, detail="Invalid signed actor")
        return actor, origin

    def worker_principal(authorization: str = Header(default="")) -> str:
        worker_id = (
            worker_authenticator.authenticate(authorization)
            if worker_authenticator is not None
            else None
        )
        if worker_id is None:
            raise HTTPException(status_code=401, detail="Unauthorized worker")
        return worker_id

    def valid_host(host: str) -> str:
        if _HOST_RE.fullmatch(host) is None:
            raise HTTPException(status_code=422, detail="Invalid host id")
        return host

    def valid_unit(unit: str) -> str:
        if _UNIT_RE.fullmatch(unit) is None:
            raise HTTPException(status_code=422, detail="Invalid unit name")
        return unit

    auth = [Depends(authenticate)]

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/backends", dependencies=auth)
    async def backends() -> dict[str, object]:
        snapshot = await asyncio.to_thread(service.backend_snapshot)
        return {"items": [snapshot]}

    @app.get("/v1/capabilities", dependencies=auth)
    async def capabilities() -> dict[str, object]:
        return await service.capabilities()

    @app.get("/v1/fleet", dependencies=auth)
    async def fleet() -> dict[str, object]:
        return await service.fleet_overview()

    @app.get("/v1/hosts/{host}", dependencies=auth)
    async def host(host: str) -> dict[str, object]:
        return await service.host_facts(valid_host(host))

    @app.get("/v1/hosts/{host}/metrics", dependencies=auth)
    async def host_metrics(host: str) -> dict[str, object]:
        return (await service.query_capability("host.metrics.read", {"host": valid_host(host)})).as_dict()

    @app.get("/v1/hosts/{host}/units/{unit}", dependencies=auth)
    async def unit(host: str, unit: str) -> dict[str, object]:
        return await service.unit_status(valid_host(host), valid_unit(unit))

    @app.get("/v1/hosts/{host}/units/{unit}/logs", dependencies=auth)
    async def logs(
        host: str,
        unit: str,
        lines: int = Query(default=50, ge=1, le=200),
        since_seconds: int = Query(default=3600, ge=1, le=86400),
    ) -> dict[str, object]:
        return await service.unit_logs(
            valid_host(host), valid_unit(unit), lines, since_seconds
        )

    @app.get("/v1/alerts", dependencies=auth)
    async def alerts() -> dict[str, object]:
        return await service.alerts()

    @app.get("/v1/failed-units", dependencies=auth)
    async def failed_units() -> dict[str, object]:
        return await service.failed_units()

    @app.get("/v1/observations", dependencies=auth)
    async def observations(
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, object]:
        items = await asyncio.to_thread(service.recent_observations, limit=limit)
        return {"items": items}

    @app.get("/v1/diagnostics/templates", dependencies=auth)
    async def diagnostic_templates() -> dict[str, object]:
        return {"items": diagnostics.templates() if diagnostics is not None else []}

    @app.get("/v1/diagnostics", dependencies=auth)
    async def diagnostic_runs(
        limit: int = Query(default=30, ge=1, le=100),
    ) -> dict[str, object]:
        items = (
            await asyncio.to_thread(diagnostics.recent, limit=limit)
            if diagnostics is not None
            else []
        )
        return {"items": items}

    @app.get("/v1/diagnostics/{run_id}", dependencies=auth)
    async def diagnostic_detail(run_id: int) -> dict[str, object]:
        if diagnostics is None:
            raise HTTPException(status_code=503, detail="Diagnostics unavailable")
        result = await asyncio.to_thread(diagnostics.detail, run_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Diagnostic run not found")
        return result

    @app.post("/v1/diagnostics", dependencies=auth)
    async def run_diagnostic(request: DiagnosticRunRequest) -> dict[str, object]:
        if diagnostics is None:
            raise HTTPException(status_code=503, detail="Diagnostics unavailable")
        host_id = valid_host(request.host_id)
        if request.target_id and _TARGET_RE.fullmatch(request.target_id) is None:
            raise HTTPException(status_code=422, detail="Invalid diagnostic target")
        try:
            result = await diagnostics.run(
                template_key=request.template,
                host_id=host_id,
                target_id=request.target_id,
                subject=request.subject,
                requested_by=request.requested_by,
            )
            conclusion = result.get("conclusion") if isinstance(result.get("conclusion"), dict) else {}
            counts = conclusion.get("counts") if isinstance(conclusion.get("counts"), dict) else {}
            if reliability is not None and int(counts.get("failed") or 0) > 0:
                await asyncio.to_thread(
                    reliability.observe_incident,
                    incident_key=f"diagnostic:{host_id}:{request.template}",
                    host_id=host_id, service_ref="",
                    severity="warning", summary=str(result.get("summary") or "排障发现失败证据"),
                    event_type="diagnostic_failed",
                    source_ref=str(result.get("handle") or f"diagnostic#{result.get('run_id') or ''}"),
                    confidence=str(result.get("confidence") or "unknown"),
                    payload={"failed_checks": conclusion.get("failed_checks", [])},
                )
            return result
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None

    def execution_service() -> ClusterExecutionService:
        if execution is None:
            raise HTTPException(status_code=503, detail="Execution control unavailable")
        return execution

    def policy_store() -> ResourcePolicyStore:
        if resource_policies is None:
            raise HTTPException(status_code=503, detail="Resource scheduling unavailable")
        return resource_policies

    def reliability_store() -> ReliabilityStore:
        if reliability is None:
            raise HTTPException(status_code=503, detail="Reliability service unavailable")
        return reliability

    def deployment_service() -> DeploymentService:
        if deployments is None:
            raise HTTPException(status_code=503, detail="Deployment service unavailable")
        return deployments

    @app.get("/v1/execution/capabilities", dependencies=auth)
    async def execution_capabilities() -> dict[str, object]:
        result = execution_service().capabilities()
        result["ops_management"] = {"available": management is not None,
            "backend": management.client.backend_name if management else "unavailable",
            "hosts": sorted(management.hosts) if management else [], "approval_required": True}
        result["guardians"]["remediation_available"] = guardian_ops is not None
        result["guardians"]["target_details"] = [
            {**target, "target_hash": content_hash(guardian_target_snapshot(target))}
            for target in execution_service().diagnostic_targets.values()
        ]
        return result

    def management_service() -> OpsManagementService:
        if management is None:
            raise HTTPException(503, "Ops management is not configured")
        return management

    @app.get("/v1/ops/catalog")
    async def ops_catalog(operation: str = "", principal: tuple[str, str] = Depends(signed_principal)):
        try:
            return await management_service().catalog(principal[0], operation)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except OpsError as exc:
            raise HTTPException(502, str(exc)) from None

    @app.post("/v1/ops/call")
    async def ops_call(body: OpsCallRequest, principal: tuple[str, str] = Depends(signed_principal)):
        try:
            return await management_service().call(body.operation, body.params, actor=principal[0],
                origin=principal[1], idempotency_key=body.idempotency_key)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except OpsError as exc:
            raise HTTPException(502, str(exc)) from None

    @app.get("/v1/ops/intent-receipts/{digest}")
    async def ops_receipt(digest: str,
                          principal: tuple[str, str] = Depends(signed_principal)):
        if re.fullmatch(r"[a-f0-9]{64}", digest) is None:
            raise HTTPException(422, "Invalid task intent digest")
        try:
            return await management_service().receipt("subagent:" + digest, actor=principal[0], origin=principal[1])
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.get("/v1/operations", dependencies=auth)
    async def operations(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, object]:
        return {"items": await asyncio.to_thread(execution_service().store.recent_operations, limit)}

    @app.post("/v1/operations/prepare")
    async def prepare_operation(
        body: OperationPrepareRequest,
        principal: tuple[str, str] = Depends(signed_principal),
    ) -> dict[str, object]:
        actor, origin = principal
        try:
            if management is not None:
                if body.operation not in {"service.start", "service.stop", "service.restart", "service.reload"} or any(
                    (body.arguments, body.expected_state, body.verification, body.compensation)
                ):
                    raise ValueError("Use ops_catalog and ops_call for upstream service preconditions; custom checks cannot be silently dropped")
                result = await management.call(body.operation.replace("service.", "units."),
                    {"host": body.host_id, "unit": body.resource_ref}, actor=actor, origin=origin,
                    idempotency_key=body.idempotency_key)
                return result["operation"]
            return await asyncio.to_thread(
                execution_service().prepare_operation,
                body.model_dump(exclude_none=True), actor_id=actor, origin_scope=origin,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except OpsError as exc:
            raise HTTPException(502, str(exc)) from None

    @app.get("/v1/operations/{operation_id}")
    async def operation(
        operation_id: str,
        principal: tuple[str, str] = Depends(signed_principal),
    ) -> dict[str, object]:
        actor, origin = principal
        try:
            item = await asyncio.to_thread(
                execution_service().operation_status,
                operation_id, actor_id=actor, origin_scope=origin,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
        if item is None:
            raise HTTPException(status_code=404, detail="Operation not found")
        return item

    @app.post("/v1/operations/{operation_id}/approve")
    async def approve_operation(
        operation_id: str,
        body: OperationApproveRequest,
        principal: tuple[str, str] = Depends(signed_principal),
    ) -> dict[str, object]:
        actor, _ = principal
        try:
            item = await asyncio.to_thread(execution_service().store.get_operation, operation_id)
            if item and item["operation"] in {"maxops.execute", "ssh.execute"}:
                return await management_service().approve(operation_id, actor=actor,
                    expected_hash=body.contract_hash, expected_version=body.resource_version)
            return await asyncio.to_thread(
                execution_service().approve_operation,
                operation_id, actor_id=actor, expected_hash=body.contract_hash,
                expected_version=body.resource_version,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

        except OpsError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @app.post("/v1/operations/{operation_id}/cancel")
    async def cancel_operation(
        operation_id: str,
        principal: tuple[str, str] = Depends(signed_principal),
    ) -> dict[str, object]:
        actor, _ = principal
        try:
            return await asyncio.to_thread(
                execution_service().store.cancel_operation, operation_id,
                actor_id=actor, origin_scope=principal[1],
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.get("/v1/workers", dependencies=auth)
    async def workers() -> dict[str, object]:
        return {"items": await asyncio.to_thread(execution_service().store.workers)}

    @app.post("/v1/worker/heartbeat")
    async def worker_heartbeat(
        body: WorkerHeartbeatRequest,
        worker_id: str = Depends(worker_principal),
    ) -> dict[str, object]:
        try:
            return await asyncio.to_thread(
                execution_service().heartbeat, worker_id, body.model_dump()
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.post("/v1/worker/claim")
    async def worker_claim(worker_id: str = Depends(worker_principal)) -> dict[str, object]:
        item = await asyncio.to_thread(execution_service().claim_job, worker_id)
        return {"job": item}

    @app.post("/v1/worker/jobs/{job_id}/complete")
    async def worker_complete(
        job_id: str,
        body: WorkerCompleteRequest,
        worker_id: str = Depends(worker_principal),
    ) -> dict[str, object]:
        try:
            return await asyncio.to_thread(
                execution_service().complete_job,
                job_id, worker_id=worker_id, fence=body.fence, ok=body.ok,
                result=body.result, error_code=body.error_code,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @app.post("/v1/worker/jobs/{job_id}/renew")
    async def worker_renew(
        job_id: str,
        body: WorkerLeaseRequest,
        worker_id: str = Depends(worker_principal),
    ) -> dict[str, int]:
        try:
            return await asyncio.to_thread(
                execution_service().store.renew_job,
                job_id, worker_id=worker_id, fence=body.fence,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @app.post("/v1/worker/jobs/{job_id}/checkpoints")
    async def worker_checkpoint(
        job_id: str,
        body: WorkerCheckpointRequest,
        worker_id: str = Depends(worker_principal),
    ) -> dict[str, object]:
        try:
            return await asyncio.to_thread(
                execution_service().save_checkpoint,
                job_id, worker_id=worker_id, fence=body.fence, phase=body.phase,
                format_version=body.format_version,
                executor_version=body.executor_version, state=body.state,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.get("/v1/worker/artifacts/{artifact_id}")
    async def worker_artifact(
        artifact_id: str,
        job_id: str = Query(min_length=36, max_length=36),
        fence: int = Query(ge=1),
        worker_id: str = Depends(worker_principal),
    ) -> Response:
        try:
            item, content = await asyncio.to_thread(
                execution_service().store.artifact_bytes_for_worker,
                artifact_id, job_id=job_id, worker_id=worker_id, fence=fence,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
        return Response(
            content,
            media_type=str(item["media_type"]),
            headers={
                "ETag": execution_service().artifact_etag(item),
                "X-Artifact-SHA256": str(item["sha256"]),
            },
        )

    @app.get("/v1/jobs", dependencies=auth)
    async def jobs(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, object]:
        return {"items": await asyncio.to_thread(execution_service().store.recent_jobs, limit)}

    @app.post("/v1/jobs")
    async def submit_job(
        body: WorkerJobRequest,
        principal: tuple[str, str] = Depends(signed_principal),
    ) -> dict[str, object]:
        actor, origin = principal
        try:
            return await asyncio.to_thread(
                execution_service().submit_job,
                body.model_dump(exclude_none=True), actor_id=actor, origin_scope=origin,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @app.get("/v1/jobs/{job_id}")
    async def job(
        job_id: str,
        principal: tuple[str, str] = Depends(signed_principal),
    ) -> dict[str, object]:
        actor, origin = principal
        try:
            item = await asyncio.to_thread(
                execution_service().job_status,
                job_id, actor_id=actor, origin_scope=origin,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
        if item is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return item

    @app.post("/v1/jobs/{job_id}/cancel")
    async def cancel_job(
        job_id: str,
        principal: tuple[str, str] = Depends(signed_principal),
    ) -> dict[str, object]:
        try:
            return await asyncio.to_thread(
                execution_service().store.cancel_job, job_id,
                actor_id=principal[0], origin_scope=principal[1],
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None

    @app.get("/v1/reservations", dependencies=auth)
    async def reservations() -> dict[str, object]:
        return {"items": await asyncio.to_thread(execution_service().store.reservations)}

    @app.post("/v1/artifacts")
    async def upload_artifact(
        body: ArtifactUploadRequest,
        principal: tuple[str, str] = Depends(signed_principal),
    ) -> dict[str, object]:
        actor, origin = principal
        try:
            item = await asyncio.to_thread(
                execution_service().store.store_artifact,
                actor_id=actor, origin_scope=origin, name=body.name,
                media_type=body.media_type, content_base64=body.content_base64,
            )
            item.pop("storage_ref", None)
            return item
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.get("/v1/previews", dependencies=auth)
    async def previews(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, object]:
        return {"items": await asyncio.to_thread(execution_service().store.previews, limit)}

    app.include_router(
        build_resource_router(
            auth=auth,
            signed_principal=signed_principal,
            policy_store=policy_store,
        )
    )
    app.include_router(
        build_reliability_router(
            auth=auth,
            signed_principal=signed_principal,
            reliability_store=reliability_store,
            execution_service=execution_service,
            guardian_ops=guardian_ops,
        )
    )
    app.include_router(
        build_deployment_router(
            auth=auth,
            signed_principal=signed_principal,
            deployment_service=deployment_service,
            deployer_authenticator=deployer_authenticator,
        )
    )

    return app
