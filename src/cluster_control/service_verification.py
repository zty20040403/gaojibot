"""Verify native unit-action receipts and fresh, stable target observations."""
from __future__ import annotations

from datetime import datetime
import re
import time
from typing import Any

from .adapters.ops import OpsError


SERVICE_ACTIONS = frozenset({"units.start", "units.stop", "units.restart", "units.reload"})
STABILITY_SECONDS = 5
VERIFICATION_SECONDS = 120
ACTION_LABELS = {"start": "启动", "stop": "停止", "restart": "重启", "reload": "重载"}


def timestamp(value: Any) -> float:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Service evidence must have an explicit timezone")
    return parsed.timestamp()


def running(state: dict[str, Any]) -> bool:
    return state.get("active_state") == "active" and state.get("sub_state") in {"running", "exited"}


def receipt(record: dict[str, Any], job: dict[str, Any], now: float) -> dict[str, Any]:
    params = record["arguments"]["params"]
    operation = record["arguments"]["op"]
    handle, spec, report = job.get("handle", {}), job.get("spec", {}), job.get("result", {})
    if not all(isinstance(item, dict) for item in (handle, spec, report)):
        raise ValueError("Service receipt is incomplete")
    if record.get("backend_ref") == "ssh-management-v1":
        argv = report.get("argv")
        attributed = (report.get("attribution") == "systemctl_exit_and_target_observed"
            and type(report.get("exit_code")) is int and report["exit_code"] == 0
            and isinstance(argv, list) and len(argv) == 3 and isinstance(argv[0], str)
            and argv[0].startswith("/") and argv[0].endswith("/systemctl")
            and argv[1:] == [operation.removeprefix("units."), params.get("unit")])
    else:
        attributed = (report.get("attribution") == "systemd_job_accepted_and_target_observed"
            and re.fullmatch(r"/org/freedesktop/systemd1/job/\d+", str(report.get("manager_job", ""))) is not None)
    if (handle.get("job_id") != record["backend_operation_id"]
            or handle.get("host") != params["host"] or handle.get("operation") != operation
            or handle.get("state") != "succeeded"
            or spec.get("host") != params["host"] or spec.get("unit") != params.get("unit")
            or spec.get("expected_invocation_id") != params.get("expected_invocation_id")
            or report.get("unit") != params.get("unit")
            or report.get("action") != operation.removeprefix("units.")
            or report.get("success") is not True
            or not attributed):
        raise ValueError("Service receipt does not prove the requested target action")
    finished = timestamp(job.get("updated_at"))
    if not record["created_at"] <= finished <= now + 5:
        raise ValueError("Service receipt time is outside this operation")
    before, after = report.get("before"), report.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise ValueError("Service receipt lacks before/after state")
    for state in (before, after):
        if (not isinstance(state.get("active_state"), str) or not isinstance(state.get("sub_state"), str)
                or not state["active_state"] or not state["sub_state"]):
            raise ValueError("Service receipt lacks an explicit before/after state")
        invocation = state.get("invocation_id")
        if ((invocation and (not isinstance(invocation, str) or not re.fullmatch(r"[a-f0-9]{32}", invocation)))
                or (state.get("active_state") == "active" and not invocation)):
            raise ValueError("Service receipt lacks a valid running instance identity")
    if params.get("expected_invocation_id") is not None and before.get("invocation_id") != params["expected_invocation_id"]:
        raise ValueError("Service baseline does not match the expected running instance")
    return {"level": "service_state", "action": report["action"], "host": params["host"],
            "unit": params["unit"], "before": before, "after": after,
            "job_finished_at": job["updated_at"], "verified": False,
            "application_health": "not_checked"}


def observe_unit(payload: Any, proof: dict[str, Any], record: dict[str, Any], now: float) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("unit"), dict):
        raise ValueError("Service observation is incomplete")
    unit = payload["unit"]
    observed = timestamp(payload.get("observed_at"))
    if (payload.get("host") != proof["host"] or unit.get("unit") != proof["unit"]
            or now - observed > 60 or observed > now + 5
            or observed < max(record["created_at"], timestamp(proof["job_finished_at"]))):
        raise ValueError("Service observation is stale or belongs to another target")
    details = unit.get("details")
    if not isinstance(details, dict):
        raise ValueError("Service observation lacks process details")
    if not isinstance(unit.get("active_state"), str) or not isinstance(unit.get("sub_state"), str):
        raise ValueError("Service observation has invalid state fields")
    return {"observed_at": payload["observed_at"], "load_state": unit.get("load_state"),
            "active_state": unit.get("active_state"), "sub_state": unit.get("sub_state"),
            "invocation_id": details.get("invocation_id"), "main_pid": details.get("main_pid"),
            "restarts": details.get("restarts"), "exec_main_status": details.get("exec_main_status")}


def target_matches(proof: dict[str, Any], current: dict[str, Any]) -> bool:
    if current.get("load_state") != "loaded":
        return False
    if proof["action"] == "stop":
        return (current.get("active_state") == "inactive" and current.get("sub_state") == "dead"
                and current.get("main_pid") in (None, 0))
    if current.get("sub_state") == "running" and (type(current.get("main_pid")) is not int or current["main_pid"] <= 0):
        return False
    return (running(current) and bool(proof["after"].get("invocation_id"))
            and current.get("invocation_id") == proof["after"]["invocation_id"])


def describe_service(result: dict[str, Any], status: str) -> None:
    proof = result.get("verification", {})
    target = f"{proof.get('host', '')} 的 {proof.get('unit', '服务')}"
    action = ACTION_LABELS.get(proof.get("action"), "操作")
    if status == "succeeded":
        current = proof["current"]
        if proof["action"] == "stop":
            summary = f"{target}已停止，两次复查均为 inactive/dead。"
        elif proof["action"] == "start" and proof["before"].get("invocation_id") == proof["after"].get("invocation_id"):
            summary = f"{target}原本就在运行，本次未重启；已复查仍正常运行。"
        else:
            summary = f"{target}已{action}，两次复查均为 {current['active_state']}/{current['sub_state']}。"
        if proof["action"] == "restart":
            summary += f"实例编号 {proof['before'].get('invocation_id') or '未运行'} → {proof['after']['invocation_id']}。"
        if current.get("main_pid"):
            summary += f"当前 PID {current['main_pid']}。"
        result["summary"] = summary
        result["instruction"] = "按 summary 和 verification 中的事实汇报。已验证 systemd 服务状态，未验证业务接口；不得说所有业务都已恢复。"
    else:
        prefix = "复查失败" if status == "failed" else "尚未确认成功"
        result["summary"] = f"{target}{action}{prefix}：{proof.get('reason', '等待目标状态证据')}。"
        result["instruction"] = "不得声称操作已成功；说明已确认的事实和缺少的证据。继续跟踪原 operation_id，禁止重新提交重启。"


async def verify_service(manager: Any, record: dict[str, Any], job: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    now = time.time()
    old = record.get("result") or {}
    previous = old.get("verification") or {}
    started = old.get("verification_started_at", now)
    result = {**job, "phase": "verifying_service", "verification_started_at": started}
    params = record["arguments"]["params"]
    try:
        proof = receipt(record, job, now)
    except (ValueError, KeyError, TypeError) as exc:
        result.update(phase="outcome_unknown", verification={"verified": False, "host": record["host_id"],
            "unit": params.get("unit"), "action": record["arguments"]["op"].removeprefix("units."), "reason": str(exc)})
        describe_service(result, "needs_attention")
        return "needs_attention", result, "service_receipt_unverified"
    result["verification"] = proof
    deadline = min(started + VERIFICATION_SECONDS, record["deadline_at"])
    if now >= deadline:
        proof.update({key: previous[key] for key in ("current", "stable_since") if key in previous})
        proof["reason"] = "复查期限已到，保留已获得的证据，但不再宣称验收成功"
        result["phase"] = "outcome_unknown"
        describe_service(result, "needs_attention")
        return "needs_attention", result, "service_verification_timeout"
    before, after, action = proof["before"], proof["after"], proof["action"]
    if action == "restart":
        proof["restart_confirmed"] = bool(after.get("invocation_id")) and after.get("invocation_id") != before.get("invocation_id")
        if not proof["restart_confirmed"]:
            proof["reason"] = "上游返回成功，但重启前后实例编号没有变化或缺失"
            result["phase"] = "outcome_unknown"
            describe_service(result, "needs_attention")
            return "needs_attention", result, "service_restart_unverified"
    valid_effect = (after.get("active_state") == "inactive" if action == "stop" else running(after))
    if action == "reload":
        valid_effect = valid_effect and after.get("reload_result") == "success"
    if not valid_effect:
        proof["reason"] = "命令结束后的服务状态不符合目标"
        result["phase"] = "service_failed"
        describe_service(result, "failed")
        return "failed", result, "service_target_failed"
    status, error = "reconciling", ""
    try:
        response = await manager.client.call("units.status", {"host": proof["host"], "unit": proof["unit"]})
        now = time.time()
        current = observe_unit(response.data, proof, record, now)
        proof["current"] = current
        if target_matches(proof, current):
            first = previous.get("stable_since")
            same_instance = (previous.get("current", {}).get("invocation_id") == current["invocation_id"]
                             and previous.get("current", {}).get("restarts") == current["restarts"])
            proof["stable_since"] = first if first and same_instance else current["observed_at"]
            elapsed = timestamp(current["observed_at"]) - timestamp(proof["stable_since"])
            proof["stable_seconds"] = max(0, elapsed)
            if first and same_instance and elapsed >= STABILITY_SECONDS:
                proof.update(verified=True, reason="目标状态与两次独立复查一致")
                result["phase"] = "verified"
                status = "succeeded"
                if record["status"] == "cancelling":
                    result["cancellation_note"] = "取消到达时操作已生效，不能撤销；目标状态已验证。"
            else:
                proof["reason"] = "第一次复查符合目标，等待第二次新鲜状态确认"
        elif current.get("active_state") == "failed":
            status, error = "failed", "service_unhealthy_after_action"
            result["phase"] = "service_failed"
            proof["reason"] = "操作后服务进入 failed 状态"
        else:
            proof["reason"] = "当前服务状态或实例编号与操作回执不一致"
    except (OpsError, ValueError, KeyError, TypeError) as exc:
        proof["reason"] = f"无法确认目标当前状态：{str(exc)[:400]}"
        # A failed read must not erase the first successful observation on takeover.
        for key in ("stable_since", "current"):
            if key in previous:
                proof[key] = previous[key]
    if status in {"reconciling", "succeeded"} and time.time() >= deadline:
        status, error = "needs_attention", "service_verification_timeout"
        result["phase"] = "outcome_unknown"
        proof["verified"] = False
        proof["reason"] += "，复查期限已到，不能宣称成功"
    describe_service(result, status)
    return status, result, error
