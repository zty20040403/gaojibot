"""Compact, evidence-labelled projections for conversational status queries."""

from __future__ import annotations

import asyncio
import math
import re
import time
from typing import Any

from .fleet_client import FleetControlError


def requires_local_model_status(text: str) -> bool:
    return bool(
        re.search(r"千问|qwen", text, re.IGNORECASE)
        and re.search(
            r"状态|寄了|寄了吗|挂了|挂了吗|在线|能用|可用|连通|连不上|"
            r"启动|开了|开没|正常|现在|目前|还活|还在|关了",
            text,
        )
    )


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: Any) -> list[dict[str, Any]]:
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def _error(payload: dict[str, Any]) -> dict[str, Any] | None:
    error = _object(payload.get("error"))
    return {
        key: error[key] for key in ("code", "message", "retryable") if key in error
    } or None


def _fresh(payload: dict[str, Any], now: int) -> bool:
    expires = payload.get("expires_at")
    return payload.get("status") == "fresh" and (expires is None or expires > now)


def _sample_fresh(sample_at: Any, now: int | float) -> bool:
    # Stored capture times use whole seconds; Prometheus samples retain fractions.
    return (type(sample_at) in (int, float) and math.isfinite(sample_at)
            and -1 < now - sample_at <= 90)


def _root_disk(host: dict[str, Any]) -> dict[str, Any] | None:
    pressure = _object(host.get("pressure"))
    values: dict[str, Any] = {}
    devices: set[str] = set()
    device_count = 0
    for metric, label in (
        ("filesystem_size_bytes", "total_bytes"),
        ("filesystem_available_bytes", "available_bytes"),
    ):
        for sample in _items(_object(pressure.get(metric)).get("samples")):
            if _object(sample.get("labels")).get("mountpoint") != "/":
                continue
            value = sample.get("value")
            if (
                sample.get("state") == "available"
                and isinstance(value, (int, float))
                and value >= 0
            ):
                values[label] = int(value)
                device = _object(sample.get("labels")).get("device")
                if device:
                    devices.add(str(device))
                    device_count += 1
                break
    if "available_bytes" not in values:
        return None
    return {
        "mountpoint": "/",
        "device": next(iter(devices)) if len(devices) == 1 and device_count == 2 else None,
        **values,
        "available_gib": round(values["available_bytes"] / 1024**3, 1),
    }


def summarize_fleet(
    payload: dict[str, Any], *, host_id: str = "", now: int | None = None,
    acquired_at: int | None = None,
) -> dict[str, Any]:
    timestamp = int(time.time()) if now is None else now
    captured_at = timestamp if acquired_at is None else acquired_at
    snapshot_current = 0 <= timestamp - captured_at <= 90
    observations = {
        str(item.get("host")): item
        for item in _items(_object(payload.get("data")).get("hosts"))
    }
    inventory = _items(payload.get("inventory"))
    hosts = list(
        dict.fromkeys(
            [str(item["host_id"]) for item in inventory if item.get("host_id")]
            + list(observations)
        )
    )
    if host_id:
        hosts = [host_id] if host_id in hosts else []
    failures = _object(payload.get("failed_units"))
    failed_by_host = {
        str(item.get("host")): item
        for item in _items(_object(failures.get("data")).get("hosts"))
    }
    alerts_result = _object(payload.get("active_alerts"))
    alerts = _items(_object(alerts_result.get("data")).get("alerts"))
    summaries: list[dict[str, Any]] = []
    for name in hosts:
        host = observations.get(name, {})
        agent = _object(host.get("agent"))
        exporter = _object(host.get("exporter"))
        sample_at = exporter.get("sample_at_unix_seconds")
        exporter_fresh = _sample_fresh(sample_at, timestamp)
        direct_sample = _object(host.get("resource_observation"))
        ssh_fresh = (payload.get("source_backend") == "ssh" and direct_sample.get("source") == "ssh"
            and _sample_fresh(direct_sample.get("sample_at_unix_seconds"), timestamp))
        # Cache validity belongs to receipt time; sample age is checked at assembly.
        current = snapshot_current and _fresh(payload, captured_at) and bool(host)
        agent_up = agent.get("state") == "reachable"
        exporter_up = exporter.get("state") == "up" and exporter_fresh
        online = current and (agent_up or exporter_up)
        failed = failed_by_host.get(name, {})
        units = _items(failed.get("units"))
        failure_count = (
            len(units)
            if snapshot_current and _fresh(failures, captured_at)
            and failed.get("state") == "available"
            and isinstance(failed.get("units"), list)
            else None
        )
        if (
            failure_count is None
            and current
            and agent_up
            and isinstance(agent.get("failed_units"), int)
        ):
            failure_count = agent["failed_units"]
        host_alerts = [
            item
            for item in alerts
            if _object(item.get("labels")).get("instance") == name
        ]
        alert_count = len(host_alerts) if snapshot_current and _fresh(alerts_result, captured_at) else None
        shown_alerts = host_alerts if host_id else host_alerts[:3]
        shown_units = units if host_id else units[:5]
        status = "online" if online else "stale" if host and not current else "unknown"
        summary = f"{name} 在线"
        if online:
            summary += "，监控采样正常" if exporter_up else "，SSH 实时检查正常" if ssh_fresh else "，监控采样未确认"
            if failure_count is not None:
                summary += f"，已授权服务中有 {failure_count} 个失败"
            if alert_count:
                summary += f"，另有 {alert_count} 条活动告警"
        elif not current:
            summary = f"{name} 暂无新鲜状态，不能据此判断关机"
        else:
            summary = f"{name} 当前观察信号不可达或不完整，原因尚未确认"
        summaries.append(
            {
                "host_id": name,
                "status": status,
                "summary": summary,
                "agent_state": agent.get("state", "unknown"),
                "agent_observed_at": agent.get("observed_at"),
                "exporter_state": exporter.get("state", "unknown"),
                "exporter_sample_at": sample_at,
                "root_disk": _root_disk(host) if current and (exporter_fresh or ssh_fresh) else None,
                "resource_source": "ssh" if ssh_fresh else "exporter" if exporter_up else "unknown",
                "failed_service_count": failure_count,
                "failed_services": [
                    {
                        key: item[key]
                        for key in ("unit", "active_state", "sub_state")
                        if key in item
                    }
                    for item in shown_units
                ],
                "failed_services_truncated": failure_count is not None and failure_count > len(shown_units),
                "active_alert_count": alert_count,
                "alerts_truncated": len(host_alerts) > len(shown_alerts),
                "alerts_observed_at": alerts_result.get("observed_at"),
                "alerts": [
                    {
                        "name": _object(item.get("labels")).get("alertname"),
                        "severity": _object(item.get("labels")).get("severity"),
                        "summary": str(
                            _object(item.get("annotations")).get("summary", "")
                        )[:180],
                    }
                    for item in shown_alerts
                ],
            }
        )
    return {
        "ok": bool(summaries) and any(item["status"] == "online" for item in summaries),
        "source": "ops",
        "status": payload.get("status", "unavailable"),
        "observed_at": payload.get("observed_at"),
        "received_at": payload.get("received_at"),
        "acquired_at": captured_at,
        "assembled_at": timestamp,
        "hosts": summaries,
        "error": _error(payload),
        "scope": "服务与告警只覆盖已授权项；在线不代表所有业务均已验证。",
        "answer_guidance": "直接说明已有事实；单项查询失败不否定其他成功观测。状态已明确时不要要求用户重填主机名。",
    }


async def fleet_overview(client: Any, *, now: int | None = None) -> dict[str, Any]:
    async def read(call: Any) -> dict[str, Any]:
        try:
            return await call
        except FleetControlError as exc:
            return {"status": "unavailable", "error": {"code": exc.code, "message": str(exc)}}

    fleet, workers, policies = await asyncio.gather(
        read(client.fleet()), read(client.workers()), read(client.resource_policies())
    )
    timestamp = int(time.time()) if now is None else now
    summary = summarize_fleet(fleet, now=timestamp)
    policies_by_id = {
        str(item.get("worker_id")): item for item in _items(policies.get("items"))
    }
    items = []
    for worker in _items(workers.get("items")):
        policy = policies_by_id.get(str(worker.get("worker_id")), {})
        seen = worker.get("last_seen_at")
        fresh = isinstance(seen, (int, float)) and 0 <= timestamp - seen <= 45
        state = str(policy.get("desired_availability") or "unknown")
        ready = fresh and worker.get("availability") == "available" and state == "available"
        items.append({
            "worker_id": worker.get("worker_id"),
            "host_id": worker.get("host_id"),
            "last_seen_at": seen,
            "heartbeat_fresh": fresh,
            "availability": worker.get("availability", "unknown"),
            "owner_availability": state,
            "ready_for_scheduling": ready,
            "capabilities": worker.get("capabilities", []),
            "capacity": {key: value for key, value in _object(worker.get("capacity")).items()
                         if key in {"cpu_millis", "memory_bytes", "gpu_slots"}},
            "owner_limits": {key: policy[key] for key in
                             ("cpu_limit_millis", "memory_limit_bytes", "gpu_limit_slots") if key in policy},
        })
    summary["workers"] = {
        "source": "gaoji-control/worker-heartbeat",
        "status": "unavailable" if workers.get("error") else "partial" if policies.get("error") else "observed",
        "items": items,
        "error": _error(workers) or _error(policies),
        "guidance": "接单状态不等于当前用户有借用权限或剩余配额。指定机器请使用其 worker_id；离线或让路时不能擅自换机。",
    }
    return summary


def summarize_resources(payload: dict[str, Any], host_id: str, *, now: int) -> dict[str, Any]:
    data = _object(payload.get("data"))
    if not _fresh(payload, now) or data.get("host") != host_id:
        return {"status": "unavailable"}
    metrics = _object(_object(data.get("observation")).get("metrics"))
    def samples(name):
        return [sample for sample in _items(_object(metrics.get(name)).get("samples"))
            if sample.get("state") == "available" and type(sample.get("value")) in (int, float)
            and math.isfinite(sample["value"]) and _sample_fresh(sample.get("sample_at_unix_seconds"), now)]
    def scalar(name):
        values = samples(name)
        return values[0] if len(values) == 1 else None
    total, available = scalar("memory_total_bytes"), scalar("memory_available_bytes")
    idle = samples("cpu_idle_seconds_per_second")
    cpu_ids = [sample.get("labels", {}).get("cpu") for sample in idle]
    valid_cpu = bool(idle) and None not in cpu_ids and len(set(cpu_ids)) == len(idle) and all(0 <= sample["value"] <= 1 for sample in idle)
    valid_memory = total and available and 0 <= available["value"] <= total["value"] and total["value"] > 0
    result = {"status": "available" if valid_cpu and valid_memory else "partial"}
    if valid_cpu:
        window = _object(metrics.get("cpu_idle_seconds_per_second")).get("window_seconds", 300)
        if type(window) not in (int, float) or not math.isfinite(window) or window <= 0:
            window = None
        result.update(cpu_busy_percent=round((1 - sum(s["value"] for s in idle) / len(idle)) * 100, 2),
            cpu_window_seconds=window, cpu_observed_at=min(s["sample_at_unix_seconds"] for s in idle), logical_cpus=len(idle))
    if valid_memory:
        result.update(memory_total_bytes=int(total["value"]), memory_available_bytes=int(available["value"]),
            memory_observed_at=min(total["sample_at_unix_seconds"], available["sample_at_unix_seconds"]))
    load = scalar("load1")
    if load:
        result["load1"] = load["value"]
    return result


async def inspect_host(client: Any, host_id: str) -> dict[str, Any]:
    async def read(call: Any) -> tuple[dict[str, Any], int]:
        try:
            payload = await call
        except FleetControlError as exc:
            payload = {
                "status": "unavailable",
                "error": {"code": exc.code, "message": str(exc)},
            }
        return payload, int(time.time())

    async def read_metrics() -> dict[str, Any]:
        call = getattr(client, "host_metrics", None)
        return await call(host_id) if callable(call) else {"status": "unavailable"}

    (facts_result, _), (fleet_result, fleet_at), (metrics, _) = await asyncio.gather(
        read(client.host(host_id)), read(client.fleet()), read(read_metrics())
    )
    summary = summarize_fleet(fleet_result, host_id=host_id, acquired_at=fleet_at)
    for host in summary["hosts"]:
        host["resources"] = summarize_resources(metrics, host_id, now=int(time.time()))
    facts = _object(_object(facts_result.get("data")).get("facts"))
    summary["system"] = {
        "status": facts_result.get("status", "unavailable"),
        "observed_at": facts_result.get("observed_at"),
        **{
            key: facts[key]
            for key in (
                "kernel",
                "uptime_seconds",
                "profile_generation",
                "profile_matches_running",
            )
            if key in facts
        },
        "error": _error(facts_result),
    }
    if (
        _fresh(facts_result, int(time.time()))
        and _object(facts_result.get("data")).get("host") == host_id
    ):
        summary["ok"] = True
        if not any(item["status"] == "online" for item in summary["hosts"]):
            summary["summary"] = (
                f"{host_id} 在线，已成功读取实时系统信息；资源与告警概况未确认。"
            )
    return summary


async def model_status(context: Any, profile_name: str = "") -> dict[str, Any]:
    runtime = context.local_model
    name = profile_name.strip() or (runtime.profile.name if runtime else "qwen-local")
    profiles = {profile.name: profile for profile in context.model_catalog.profiles}
    profile = profiles.get(name)
    if profile is None:
        return {
            "ok": False,
            "error": "模型配置不存在。",
            "available_profiles": sorted(profiles),
        }
    readiness: dict[str, Any] | None = None
    if runtime is not None and runtime.profile.name == name:
        await runtime.probe_once()
        readiness = runtime.snapshot()
    health = context.llm_gateway.health_snapshot().get(name, {})
    success_at = int(health.get("last_success_at") or 0)
    failure_at = int(health.get("last_failure_at") or 0)
    last_response = (
        "failed"
        if failure_at >= success_at and failure_at
        else "succeeded"
        if success_at
        else "unknown"
    )
    if readiness is not None:
        summary = (
            "千问接口可访问，目标模型已列出"
            if readiness["ready"]
            else str(readiness["reason"])
        )
    else:
        summary = "未执行主动探测，以下为 Bot 进程启动后的实际请求记录"
    if last_response == "failed":
        summary += "；最近一次实际请求失败"
    elif last_response == "succeeded":
        summary += "；最近一次实际请求成功"
    else:
        summary += "；本进程暂无实际请求成功记录，尚不能确认生成回复正常"
    return {
        "ok": True,
        "source": "gaoji-model-runtime",
        "profile": name,
        "model": profile.model,
        "checked_at": int(time.time()),
        "summary": summary,
        "readiness": {
            key: readiness.get(key)
            for key in (
                "state",
                "ready",
                "reason",
                "checked_at",
                "latency_ms",
                "service_state",
                "control_configured",
            )
        }
        if readiness
        else None,
        "requests": {
            "latest_result": last_response,
            "last_success_at": success_at or None,
            "last_failure_at": failure_at or None,
            **{
                key: health.get(key)
                for key in (
                    "request_count",
                    "total_successes",
                    "total_failures",
                    "average_latency_ms",
                    "last_error_kind",
                )
            },
        },
        "routing": {
            "simple_chat_selected": context.settings.model_simple_chat_profile == name,
            "circuit_state": health.get("status", "unknown"),
            "circuit_breaker_enabled": profile.circuit_breaker_enabled,
        },
        "answer_guidance": "先回答接口与模型的当前状态，再按时间说明最近生成结果。/models 成功不等于生成成功；历史失败不等于现在仍失败。未配置启停管理接口不代表推理接口不可用。",
    }
