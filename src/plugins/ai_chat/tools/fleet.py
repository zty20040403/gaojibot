"""Cluster tool handlers; identity and authorization remain host controlled."""

from __future__ import annotations

import json
import re
from functools import partial

import httpx

from ..ai_tools import (
    OPS_CATALOG_TOOL,
    OPS_CALL_TOOL,
    SERVICE_CONTROL_TOOL,
    HOST_REBOOT_TOOL,
    FLEET_OVERVIEW_TOOL,
    HOST_INSPECT_TOOL,
    SERVICE_INSPECT_TOOL,
    MODEL_STATUS_TOOL,
    SERVICE_LOGS_TOOL,
    DIAGNOSE_INCIDENT_TOOL,
    OPERATION_PREPARE_TOOL,
    OPERATION_STATUS_TOOL,
    OPERATION_CANCEL_TOOL,
    CLUSTER_ARTIFACT_UPLOAD_TOOL,
    CLUSTER_JOB_SUBMIT_TOOL,
    CLUSTER_JOB_STATUS_TOOL,
    CLUSTER_CASE_SEARCH_TOOL,
    CLUSTER_GUARDIAN_CREATE_TOOL,
    CLUSTER_GUARDIAN_STATUS_TOOL,
    OPS_CATALOG_TOOL_NAME,
    OPS_CALL_TOOL_NAME,
    SERVICE_CONTROL_TOOL_NAME,
    HOST_REBOOT_TOOL_NAME,
    FLEET_OVERVIEW_TOOL_NAME,
    HOST_INSPECT_TOOL_NAME,
    SERVICE_INSPECT_TOOL_NAME,
    MODEL_STATUS_TOOL_NAME,
    SERVICE_LOGS_TOOL_NAME,
    DIAGNOSE_INCIDENT_TOOL_NAME,
    OPERATION_PREPARE_TOOL_NAME,
    OPERATION_STATUS_TOOL_NAME,
    OPERATION_CANCEL_TOOL_NAME,
    CLUSTER_ARTIFACT_UPLOAD_TOOL_NAME,
    CLUSTER_JOB_SUBMIT_TOOL_NAME,
    CLUSTER_JOB_STATUS_TOOL_NAME,
    CLUSTER_CASE_SEARCH_TOOL_NAME,
    CLUSTER_GUARDIAN_CREATE_TOOL_NAME,
    CLUSTER_GUARDIAN_STATUS_TOOL_NAME,
    host_operation_call,
)
from ..fleet_client import FleetControlError
from ..fleet_case_recall import semantic_runbook_scores
from ..fleet_tools import fleet_overview, inspect_host, model_status
from .registry import FunctionTool
from .state import ToolContext, TurnState


MANAGEMENT_TOOLS = frozenset(
    {
        OPS_CATALOG_TOOL_NAME,
        OPS_CALL_TOOL_NAME,
        SERVICE_CONTROL_TOOL_NAME,
        HOST_REBOOT_TOOL_NAME,
    }
)


def registered_admin(context: ToolContext) -> bool:
    mobile = getattr(context.app, "mobile_authorization", None)
    return (
        mobile is not None
        and mobile.store.account_for_qq(str(context.event.user_id)) is not None
    )


def enabled(context: ToolContext, *, name: str) -> bool:
    if "fleet" not in context.capabilities or context.app.fleet_client is None:
        return False
    if name == SERVICE_LOGS_TOOL_NAME:
        return "fleet_logs" in context.capabilities
    if name in MANAGEMENT_TOOLS:
        return "ops" in context.capabilities and registered_admin(context)
    return True


async def execute(turn: TurnState, arguments: dict[str, object], *, name: str) -> str:
    client = turn.context.app.fleet_client
    if not ("fleet" in turn.context.capabilities) or client is None:
        return json.dumps(
            {"ok": False, "error": "当前会话无权读取服务器集群状态。"},
            ensure_ascii=False,
        )
    if name == SERVICE_LOGS_TOOL_NAME and not (
        "fleet_logs" in turn.context.capabilities
    ):
        return json.dumps(
            {"ok": False, "error": "当前会话无权读取服务器日志。"},
            ensure_ascii=False,
        )
    if name in {
        OPS_CATALOG_TOOL_NAME,
        OPS_CALL_TOOL_NAME,
        SERVICE_CONTROL_TOOL_NAME,
        HOST_REBOOT_TOOL_NAME,
    } and not registered_admin(turn.context):
        return json.dumps(
            {"ok": False, "error": "仅管理员可以访问服务器管理接口。"},
            ensure_ascii=False,
        )
    host_id = str(arguments.get("host_id") or "").strip()
    unit = str(arguments.get("unit") or "").strip()
    if name in {
        HOST_INSPECT_TOOL_NAME,
        SERVICE_INSPECT_TOOL_NAME,
        SERVICE_LOGS_TOOL_NAME,
        DIAGNOSE_INCIDENT_TOOL_NAME,
        CLUSTER_GUARDIAN_CREATE_TOOL_NAME,
    } and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", host_id):
        return json.dumps(
            {"ok": False, "error": "host_id 无效或没有提供。"},
            ensure_ascii=False,
        )
    if unit and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,119}\.service", unit):
        return json.dumps(
            {"ok": False, "error": "systemd unit 名称无效。"},
            ensure_ascii=False,
        )
    try:
        if name == OPS_CATALOG_TOOL_NAME:
            payload = await client.ops_catalog(
                str(arguments.get("operation") or ""),
                actor=f"qq:{turn.context.event.user_id}",
                origin=turn.context.services.chat._conversation_scope(
                    turn.context.event
                ).key,
            )
        elif name == OPS_CALL_TOOL_NAME:
            payload = await client.ops_call(
                arguments,
                actor=f"qq:{turn.context.event.user_id}",
                origin=turn.context.services.chat._conversation_scope(
                    turn.context.event
                ).key,
            )
        elif name in {SERVICE_CONTROL_TOOL_NAME, HOST_REBOOT_TOOL_NAME}:
            payload = await client.ops_call(
                host_operation_call(name, arguments),
                actor=f"qq:{turn.context.event.user_id}",
                origin=turn.context.services.chat._conversation_scope(
                    turn.context.event
                ).key,
            )
        elif name == FLEET_OVERVIEW_TOOL_NAME:
            payload = await fleet_overview(client)
        elif name == MODEL_STATUS_TOOL_NAME:
            payload = await model_status(
                turn.context.app, str(arguments.get("profile") or "")
            )
        elif name == OPERATION_PREPARE_TOOL_NAME:
            resource_ref = str(arguments.get("resource_ref") or "").strip()
            operation = str(arguments.get("operation") or "").strip()
            if not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,119}\.service", resource_ref
            ):
                return json.dumps(
                    {"ok": False, "error": "resource_ref 必须是完整服务名。"},
                    ensure_ascii=False,
                )
            payload = await client.prepare_operation(
                {
                    "host_id": host_id,
                    "resource_ref": resource_ref,
                    "operation": operation,
                    "arguments": {},
                    "expected_state": arguments.get("expected_state") or {},
                    "verification": arguments.get("verification") or {},
                    "compensation": {},
                    "idempotency_key": str(arguments.get("idempotency_key") or ""),
                },
                actor=f"qq:{turn.context.event.user_id}",
                origin=turn.context.services.chat._conversation_scope(
                    turn.context.event
                ).key,
            )
        elif name == OPERATION_STATUS_TOOL_NAME:
            payload = await client.operation(
                str(arguments.get("operation_id") or ""),
                actor=f"qq:{turn.context.event.user_id}",
                origin=turn.context.services.chat._conversation_scope(
                    turn.context.event
                ).key,
            )
        elif name == OPERATION_CANCEL_TOOL_NAME:
            payload = await client.cancel_operation(
                str(arguments.get("operation_id") or ""),
                actor=f"qq:{turn.context.event.user_id}",
                origin=turn.context.services.chat._conversation_scope(
                    turn.context.event
                ).key,
            )
        elif name == CLUSTER_ARTIFACT_UPLOAD_TOOL_NAME:
            if turn.agent_executor is None:
                return json.dumps(
                    {"ok": False, "error": "当前会话没有沙盒文件权限。"},
                    ensure_ascii=False,
                )
            artifact_path = str(arguments.get("path") or "")
            if artifact_path.startswith("/workspace/"):
                artifact_path = artifact_path.removeprefix("/workspace/")
            content = await turn.agent_executor.sandbox_manager.read_file(
                turn.agent_executor.owner,
                str(arguments.get("sandbox_id") or ""),
                artifact_path,
                max_bytes=25 * 1024 * 1024,
            )
            payload = await client.upload_artifact(
                name=str(arguments.get("name") or "artifact.bin"),
                media_type=str(
                    arguments.get("media_type") or "application/octet-stream"
                ),
                content=content,
                actor=f"qq:{turn.context.event.user_id}",
                origin=turn.context.services.chat._conversation_scope(
                    turn.context.event
                ).key,
            )
        elif name == CLUSTER_JOB_SUBMIT_TOOL_NAME:
            kind = str(arguments.get("kind") or "")
            expected_cost = int(arguments.get("expected_cost_microunits") or 0)
            max_cost = int(
                arguments.get("max_cost_microunits")
                if "max_cost_microunits" in arguments
                else expected_cost
            )
            job_payload: dict[str, object] = {}
            if kind == "probe.http":
                job_payload["target_id"] = str(arguments.get("target_id") or "")
            else:
                job_payload["artifact_id"] = str(arguments.get("artifact_id") or "")
            if kind == "preview.static":
                job_payload["ttl_seconds"] = int(arguments.get("ttl_seconds") or 3600)
            payload = await client.submit_job(
                {
                    "kind": kind,
                    "payload": job_payload,
                    "constraints": {
                        "worker_id": str(arguments.get("worker_id") or "").strip(),
                        "cpu_millis": int(arguments.get("cpu_millis") or 500),
                        "memory_bytes": int(arguments.get("memory_bytes") or 268435456),
                        "gpu_slots": int(arguments.get("gpu_slots") or 0),
                        "priority": str(arguments.get("priority") or "normal"),
                        "borrow_required": arguments.get("borrow_required", False),
                        "checkpoint_format": "gaoji-result-v1",
                        "executor_version": "worker-v2",
                        "expected_cost_microunits": expected_cost,
                        "max_cost_microunits": max_cost,
                    },
                    "idempotency_key": str(arguments.get("idempotency_key") or ""),
                },
                actor=f"qq:{turn.context.event.user_id}",
                origin=turn.context.services.chat._conversation_scope(
                    turn.context.event
                ).key,
            )
        elif name == CLUSTER_JOB_STATUS_TOOL_NAME:
            payload = await client.job(
                str(arguments.get("job_id") or ""),
                actor=f"qq:{turn.context.event.user_id}",
                origin=turn.context.services.chat._conversation_scope(
                    turn.context.event
                ).key,
            )
        elif name == CLUSTER_CASE_SEARCH_TOOL_NAME:
            query = str(arguments.get("query") or turn.user_text)[:2000]
            service_ref = str(arguments.get("service_ref") or "")[:160]
            catalog = await client.runbook_cases(limit=200)
            case_items = [
                item for item in catalog.get("items", []) if isinstance(item, dict)
            ]
            semantic_scores: dict[str, float] = {}
            try:
                semantic_scores = await semantic_runbook_scores(
                    turn.context.app.semantic_recall,
                    turn.context.app.semantic_index_state,
                    case_items,
                    query,
                )
            except (
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                httpx.HTTPError,
            ) as exc:
                turn.context.app.logger.warning(
                    f"Runbook semantic recall failed softly: {exc}"
                )
            current_facts: dict[str, object] = {
                "host_id": host_id,
                "service_ref": service_ref,
            }
            if host_id:
                try:
                    host_snapshot = await client.host(host_id)
                    current_facts["host"] = host_snapshot.get("data", {})
                except FleetControlError:
                    current_facts["host"] = {"status": "unavailable"}
            if host_id and service_ref:
                try:
                    service_snapshot = await client.unit(host_id, service_ref)
                    current_facts["service"] = service_snapshot.get("data", {})
                except FleetControlError:
                    current_facts["service"] = {"status": "unavailable"}
            payload = await client.search_runbook_cases(
                {
                    "query": query,
                    "host_id": host_id,
                    "service_ref": service_ref,
                    "current_facts": current_facts,
                    "candidate_case_ids": list(semantic_scores)[:50],
                    "limit": 50,
                },
                actor=f"qq:{turn.context.event.user_id}",
                origin=turn.context.services.chat._conversation_scope(
                    turn.context.event
                ).key,
            )
            result_items = [
                item for item in payload.get("items", []) if isinstance(item, dict)
            ]
            for item in result_items:
                semantic_score = semantic_scores.get(
                    str(item.get("case_id") or ""), 0.0
                )
                lexical_score = min(
                    max(float(item.get("retrieval_score") or 0), 0.0) / 5.0,
                    1.0,
                )
                item["semantic_score"] = round(semantic_score, 4)
                item["combined_score"] = round(
                    semantic_score * 0.75 + lexical_score * 0.25, 4
                )
            result_items.sort(
                key=lambda item: (
                    bool(item.get("applicable")),
                    float(item.get("combined_score") or 0),
                    int(item.get("updated_at") or 0),
                ),
                reverse=True,
            )
            payload["items"] = result_items[:10]
            payload["retrieval"] = "bge-m3+lexical" if semantic_scores else "lexical"
        elif name == CLUSTER_GUARDIAN_STATUS_TOOL_NAME:
            if not registered_admin(turn.context):
                return json.dumps(
                    {"ok": False, "error": "只有登记管理员能查看目标守护。"},
                    ensure_ascii=False,
                )
            payload = await client.guardian(
                str(arguments.get("guardian_id") or ""),
                actor=f"qq:{turn.context.event.user_id}",
                origin=turn.context.services.chat._conversation_scope(
                    turn.context.event
                ).key,
            )
        elif name == CLUSTER_GUARDIAN_CREATE_TOOL_NAME:
            if not registered_admin(turn.context):
                return json.dumps(
                    {"ok": False, "error": "只有登记管理员能创建目标守护。"},
                    ensure_ascii=False,
                )
            payload = await client.create_guardian(
                {
                    "target_id": str(arguments.get("target_id") or ""),
                    "mode": "observe",
                    "expires_at": int(arguments.get("expires_at") or 0),
                    "interval_seconds": int(arguments.get("interval_seconds") or 60),
                    "failure_threshold": int(arguments.get("failure_threshold") or 3),
                    "max_actions": 0,
                    "probe_policy": {},
                    "authorized_action": {},
                },
                actor=f"qq:{turn.context.event.user_id}",
                origin=turn.context.services.chat._conversation_scope(
                    turn.context.event
                ).key,
            )
        elif name == DIAGNOSE_INCIDENT_TOOL_NAME:
            template = str(arguments.get("template") or "").strip()
            if template not in {
                "model_connectivity",
                "admin_502",
                "qq_no_reply",
                "reply_latency",
                "host_unreachable",
                "storage_pressure",
            }:
                return json.dumps(
                    {"ok": False, "error": "排障模板无效。"},
                    ensure_ascii=False,
                )
            target_id = str(arguments.get("target_id") or "").strip()
            if target_id and not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", target_id):
                return json.dumps(
                    {"ok": False, "error": "固定探测目标无效。"},
                    ensure_ascii=False,
                )
            payload = await client.run_diagnostic(
                template=template,
                host_id=host_id,
                target_id=target_id,
                subject=str(arguments.get("subject") or turn.user_text)[:1000],
                requested_by=(
                    f"qq:{turn.context.event.user_id}@"
                    f"{turn.context.services.chat._conversation_scope(turn.context.event).key}"
                ),
            )
        elif name == HOST_INSPECT_TOOL_NAME:
            payload = await inspect_host(client, host_id)
        elif name == SERVICE_INSPECT_TOOL_NAME:
            if not unit:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "提供完整服务名；只查主机请用 host_inspect。",
                    },
                    ensure_ascii=False,
                )
            payload = await client.unit(host_id, unit)
        else:
            if not unit:
                return json.dumps(
                    {"ok": False, "error": "读取日志必须提供 unit。"},
                    ensure_ascii=False,
                )
            try:
                lines = min(max(int(arguments.get("lines") or 50), 1), 200)
            except (TypeError, ValueError):
                lines = 50
            try:
                since_seconds = min(
                    max(int(arguments.get("since_seconds") or 3600), 1),
                    86400,
                )
            except (TypeError, ValueError):
                since_seconds = 3600
            payload = await client.logs(
                host_id,
                unit,
                lines=lines,
                since_seconds=since_seconds,
            )
    except FleetControlError as exc:
        turn.context.app.logger.warning(
            "Fleet control tool failed ({}): {}", exc.code, exc
        )
        return json.dumps(
            {
                "ok": False,
                "status": "unavailable",
                "error_code": exc.code,
                "error": str(exc),
                "retryable": exc.retryable,
            },
            ensure_ascii=False,
        )
    return json.dumps(payload, ensure_ascii=False)


HANDLERS = tuple(
    FunctionTool(
        definition,
        partial(enabled, name=definition["function"]["name"]),
        partial(execute, name=definition["function"]["name"]),
    )
    for definition in (
        OPS_CATALOG_TOOL,
        OPS_CALL_TOOL,
        SERVICE_CONTROL_TOOL,
        HOST_REBOOT_TOOL,
        FLEET_OVERVIEW_TOOL,
        HOST_INSPECT_TOOL,
        SERVICE_INSPECT_TOOL,
        MODEL_STATUS_TOOL,
        SERVICE_LOGS_TOOL,
        DIAGNOSE_INCIDENT_TOOL,
        OPERATION_PREPARE_TOOL,
        OPERATION_STATUS_TOOL,
        OPERATION_CANCEL_TOOL,
        CLUSTER_ARTIFACT_UPLOAD_TOOL,
        CLUSTER_JOB_SUBMIT_TOOL,
        CLUSTER_JOB_STATUS_TOOL,
        CLUSTER_CASE_SEARCH_TOOL,
        CLUSTER_GUARDIAN_CREATE_TOOL,
        CLUSTER_GUARDIAN_STATUS_TOOL,
    )
)
