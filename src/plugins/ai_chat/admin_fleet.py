from __future__ import annotations

from src.bot_security.service import actor as authenticated_actor, audit_actor

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .fleet_client import FleetControlError


_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_UNIT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,119}\.service")
_TARGET_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")


class FleetDiagnosticRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: str = Field(min_length=1, max_length=64)
    host_id: str = Field(default="local", min_length=1, max_length=64)
    target_id: str = Field(default="", max_length=64)
    subject: str = Field(default="", max_length=1000)


class FleetOperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host_id: str
    resource_ref: str
    operation: str
    arguments: dict[str, object] = Field(default_factory=dict)
    expected_state: dict[str, object] = Field(default_factory=dict)
    verification: dict[str, object] = Field(default_factory=dict)
    compensation: dict[str, object] = Field(default_factory=dict)
    idempotency_key: str


class FleetApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_hash: str = Field(pattern="^[a-f0-9]{64}$")
    resource_version: int = Field(ge=1)


class OpsCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str = Field(min_length=1, max_length=120)
    params: dict[str, Any]
    idempotency_key: str = Field(default="", max_length=160)


class FleetDeploymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository_id: str
    source_revision: str = Field(pattern="^[a-f0-9]{40}$")
    expected_remote_revision: str = Field(default="", pattern="^$|^[a-f0-9]{40}$")
    target_hosts: list[str] = Field(min_length=1, max_length=8)
    requested_changes: list[str] = Field(min_length=1, max_length=32)
    strategy: str = "serial"
    canary_host_id: str = ""
    failure_policy: str = "pause"
    deadline_at: int | None = None
    idempotency_key: str


class FleetJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    payload: dict[str, object] = Field(default_factory=dict)
    constraints: dict[str, object] = Field(default_factory=dict)
    idempotency_key: str


class FleetWorkerAvailabilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    desired_availability: str
    reason: str = Field(default="", max_length=500)
    resource_version: int = Field(ge=1)


class FleetWorkerCapacityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_gpu: bool = False
    cpu_limit_millis: int = Field(ge=0, le=128_000)
    memory_limit_bytes: int = Field(ge=0, le=128 * 1024**3)
    gpu_limit_slots: int = Field(default=0, ge=0, le=16)
    resource_version: int = Field(ge=1)


class FleetGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: str
    grantee_actor_id: str
    origin_scope: str
    allowed_kinds: list[str]
    valid_until: int
    cpu_millis: int
    memory_bytes: int
    gpu_slots: int = 0
    priority: str = "normal"
    max_cost_microunits: int = 0


class FleetStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    resource_version: int = Field(ge=1)


class FleetGuardianRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: str
    host_id: str = ""
    service_ref: str = ""
    mode: str = "observe"
    expires_at: int
    interval_seconds: int = 60
    failure_threshold: int = 3
    max_actions: int = 0
    probe_policy: dict[str, object] = Field(default_factory=dict)
    authorized_action: dict[str, object] = Field(default_factory=dict)
    confirm_remediation: bool = False
    expected_target_hash: str = ""


class FleetRunbookCaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=240)
    host_id: str = Field(default="", max_length=64)
    service_ref: str = Field(default="", max_length=160)
    symptoms: str = Field(min_length=1, max_length=4000)
    confirmed_cause: str = Field(min_length=1, max_length=4000)
    resolution: list[str | dict[str, object]] = Field(min_length=1)
    applicability: dict[str, object] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(min_length=1)
    status: str = "verified"
    confidence: str = "confirmed"


def _validate(value: str, pattern: re.Pattern[str], kind: str) -> str:
    if pattern.fullmatch(value) is None:
        raise HTTPException(status_code=422, detail=f"invalid {kind}")
    return value


async def _safe_call(
    name: str,
    operation: Callable[[], Awaitable[dict[str, Any]]],
) -> tuple[str, dict[str, object]]:
    try:
        payload = await operation()
    except FleetControlError as exc:
        payload = {
            "ok": False,
            "status": "unavailable",
            "error": {
                "code": exc.code,
                "message": str(exc),
                "retryable": exc.retryable,
            },
        }
    except Exception:
        payload = {
            "ok": False,
            "status": "unavailable",
            "error": {
                "code": "control_unavailable",
                "message": "集群控制服务暂时不可用",
                "retryable": True,
            },
        }
    return name, payload


def _optional_call(
    client: Any,
    method_name: str,
    fallback: dict[str, Any],
    **kwargs: Any,
) -> Callable[[], Awaitable[dict[str, Any]]]:
    """Keep the console usable while bot and control plane roll independently."""

    async def invoke() -> dict[str, Any]:
        method = getattr(client, method_name, None)
        if not callable(method):
            return fallback
        return await method(**kwargs)

    return invoke


def register_fleet_admin_routes(
    router: APIRouter,
    services: Any,
    authorize: Callable[[Optional[str]], None],
    versioned: Callable[[str, dict[str, object]], dict[str, object]],
    authorize_management: Callable[[Optional[str]], None] | None = None,
) -> None:
    def management_auth(authorization: Optional[str]) -> None:
        if authorize_management is None:
            raise HTTPException(503, "服务器管理需要已配置的管理员认证")
        authorize_management(authorization)

    @router.get("/api/fleet/ops/catalog")
    async def ops_catalog(operation: str = "", authorization: Optional[str] = Header(default=None)):
        management_auth(authorization)
        try:
            return await configured_client().ops_catalog(operation, actor=authenticated_actor(), origin="admin-console")
        except FleetControlError as exc:
            raise HTTPException(502, str(exc)) from None

    @router.post("/api/fleet/ops/call")
    async def ops_call(request: OpsCallRequest, authorization: Optional[str] = Header(default=None)):
        management_auth(authorization)
        try:
            return await configured_client().ops_call(request.model_dump(), actor=authenticated_actor(), origin="admin-console")
        except FleetControlError as exc:
            raise HTTPException(502, str(exc)) from None

    @router.get("/api/fleet/operations/{operation_id}")
    async def operation_detail(operation_id: str, authorization: Optional[str] = Header(default=None)):
        management_auth(authorization)
        try:
            return await configured_client().operation(operation_id, actor=authenticated_actor(), origin="admin-console")
        except FleetControlError as exc:
            raise HTTPException(502, str(exc)) from None

    @router.get("/api/fleet")
    async def fleet(
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        client = services.fleet_client
        if client is None:
            return versioned(
                "fleet",
                {
                    "configured": False,
                    "fleet": {"status": "unavailable", "inventory": []},
                    "backends": {"items": []},
                    "capabilities": {"capabilities": []},
                    "observations": {"items": []},
                    "diagnostic_templates": {"items": []},
                    "diagnostics": {"items": []},
                    "execution_capabilities": {},
                    "operations": {"items": []},
                    "deployment_capabilities": {},
                    "deployments": {"items": []},
                    "workers": {"items": []},
                    "jobs": {"items": []},
                    "reservations": {"items": []},
                    "previews": {"items": []},
                    "resource_policies": {"items": []},
                    "borrow_grants": {"items": []},
                    "incidents": {"items": []},
                    "runbook_cases": {"items": []},
                    "guardians": {"items": []},
                },
            )
        results = await asyncio.gather(
            _safe_call("fleet", _optional_call(client, "fleet", {"status": "unavailable", "inventory": []})),
            _safe_call("backends", _optional_call(client, "backends", {"items": []})),
            _safe_call("capabilities", _optional_call(client, "capabilities", {"capabilities": []})),
            _safe_call("observations", _optional_call(client, "observations", {"items": []}, limit=50)),
            _safe_call("diagnostic_templates", _optional_call(client, "diagnostic_templates", {"items": []})),
            _safe_call("diagnostics", _optional_call(client, "diagnostics", {"items": []}, limit=30)),
            _safe_call("execution_capabilities", _optional_call(client, "execution_capabilities", {})),
            _safe_call("operations", _optional_call(client, "operations", {"items": []}, limit=50)),
            _safe_call("deployment_capabilities", _optional_call(client, "deployment_capabilities", {})),
            _safe_call("deployments", _optional_call(client, "deployments", {"items": []}, limit=50)),
            _safe_call("workers", _optional_call(client, "workers", {"items": []})),
            _safe_call("jobs", _optional_call(client, "jobs", {"items": []}, limit=50)),
            _safe_call("reservations", _optional_call(client, "reservations", {"items": []})),
            _safe_call("previews", _optional_call(client, "previews", {"items": []}, limit=50)),
            _safe_call("resource_policies", _optional_call(client, "resource_policies", {"items": []})),
            _safe_call("borrow_grants", _optional_call(client, "borrow_grants", {"items": []}, limit=100)),
            _safe_call("incidents", _optional_call(client, "incidents", {"items": []}, limit=100)),
            _safe_call("runbook_cases", _optional_call(client, "runbook_cases", {"items": []}, limit=100)),
            _safe_call("guardians", _optional_call(client, "guardians", {"items": []}, limit=100)),
        )
        return versioned("fleet", {"configured": True, **dict(results)})

    def configured_client() -> Any:
        client = services.fleet_client
        if client is None:
            raise HTTPException(status_code=503, detail="集群控制服务尚未配置")
        return client

    @router.get("/api/fleet/hosts/{host_id}")
    async def fleet_host(
        host_id: str,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        try:
            return await configured_client().host(
                _validate(host_id, _HOST_RE, "host id")
            )
        except FleetControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @router.get("/api/fleet/hosts/{host_id}/units/{unit}")
    async def fleet_unit(
        host_id: str,
        unit: str,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        try:
            return await configured_client().unit(
                _validate(host_id, _HOST_RE, "host id"),
                _validate(unit, _UNIT_RE, "unit"),
            )
        except FleetControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @router.get("/api/fleet/hosts/{host_id}/units/{unit}/logs")
    async def fleet_logs(
        host_id: str,
        unit: str,
        lines: int = Query(default=50, ge=1, le=200),
        since_seconds: int = Query(default=3600, ge=1, le=86400),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        try:
            return await configured_client().logs(
                _validate(host_id, _HOST_RE, "host id"),
                _validate(unit, _UNIT_RE, "unit"),
                lines=lines,
                since_seconds=since_seconds,
            )
        except FleetControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @router.get("/api/fleet/diagnostics/{run_id}")
    async def fleet_diagnostic_detail(
        run_id: int,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        try:
            return await configured_client().diagnostic(run_id)
        except FleetControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @router.post("/api/fleet/diagnostics")
    async def run_fleet_diagnostic(
        request: FleetDiagnosticRequest,
        authorization: Optional[str] = Header(default=None),
        admin_actor: Optional[str] = Header(default=None, alias="X-Admin-Actor"),
    ) -> dict[str, object]:
        authorize(authorization)
        host_id = _validate(request.host_id, _HOST_RE, "host id")
        if request.target_id and _TARGET_RE.fullmatch(request.target_id) is None:
            raise HTTPException(status_code=422, detail="invalid target id")
        actor = audit_actor()
        try:
            return await configured_client().run_diagnostic(
                template=request.template,
                host_id=host_id,
                target_id=request.target_id,
                subject=request.subject,
                requested_by=actor,
            )
        except FleetControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @router.post("/api/fleet/operations")
    async def prepare_fleet_operation(
        request: FleetOperationRequest,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        management_auth(authorization)
        try:
            return await configured_client().prepare_operation(
                request.model_dump(), actor=authenticated_actor(), origin="admin-console"
            )
        except FleetControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @router.get("/api/fleet/deployments/{deployment_id}")
    async def fleet_deployment_detail(
        deployment_id: str,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        try:
            return await configured_client().deployment(
                deployment_id,
                actor=authenticated_actor(),
                origin="admin-console",
            )
        except FleetControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @router.post("/api/fleet/deployments")
    async def prepare_fleet_deployment(
        request: FleetDeploymentRequest,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        management_auth(authorization)
        try:
            return await configured_client().prepare_deployment(
                request.model_dump(exclude_none=True),
                actor=authenticated_actor(),
                origin="admin-console",
            )
        except FleetControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @router.post("/api/fleet/deployments/{deployment_id}/approve")
    async def approve_fleet_deployment(
        deployment_id: str,
        request: FleetApprovalRequest,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        management_auth(authorization)
        try:
            return await configured_client().approve_deployment(
                deployment_id,
                request.contract_hash,
                request.resource_version,
                actor=authenticated_actor(),
                origin="admin-console",
            )
        except FleetControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @router.post("/api/fleet/deployments/{deployment_id}/cancel")
    async def cancel_fleet_deployment(
        deployment_id: str,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        management_auth(authorization)
        try:
            return await configured_client().cancel_deployment(
                deployment_id,
                actor=authenticated_actor(),
                origin="admin-console",
            )
        except FleetControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @router.post("/api/fleet/operations/{operation_id}/approve")
    async def approve_fleet_operation(
        operation_id: str,
        request: FleetApprovalRequest,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        management_auth(authorization)
        try:
            return await configured_client().approve_operation(
                operation_id, request.contract_hash, request.resource_version,
                actor=authenticated_actor(), origin="admin-console",
            )
        except FleetControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @router.post("/api/fleet/operations/{operation_id}/cancel")
    async def cancel_fleet_operation(
        operation_id: str,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        management_auth(authorization)
        try:
            return await configured_client().cancel_operation(
                operation_id, actor=authenticated_actor(), origin="admin-console"
            )
        except FleetControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @router.post("/api/fleet/jobs")
    async def submit_fleet_job(
        request: FleetJobRequest,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        try:
            return await configured_client().submit_job(
                request.model_dump(), actor=authenticated_actor(), origin="admin-console"
            )
        except FleetControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @router.post("/api/fleet/jobs/{job_id}/cancel")
    async def cancel_fleet_job(
        job_id: str,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        try:
            return await configured_client().cancel_job(
                job_id, actor=authenticated_actor(), origin="admin-console"
            )
        except FleetControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @router.post("/api/fleet/workers/{worker_id}/availability")
    async def set_fleet_worker_availability(
        worker_id: str,
        request: FleetWorkerAvailabilityRequest,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        try:
            return await configured_client().set_worker_availability(
                _validate(worker_id, _HOST_RE, "worker id"), request.model_dump(),
                actor=authenticated_actor(), origin="admin-console",
            )
        except FleetControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @router.post("/api/fleet/workers/{worker_id}/capacity")
    async def configure_fleet_worker_capacity(
        worker_id: str,
        request: FleetWorkerCapacityRequest,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        try:
            return await configured_client().configure_worker_capacity(
                _validate(worker_id, _HOST_RE, "worker id"), request.model_dump(),
                actor=authenticated_actor(), origin="admin-console",
            )
        except FleetControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @router.post("/api/fleet/borrow-grants")
    async def create_fleet_borrow_grant(
        request: FleetGrantRequest,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        try:
            return await configured_client().create_borrow_grant(
                request.model_dump(), actor=authenticated_actor(), origin="admin-console"
            )
        except FleetControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @router.post("/api/fleet/borrow-grants/{grant_id}/status")
    async def set_fleet_borrow_grant_status(
        grant_id: str,
        request: FleetStatusRequest,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        try:
            return await configured_client().set_borrow_grant_status(
                grant_id, request.model_dump(),
                actor=authenticated_actor(), origin="admin-console",
            )
        except FleetControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @router.post("/api/fleet/guardians")
    async def create_fleet_guardian(
        request: FleetGuardianRequest,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        management_auth(authorization)
        try:
            return await configured_client().create_guardian(
                request.model_dump(), actor=authenticated_actor(), origin="admin-console"
            )
        except FleetControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @router.post("/api/fleet/runbook-cases")
    async def create_fleet_runbook_case(
        request: FleetRunbookCaseRequest,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        try:
            return await configured_client().create_runbook_case(
                request.model_dump(), actor=authenticated_actor(), origin="admin-console"
            )
        except FleetControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @router.post("/api/fleet/guardians/{guardian_id}/status")
    async def set_fleet_guardian_status(
        guardian_id: str,
        request: FleetStatusRequest,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        try:
            return await configured_client().set_guardian_status(
                guardian_id, request.model_dump(),
                actor=authenticated_actor(), origin="admin-console",
            )
        except FleetControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
