"""Read model for the complete task lifecycle, separate from execution state."""
from __future__ import annotations

from typing import Any

from .evidence import evidence_index, redact
from .external import remote_record
from .receipt_links import RECEIPT_TOOL


def task_progress(task: Any, runs: list, evidence: list[dict], external: list[dict],
                  deliveries: list[dict], final: Any, *, revision: int,
                  project_completion: dict | None = None) -> dict:
    contract = task.plan.get("contract", {})
    validation = task.result.get("validation", {}).get("acceptance", {})
    matrix = validation.get("task_outcome", {}) if isinstance(validation, dict) else {}
    terminal = task.status in {"completed", "partial", "failed", "cancelled"}
    findings, finished, next_verification, authorization = [], [], [], []
    for run in runs:
        if (run.step_key.startswith("acceptance_r")
                or run.result.get("metadata", {}).get("superseded_by_revision")):
            continue
        for key, target in (("findings", findings), ("completed", finished),
                            ("authorization", authorization), ("next_verification", next_verification)):
            for value in run.result.get(key, []):
                target.append({"run_id": run.run_id, "step": run.step_key, "value": value})
    operations = []
    waiting_approval = False
    approval_checks = []
    for item in external:
        response = item.get("response") or {}
        record = remote_record(response)
        waiting_approval |= record.get("status") == "awaiting_approval"
        if record.get("operation_id") or record.get("deployment_id") or response.get("approval_required"):
            approval_checks.append(bool(record.get("approval_ref")))
        result = record.get("result") or {}
        request = item.get("request") or {}
        arguments = record.get("arguments") or request.get("tool_arguments") or {}
        observed_status = record.get("status", item["status"])
        ended_approval = terminal and observed_status == "awaiting_approval"
        operations.append({"run_id": item["run_id"], "call_id": item["call_id"],
            "remote_path": item.get("remote_path"), "status": "unverified" if ended_approval else observed_status,
            "last_observed_status": observed_status,
            "tool_name": request.get("tool_name") or arguments.get("operation") or "",
            "host_id": record.get("host_id") or result.get("host") or arguments.get("params", {}).get("host"),
            "updated_at": item["updated_at"], "arguments": redact(arguments),
            "summary": ("本轮任务已结束；这是最后一次授权观测，不代表现在仍可批准或已经执行。"
                        if ended_approval else result.get("summary") or
                        ("工具已返回；结果以原始回执为准，不代表业务目标已完成。" if observed_status == "resolved" else "")),
            "verification": result.get("verification"),
            "error": record.get("error_code") or record.get("error") or ""})
    historical = {}
    for item in evidence:
        if item["tool_name"] == RECEIPT_TOOL:
            payload = item["payload"]
            key = payload["source_request"]["request_hash"]
            # A verified immutable dispatch proof supersedes an earlier incomplete lookup.
            if key not in historical or payload.get("ok") is True:
                historical[key] = item
    for key, item in historical.items():
        payload = item["payload"]
        receipt = payload["receipt"]
        verified = payload.get("ok") is True
        source_revision = payload["source_request"]["revision"]
        # Historical proof remains visible without blocking a newer revision's approval.
        if source_revision == revision:
            approval_checks.append(verified)
        operations.append({"run_id": item["run_id"], "call_id": "receipt:" + key,
            "remote_path": "", "status": "passed" if verified else "unverified",
            "host_id": receipt["host_id"], "updated_at": receipt.get("dispatched_at") or receipt["created_at"],
            "historical": True, "source_revision": source_revision,
            "arguments": item["arguments"], "verification": receipt,
            "summary": "历史授权与派发已核实；不是当前健康或修复结果。" if verified else "历史授权证明不足。",
            "error": ""})
    approval_relevant = bool(approval_checks)
    approval_confirmed = approval_relevant and all(approval_checks)
    active = task.status in {"running", "planning", "verifying", "waiting_external"}
    file_rows = [item for item in deliveries if item["revision"] == revision]
    files_confirmed = not contract.get("delivery_required") or bool(file_rows) and all(item["state"] == "acknowledged" for item in file_rows)
    if getattr(task, "project_id", None) is not None and project_completion:
        files = project_completion["files"]
        files_confirmed = (bool(files["items"]) or not files["required"]) and all(
            item["state"] == "acknowledged" and not item["draft"] for item in files["items"])
    final_state = final.status if final else "pending"
    delivered = files_confirmed and final_state == "committed"
    inspected = [row for row in matrix.get("criteria", []) if row.get("kind") == "host_inspection"]
    inspection_complete = bool(inspected) and all(row["status"] == "passed" for row in inspected)
    stages = [
        {"key": "inspection", "label": "检查", "status": "completed" if inspection_complete else "running" if active else "unverified" if terminal else "pending",
         "detail": f"{len(evidence)} 条宿主工具证据"},
        {"key": "findings", "label": "发现问题", "status": "completed" if terminal or findings else "running" if active else "pending",
         "detail": f"{len(findings)} 条发现，{len(next_verification)} 项待补查"},
        {"key": "authorization", "label": "授权", "status": "unverified" if terminal and waiting_approval else "waiting" if waiting_approval else "completed" if approval_confirmed else "unverified" if approval_relevant else "not_required",
         "detail": "本轮已结束，未取得有效授权；旧请求不能继续本轮任务" if terminal and waiting_approval else "等待本轮任务授权" if waiting_approval else "本轮已记录批准凭据；历史申请另列" if approval_confirmed else "本轮操作记录未提供批准凭据" if approval_relevant else "本轮未发生需授权的服务器操作"},
        {"key": "execution", "label": "执行", "status": task.result.get("execution_state") or ("completed" if task.status == "completed" else task.status),
         "detail": f"{len(finished)} 项已记录的完成工作"},
        {"key": "verification", "label": "复查", "status": matrix.get("status") or ("running" if task.status == "verifying" else "unverified" if terminal else "pending"),
         "detail": f"{sum(row.get('status') == 'passed' for row in matrix.get('criteria', []))}/{len(matrix.get('criteria', []))} 项通过"},
        {"key": "delivery", "label": "结果送达", "status": "committed" if delivered else "partial" if final_state == "committed" else final_state,
         "detail": "文字和要求的文件已确认送达" if delivered else "分别核对最终文字与文件回执"},
    ]
    if getattr(task, "project_id", None) is not None:
        state = (project_completion or {}).get("project", {})
        code = state.get("code", "pending")
        stages.insert(-1, {"key": "project", "label": "项目落地",
            "status": "completed" if code == "materialized" else "failed" if code == "restore_failed"
                      else "unverified" if code in {"access", "artifact", "invalid", "conflict", "released"} else "waiting",
            "detail": f"prj#{task.project_id} 本任务版本 v{state.get('version')} 已恢复并核实"
                      if code == "materialized" else "版本已保存，工作区恢复失败" if code == "restore_failed"
                      else "版本已保存，尚未恢复到工作区" if code == "restoring"
                      else "任务未更新项目，原版本保留" if code == "released"
                      else "尚无项目更新完成回执，不能把子任务结束当作项目落地"})
    return {"revision": revision, "stages": stages, "acceptance": matrix,
        "findings": findings, "completed": finished, "authorization": authorization,
        "next_verification": next_verification, "operations": operations,
        "evidence": evidence_index(evidence), "execution_status": task.status,
        "delivery_status": stages[-1]["status"], "files_confirmed": files_confirmed,
        "updated_at": max([task.updated_at, *(item["updated_at"] for item in external),
                           *(item["recorded_at"] for item in evidence),
                           *(item["updated_at"] for item in file_rows), final.updated_at if final else 0])}
