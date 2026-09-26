"""Task acceptance matrix: content review plus host-verified operation effects."""
from __future__ import annotations

from datetime import datetime
import math
import re
from typing import Any, Mapping

from .external import remote_record


KINDS = ("evidence", "host_inspection", "disk_delta", "service_effect", "host_reboot")
CHECK_SCHEMA = {"type": "array", "maxItems": 16, "items": {
    "type": "object", "additionalProperties": False,
    "properties": {
        "criterion_index": {"type": "integer", "minimum": 0, "maximum": 15},
        "kind": {"type": "string", "enum": list(KINDS)},
        "host_id": {"type": "string"}, "unit": {"type": "string"},
        "mountpoint": {"type": "string"},
        "action": {"type": "string", "enum": ["start", "stop", "restart", "reload"]},
        "minimum_delta_bytes": {"type": "integer", "minimum": 1},
    }, "required": ["criterion_index", "kind"]}}


def normalize_checks(raw: Any, criteria: tuple[str, ...] | list[str]) -> tuple[dict, ...]:
    if raw is None:
        raw = []
    if not isinstance(raw, list) or len(raw) > len(criteria):
        raise ValueError("outcome_checks must address existing acceptance criteria")
    checks = {}
    allowed = set(CHECK_SCHEMA["items"]["properties"])
    for value in raw:
        if not isinstance(value, dict) or set(value) - allowed:
            raise ValueError("Invalid outcome check fields")
        index, kind = value.get("criterion_index"), value.get("kind")
        if type(index) is not int or not 0 <= index < len(criteria) or index in checks or kind not in KINDS:
            raise ValueError("Outcome check must have a unique criterion index and known kind")
        if kind != "evidence" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", str(value.get("host_id", ""))):
            raise ValueError("Server acceptance requires an exact host_id")
        if kind == "service_effect" and (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,119}\.service", str(value.get("unit", "")))
            or value.get("action") not in {"start", "stop", "restart", "reload"}
        ):
            raise ValueError("Service acceptance requires unit and action")
        if kind == "disk_delta":
            if not str(value.get("mountpoint", "/")).startswith("/"):
                raise ValueError("Disk acceptance requires an absolute mountpoint")
            minimum = value.get("minimum_delta_bytes", 1)
            if type(minimum) is not int or minimum < 1:
                raise ValueError("Disk improvement must be a positive byte count")
        checks[index] = dict(value)
    return tuple(checks.get(index, {"criterion_index": index, "kind": "evidence"})
                 for index in range(len(criteria)))


def validate_check_targets(checks: tuple[dict, ...], criteria: tuple[str, ...] | list[str]) -> None:
    """Catch crossed target/index bindings before a new contract can execute."""
    hosts = {check["host_id"] for check in checks if check["kind"] != "evidence"}
    for check in checks:
        if check["kind"] == "evidence":
            continue
        index, target = check["criterion_index"], check["host_id"]
        mentioned = {host for host in hosts if re.search(
            rf"(?<![A-Za-z0-9_-]){re.escape(host)}(?![A-Za-z0-9_-])", criteria[index], re.IGNORECASE)}
        if mentioned != {target}:
            raise ValueError(
                f"acceptance[{index}] must name only target host {target} for its {check['kind']} check; "
                "split multi-host criteria and keep outcome_checks aligned with their criterion_index")


def timestamp(value: Any) -> float | None:
    if type(value) in (int, float):
        return float(value) if math.isfinite(value) else None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except ValueError:
        return None


def fresh_at_capture(value: Any, item: dict, task_created_at: int) -> bool:
    observed = timestamp(value)
    # Scraped observations can legitimately predate a task by one scrape interval.
    return (observed is not None and item["recorded_at"] >= task_created_at
            and item["recorded_at"] - 180 <= observed <= item["recorded_at"] + 5)


def operation(item: dict) -> dict:
    return remote_record(item["payload"])


def successful_evidence(item: dict) -> bool:
    if not item.get("complete"):
        return False
    payload = item["payload"]
    record = operation(item)
    if payload.get("ok") is False or payload.get("error") or record.get("error_code"):
        return False
    if record.get("operation_id"):
        return record.get("status") == "succeeded"
    if record.get("status") in {"stale", "unavailable", "failed", "pending", "running", "reconciling", "needs_attention", "timed_out", "cancelled", "awaiting_approval"}:
        return False
    return True


def acceptance_blocks_completion(validation: Mapping[str, Any]) -> bool:
    matrix = validation.get("task_outcome")
    return validation.get("status") == "failed" or (isinstance(matrix, dict) and matrix.get("status") != "passed")


def outcome_report(validation: Mapping[str, Any], narrative: str = "", deliveries: list[dict] | None = None) -> str:
    matrix = validation.get("task_outcome")
    if not isinstance(matrix, dict):
        return narrative
    rows = matrix.get("criteria", [])
    passed = sum(row.get("status") == "passed" for row in rows)
    criteria_passed = bool(rows) and passed == len(rows) and matrix.get("status") == "passed"
    files_confirmed = not deliveries or all(item.get("ok") is True for item in deliveries)
    if criteria_passed and validation.get("status") == "passed" and files_confirmed:
        heading = f"已完成并核实 {passed} 项验收。"
    elif criteria_passed:
        heading = f"已核实 {passed} 项验收，但任务尚未全部完成。"
    else:
        heading = f"任务尚未全部完成：已核实 {passed}/{len(rows)} 项。"
    lines = [heading]
    if validation.get("status") == "failed":
        lines.append("独立复核仍有未通过项，不能把验收条目通过当成整项任务完成。")
    labels = {"passed": "已核实", "failed": "未达标", "unverified": "尚未验证"}
    for row in rows:
        lines.append(f"{labels.get(row['status'], '尚未验证')}：{row['description']}。{row['reason']}")
        detail = row.get("detail", {})
        if row.get("kind") == "host_inspection" and detail.get("observation"):
            observation = detail["observation"]
            disk = observation["root_disk"]
            lines.append(f"{detail['host_id']}：可用 {disk['available_bytes'] / 1024**3:.2f} GiB，"
                         f"已授权服务中失败 {observation['failed_service_count']} 个。")
        if detail.get("before") and detail.get("after"):
            lines.append(f"{detail['host_id']} {detail['mountpoint']}：可用容量 "
                         f"{detail['before']['available_bytes'] / 1024**3:.2f} → "
                         f"{detail['after']['available_bytes'] / 1024**3:.2f} GiB。")
    # A failed gate must not be followed by an unverified model claim of success.
    # File delivery is authoritative here; a model-written pre-upload draft is not.
    if criteria_passed and validation.get("status") == "passed" and not deliveries and narrative.strip():
        lines.append(narrative.strip())
    if deliveries:
        confirmed = sum(item.get("ok") is True for item in deliveries)
        lines.append(f"文件交付：{confirmed}/{len(deliveries)} 个已确认送达。")
        states = {"queued": "已进入持久投递队列，等待重试", "sending": "正在核对上传回执",
                  "unknown": "上传结果不明确，只核对回执，不重复上传", "rejected": "上传被拒绝或准备失败，已停止自动重试",
                  "validation_failed": "未通过文件验收，未发送", "disabled": "文件发送工具已关闭"}
        for item in deliveries:
            if item.get("ok") is not True:
                lines.append(f"{item.get('filename') or '附件'}：{states.get(item.get('state'), '尚未确认送达')}。")
    return "\n".join(lines)


def validate_report(result: dict, evidence: list[dict], *, required: bool) -> dict:
    """Normalize the specialist envelope and reject foreign or invented citations."""
    fields = ("findings", "completed", "authorization", "next_verification")
    missing = [field for field in fields if not isinstance(result.get(field), list)]
    missing += result.pop("_report_missing_fields", [])
    for field in fields:
        result.setdefault(field, [])
    refs = {item["evidence_id"] for item in evidence}
    errors = [f"缺少统一交付字段：{field}" for field in sorted(set(missing))] if required else []
    for field in ("findings", "completed"):
        valid = []
        for claim in result.get(field, []):
            if (not isinstance(claim, dict) or not isinstance(claim.get("description"), str)
                    or not claim["description"].strip() or not isinstance(claim.get("evidence_refs"), list)
                    or (required and not claim["evidence_refs"])
                    or any(not isinstance(ref, str) or ref not in refs for ref in claim["evidence_refs"])):
                errors.append(f"{field} 中存在无效结论或不存在的证据引用")
            else:
                valid.append(claim)
        result[field] = valid
    for request in result.get("authorization", []):
        if not isinstance(request, dict) or not all(isinstance(request.get(key), str) and request[key].strip()
                                                    for key in ("host_id", "action", "impact", "reason")):
            errors.append("授权需求缺少目标、动作、影响或原因")
        elif "estimated_bytes" in request and (type(request["estimated_bytes"]) is not int or request["estimated_bytes"] < 0):
            errors.append("预计释放容量不是有效字节数")
        elif "estimated_bytes" in request:
            paths = request.get("affected_paths")
            evidence_refs = request.get("evidence_refs")
            if (not isinstance(paths, list) or not paths or any(not isinstance(path, str) or not path.startswith("/") for path in paths)
                    or not isinstance(evidence_refs, list) or not evidence_refs or any(ref not in refs for ref in evidence_refs)):
                errors.append("空间清理授权缺少具体绝对路径或估算依据")
    if errors:
        if result.get("status") == "success":
            result["status"] = "partial"
        result.setdefault("warnings", []).extend(errors)
    result["report_validation"] = {"status": "passed" if not errors else "incomplete", "errors": errors}
    return result


def _host_observation(item: dict, host: str, created: int) -> dict | None:
    if item["tool_name"] not in {"host_inspect", "fleet_overview"} or not successful_evidence(item):
        return None
    for observation in item["payload"].get("hosts", []):
        if (isinstance(observation, dict) and observation.get("host_id") == host
                and observation.get("status") == "online"
                and fresh_at_capture(observation.get("exporter_sample_at"), item, created)):
            return observation
    return None


def _inspection_resources(item: dict, observation: dict, items: list[dict], host: str, created: int) -> tuple[dict, str]:
    # The catalog tool and the compact inspection tool share the same upstream metrics.
    from ..fleet_tools import summarize_resources

    native = [value for value in items if value["tool_name"] == "ops_call"
              and value["arguments"].get("operation") == "host.metrics"
              and value["arguments"].get("params", {}).get("host") == host
              and value["recorded_at"] >= item["recorded_at"]]
    if not native:
        return observation.get("resources") or {}, item["evidence_id"]
    latest = max(native, key=lambda value: value["recorded_at"])
    payload = latest["payload"]
    data = payload.get("result") or {}
    if (not successful_evidence(latest) or payload.get("operation") != "host.metrics"
            or not isinstance(data, dict) or data.get("host") != host
            or not fresh_at_capture(data.get("observed_at"), latest, created)):
        return {}, latest["evidence_id"]
    gap = latest["recorded_at"] - item["recorded_at"]
    if gap > 180:
        return {"status": "partial", "observation_gap_seconds": gap}, latest["evidence_id"]
    return summarize_resources({"status": "fresh", "data": data}, host, now=latest["recorded_at"]), latest["evidence_id"]


def _check_server(check: dict, items: list[dict], created: int) -> tuple[str, str, dict]:
    host, kind = check["host_id"], check["kind"]
    if kind == "host_inspection":
        relevant = [item for item in items if item["tool_name"] in {"host_inspect", "fleet_overview"}
                    and (item["arguments"].get("host_id") == host or any(
                        isinstance(value, dict) and value.get("host_id") == host for value in item["payload"].get("hosts", [])))]
        if relevant:
            item = max(relevant, key=lambda item: item["recorded_at"])
            value = _host_observation(item, host, created)
            resource, resource_ref = _inspection_resources(item, value or {}, items, host, created)
            resource_item = next((row for row in items if row["evidence_id"] == resource_ref), item)
            if (value and value.get("root_disk") and value.get("failed_service_count") is not None
                    and resource.get("status") == "available"
                    and fresh_at_capture(resource.get("cpu_observed_at"), resource_item, created)
                    and fresh_at_capture(resource.get("memory_observed_at"), resource_item, created)):
                return "passed", "已取得新鲜磁盘、CPU、内存和可见服务状态；发现异常不等于已修复", {
                    "host_id": host, "observation": {**value, "resources": resource},
                    "evidence_ref": item["evidence_id"], "resource_evidence_ref": resource_ref}
            if value and resource.get("observation_gap_seconds"):
                gap = resource["observation_gap_seconds"]
                return "unverified", (f"资源采样与磁盘/服务观测相隔 {gap} 秒，不能作为同一时段的完整状态；"
                    "需补查该主机概况与资源指标，不必重跑目录扫描"), {
                        "host_id": host, "observation_gap_seconds": gap,
                        "evidence_ref": item["evidence_id"], "resource_evidence_ref": resource_ref}
        return "unverified", "最新观测缺少目标主机的新鲜磁盘、资源或服务数据", {}
    if kind in {"service_effect", "host_reboot"}:
        level = "service_state" if kind == "service_effect" else "host_reboot"
        for item in reversed(items):
            if item["tool_name"] not in {"service_control", "host_reboot", "ops_call", "operation_status"}:
                continue
            record = operation(item)
            proof = record.get("result", {}).get("verification", {})
            if not successful_evidence(item) or record.get("host_id") != host or not record.get("operation_id"):
                continue
            if any(operation(later).get("operation_id") == record["operation_id"]
                   and later["recorded_at"] >= item["recorded_at"]
                   and operation(later).get("status") in {"failed", "cancelled", "needs_attention", "timed_out"}
                   for later in items):
                continue
            if (timestamp(record.get("created_at")) or 0) < created:
                continue
            observed_at = proof.get("current", {}).get("observed_at") if kind == "service_effect" else proof.get("observed_at")
            if not fresh_at_capture(observed_at, item, created):
                continue
            if kind == "service_effect":
                matches = proof.get("level") == level and proof.get("host") == host and proof.get("unit") == check["unit"] and proof.get("action") == check["action"]
            else:
                matches = proof.get("host", host) == host and bool(proof.get("before_boot_id")) and bool(proof.get("after_boot_id")) and proof["before_boot_id"] != proof["after_boot_id"]
            if matches and proof.get("verified") is True:
                return "passed", record.get("result", {}).get("summary") or "宿主已核对目标变化", {
                    "operation_id": record["operation_id"], "verification": proof, "evidence_ref": item["evidence_id"]}
        return "unverified", "命令完成不等于目标完成，缺少宿主确认的操作效果", {}
    if kind == "disk_delta":
        mount = check.get("mountpoint", "/")
        samples = []
        for item in items:
            host_state = _host_observation(item, host, created)
            disk = (host_state or {}).get("root_disk") or {}
            if disk.get("mountpoint") == mount and type(disk.get("available_bytes")) is int and type(disk.get("total_bytes")) is int:
                samples.append((timestamp(host_state["exporter_sample_at"]), disk, item["evidence_id"]))
        samples.sort(key=lambda row: row[0])
        if len(samples) < 2:
            return "unverified", "缺少同一主机、同一文件系统处理前后的两次空间观测", {}
        before, after = samples[0], samples[-1]
        if (after[0] <= before[0] or after[1]["total_bytes"] != before[1]["total_bytes"]
                or not before[1].get("device") or after[1].get("device") != before[1]["device"]):
            return "unverified", "空间观测时间未前进或文件系统容量发生变化，不能直接相减", {}
        operation_items = [item for item in items if item["tool_name"] in {"ops_call", "operation_status"}
                      and successful_evidence(item) and operation(item).get("host_id") == host
                      and max(created, before[0]) <= (timestamp(operation(item).get("created_at")) or 0)
                      and (timestamp(operation(item).get("updated_at")) or math.inf) <= after[0]
                      and not any(operation(later).get("operation_id") == operation(item).get("operation_id")
                          and later["recorded_at"] >= item["recorded_at"]
                          and operation(later).get("status") in {"failed", "cancelled", "needs_attention", "timed_out"}
                          for later in items)]
        if not operation_items:
            return "unverified", "缺少两次空间采样之间本任务已完成的目标操作回执", {}
        delta = after[1]["available_bytes"] - before[1]["available_bytes"]
        comparison = {"host_id": host, "mountpoint": mount, "before": before[1], "after": after[1],
                      "before_at": before[0], "after_at": after[0], "delta_bytes": delta,
                      "evidence_refs": list(dict.fromkeys([before[2], after[2],
                          *(item["evidence_id"] for item in operation_items)])),
                      "operation_ids": list(dict.fromkeys(operation(item)["operation_id"]
                          for item in operation_items if operation(item).get("operation_id"))),
                      "attribution": "observed_change_not_exclusive_cleanup_attribution"}
        passed = delta >= check.get("minimum_delta_bytes", 1)
        return "passed" if passed else "failed", f"可用空间观测变化 {delta} 字节；不能把同期所有变化归因于清理", comparison
    return "unverified", "未实现此类验收", {}


def evaluate_acceptance(contract: Mapping[str, Any], evidence: list[dict], reviewer: Mapping[str, Any],
                        *, task_created_at: int) -> dict:
    criteria = contract.get("acceptance", [])
    checks = normalize_checks(contract.get("outcome_checks"), criteria)
    if contract.get("version", 1) >= 3:
        validate_check_targets(checks, criteria)
    reviews = reviewer.get("metadata", {}).get("criterion_reviews", [])
    reviews = reviews if isinstance(reviews, list) else []
    by_ref = {item["evidence_id"]: item for item in evidence}
    rows = []
    for check in checks:
        index = check["criterion_index"]
        matches = [value for value in reviews if isinstance(value, dict) and type(value.get("criterion_index")) is int and value["criterion_index"] == index]
        review = matches[0] if len(matches) == 1 else {}
        refs = review.get("evidence_refs", [])
        valid = isinstance(refs, list) and bool(refs) and all(isinstance(ref, str) and ref in by_ref for ref in refs)
        selected = [by_ref[ref] for ref in refs] if valid else []
        status, reason, detail = "unverified", "缺少对应条款的独立审查或有效证据", {}
        if review.get("status") == "failed":
            status, reason = "failed", str(review.get("reason") or "独立审查未通过")
        elif valid and review.get("status") == "passed" and str(review.get("reason") or "").strip():
            if check["kind"] == "evidence":
                if all(successful_evidence(item) for item in selected):
                    status, reason = "passed", str(review["reason"])
                    detail = {"level": "evidence_backed_review", "not_a_machine_proof": True}
            else:
                candidates = selected
                if check["kind"] == "disk_delta" and any(
                    ((value := _host_observation(item, check["host_id"], task_created_at))
                     and (value.get("root_disk") or {}).get("mountpoint") == check.get("mountpoint", "/"))
                    for item in selected
                ):
                    # A cited target sample anchors the composite check. Its counterpart
                    # and operation receipt come from this task's trusted evidence, not prose.
                    candidates = evidence
                status, reason, detail = _check_server(check, candidates, task_created_at)
                if status == "passed":
                    # A valid citation still cannot hide a newer contradictory observation.
                    status, reason, detail = _check_server(check, evidence, task_created_at)
        rows.append({**check, "description": criteria[index], "status": status, "reason": reason,
                     "evidence_refs": list(dict.fromkeys([
                         *(refs if valid else []), *detail.get("evidence_refs", []),
                         *([detail["evidence_ref"]] if detail.get("evidence_ref") else []),
                     ])), "detail": detail})
    # Typed mutations create mandatory effect checks even if the planner omitted one.
    effects = {}
    for item in evidence:
        if item["tool_name"] not in {"service_control", "host_reboot", "ops_call"}:
            continue
        record = operation(item)
        if not record.get("operation_id") or (timestamp(record.get("created_at")) or 0) < task_created_at:
            continue
        args = item["arguments"]
        action = args.get("action", "") if item["tool_name"] == "service_control" else str(args.get("operation") or "").removeprefix("units.")
        host = record.get("host_id")
        if not host:
            continue
        if item["tool_name"] == "host_reboot" or args.get("operation") == "host.reboot":
            effect = {"kind": "host_reboot", "host_id": host}
        elif action in {"start", "stop", "restart", "reload"}:
            effect = {"kind": "service_effect", "host_id": host,
                      "unit": args.get("unit") or args.get("params", {}).get("unit"), "action": action}
        else:
            continue
        effects[record["operation_id"]] = effect
    for op_id, effect in effects.items():
        items = [item for item in evidence if operation(item).get("operation_id") == op_id]
        status, reason, detail = _check_server(effect, items, task_created_at)
        rows.append({**effect, "criterion_index": f"operation:{op_id}", "description": f"操作 {op_id} 的实际效果",
                     "status": status, "reason": reason, "detail": detail,
                     "evidence_refs": [item["evidence_id"] for item in items]})
    # A full host inspection needs machine-checked coverage even if classified as generic evidence.
    checked_hosts = {check["host_id"] for check in checks if check["kind"] == "host_inspection"}
    observed_hosts = {item["arguments"].get("host_id") for item in evidence if item["tool_name"] == "host_inspect"}
    for host in sorted(value for value in observed_hosts - checked_hosts if isinstance(value, str) and value):
        check = {"kind": "host_inspection", "host_id": host}
        status, reason, detail = _check_server(check, evidence, task_created_at)
        rows.append({**check, "criterion_index": f"inspection:{host}", "description": f"主机 {host} 的巡检覆盖",
                     "status": status, "reason": reason, "detail": detail,
                     "evidence_refs": list(dict.fromkeys(value for key, value in detail.items() if key.endswith("evidence_ref")))})
    status = "passed" if rows and all(row["status"] == "passed" for row in rows) else "failed" if any(row["status"] == "failed" for row in rows) else "unverified"
    return {"version": 2, "status": status, "criteria": rows}
