from __future__ import annotations

import asyncio
import json
import re
import socket
import time
import zlib
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx
from prometheus_client import Counter, Histogram

from src.bot_storage import PostgresDatabase

from .service import FleetControlService


MAX_DIAGNOSTIC_PROBES = 6
MAX_DIAGNOSTIC_PHASES = 2
DIAGNOSTIC_RUN_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class DiagnosticTemplate:
    key: str
    title: str
    description: str
    default_host: str = "local"

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class DiagnosticTarget:
    target_id: str
    label: str
    kind: str
    url: str
    observer_host: str
    host_id: str = ""
    service_ref: str = ""


@dataclass(frozen=True)
class EvidenceDraft:
    phase: int
    source_backend: str
    source_version: str
    observer_host: str
    target_ref: str
    check_name: str
    status: str
    facts: dict[str, Any]
    observed_at: int | None
    received_at: int
    valid_for_seconds: int
    source_ref: str = ""
    sensitive: bool = False


TEMPLATES: tuple[DiagnosticTemplate, ...] = (
    DiagnosticTemplate(
        "model_connectivity",
        "模型连接与参数",
        "区分 DNS、模型服务未启动或加载中、HTTP 错误和近期模型调用失败。",
    ),
    DiagnosticTemplate(
        "admin_502",
        "控制台 502",
        "检查访问入口、反向代理、Bot 控制台、集群控制服务和数据库等待。",
    ),
    DiagnosticTemplate(
        "qq_no_reply",
        "QQ 不回复",
        "检查 NapCat、Bot、任务队列、模型回合和 QQ 投递链路。",
    ),
    DiagnosticTemplate(
        "reply_latency",
        "回复变慢",
        "拆分模型回合、后台任务、数据库连接池和投递等待。",
    ),
    DiagnosticTemplate(
        "host_unreachable",
        "主机失联",
        "对照 Ops Agent、Exporter 和主机事实，避免把单一探针故障当成关机。",
    ),
    DiagnosticTemplate(
        "storage_pressure",
        "存储告警",
        "区分数据库体积、媒体索引、待归档任务和节点本身的存储告警。",
    ),
)
TEMPLATE_BY_KEY = {item.key: item for item in TEMPLATES}


def _summarize_model_routes(rows: list[Any]) -> dict[str, int]:
    counts = {
        "attempts": 0,
        "failed": 0,
        "fallbacks": 0,
        "parameter_incompatible": 0,
        "network": 0,
        "auth": 0,
        "billing": 0,
        "rate_limit": 0,
        "model_unavailable": 0,
        "provider": 0,
        "empty_response": 0,
    }
    for row in rows:
        try:
            payload = json.loads(
                zlib.decompress(bytes(row["payload"])).decode("utf-8")
            )
        except (
            KeyError,
            OSError,
            TypeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            continue
        routing = payload.get("model_routing")
        if not isinstance(routing, list):
            continue
        for decision in routing:
            if not isinstance(decision, dict):
                continue
            if decision.get("fallback") is True:
                counts["fallbacks"] += 1
            outcomes = decision.get("outcomes")
            if not isinstance(outcomes, list):
                continue
            for outcome in outcomes:
                if not isinstance(outcome, dict):
                    continue
                counts["attempts"] += 1
                status = str(outcome.get("status") or "")
                reason = str(outcome.get("reason_code") or "")
                if status == "failed":
                    counts["failed"] += 1
                if reason in {"invalid_request", "tool_choice_compatibility"}:
                    counts["parameter_incompatible"] += 1
                elif reason in counts:
                    counts[reason] += 1
    return counts


class DiagnosticStore:
    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    def create_run(
        self,
        *,
        template: str,
        host_id: str,
        subject: str,
        requested_by: str,
        started_at: int,
    ) -> int:
        connection = self.database.store_connection()
        try:
            row = connection.execute(
                """
                INSERT INTO diagnostic_runs (
                    template, host_id, subject, requested_by, status,
                    confidence, summary, conclusion_json, probe_count,
                    started_at, finished_at
                ) VALUES (?, ?, ?, ?, 'running', 'unknown', '', '{}', 0, ?, NULL)
                RETURNING run_id
                """,
                (template, host_id, subject[:1000], requested_by[:200], started_at),
            ).fetchone()
            connection.commit()
            return int(row["run_id"]) if row is not None else 0
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def add_evidence(self, run_id: int, item: EvidenceDraft) -> int:
        connection = self.database.store_connection()
        try:
            row = connection.execute(
                """
                INSERT INTO diagnostic_evidence (
                    run_id, phase, source_backend, source_version,
                    observer_host, target_ref, check_name, status, facts_json,
                    observed_at, received_at, valid_for_seconds, source_ref,
                    sensitive
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING evidence_id
                """,
                (
                    run_id,
                    item.phase,
                    item.source_backend,
                    item.source_version,
                    item.observer_host,
                    item.target_ref,
                    item.check_name,
                    item.status,
                    json.dumps(item.facts, ensure_ascii=False, sort_keys=True),
                    item.observed_at,
                    item.received_at,
                    item.valid_for_seconds,
                    item.source_ref,
                    item.sensitive,
                ),
            ).fetchone()
            connection.commit()
            return int(row["evidence_id"]) if row is not None else 0
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        confidence: str,
        summary: str,
        conclusion: dict[str, Any],
        probe_count: int,
        finished_at: int,
    ) -> None:
        connection = self.database.store_connection()
        try:
            connection.execute(
                """
                UPDATE diagnostic_runs
                SET status = ?, confidence = ?, summary = ?,
                    conclusion_json = ?, probe_count = ?, finished_at = ?
                WHERE run_id = ?
                """,
                (
                    status,
                    confidence,
                    summary[:2000],
                    json.dumps(conclusion, ensure_ascii=False, sort_keys=True),
                    probe_count,
                    finished_at,
                    run_id,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _decode(value: object, fallback: Any) -> Any:
        try:
            return json.loads(str(value))
        except (TypeError, json.JSONDecodeError):
            return fallback

    def recent(self, *, limit: int = 30) -> list[dict[str, Any]]:
        connection = self.database.store_connection()
        try:
            rows = connection.execute(
                """
                SELECT run_id, template, host_id, subject, requested_by,
                       status, confidence, summary, probe_count,
                       started_at, finished_at
                FROM diagnostic_runs
                ORDER BY run_id DESC LIMIT ?
                """,
                (min(max(int(limit), 1), 100),),
            ).fetchall()
            return [self._run_row(row) for row in rows]
        finally:
            connection.close()

    def get(self, run_id: int) -> dict[str, Any] | None:
        connection = self.database.store_connection()
        try:
            run = connection.execute(
                "SELECT * FROM diagnostic_runs WHERE run_id = ?",
                (int(run_id),),
            ).fetchone()
            if run is None:
                return None
            rows = connection.execute(
                """
                SELECT evidence_id, phase, source_backend, source_version,
                       observer_host, target_ref, check_name, status,
                       facts_json, observed_at, received_at, valid_for_seconds,
                       source_ref, sensitive
                FROM diagnostic_evidence
                WHERE run_id = ? ORDER BY phase, evidence_id
                """,
                (int(run_id),),
            ).fetchall()
            result = self._run_row(run)
            result["conclusion"] = self._decode(run["conclusion_json"], {})
            result["evidence"] = [
                {
                    **dict(row),
                    "handle": f"evidence#{int(row['evidence_id'])}",
                    "facts": self._decode(row["facts_json"], {}),
                }
                for row in rows
            ]
            for item in result["evidence"]:
                item.pop("facts_json", None)
            return result
        finally:
            connection.close()

    @staticmethod
    def _run_row(row: Any) -> dict[str, Any]:
        return {
            "run_id": int(row["run_id"]),
            "handle": f"diagnostic#{int(row['run_id'])}",
            "template": str(row["template"]),
            "host_id": str(row["host_id"]),
            "subject": str(row["subject"]),
            "requested_by": str(row["requested_by"]),
            "status": str(row["status"]),
            "confidence": str(row["confidence"]),
            "summary": str(row["summary"]),
            "probe_count": int(row["probe_count"]),
            "started_at": int(row["started_at"]),
            "finished_at": (
                int(row["finished_at"])
                if row["finished_at"] is not None
                else None
            ),
        }

    def database_snapshot(self) -> dict[str, Any]:
        try:
            self.database.healthcheck()
            topology = self.database.topology_snapshot()
        except Exception:
            return {
                "status": "failed",
                "facts": {"available": False, "overall": "offline"},
            }
        overall = str(topology.get("overall") or "unknown")
        pool = topology.get("pool") if isinstance(topology.get("pool"), dict) else {}
        status = "passed" if overall == "healthy" else "warning"
        if int(pool.get("waiting") or 0) > 0:
            status = "warning"
        return {
            "status": status,
            "facts": {
                "available": True,
                "overall": overall,
                "writable_node": topology.get("writable_node"),
                "online_nodes": sum(
                    1
                    for item in topology.get("nodes", [])
                    if isinstance(item, dict) and item.get("status") == "online"
                ),
                "node_count": len(topology.get("nodes", [])),
                "pool_size": int(pool.get("size") or 0),
                "pool_available": int(pool.get("available") or 0),
                "pool_waiting": int(pool.get("waiting") or 0),
            },
        }

    def runtime_snapshot(self, kind: str, *, since_seconds: int = 3600) -> dict[str, Any]:
        since = int(time.time()) - min(max(int(since_seconds), 60), 86400)
        connection = self.database.store_connection()
        try:
            if kind == "traces":
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS total,
                           COUNT(*) FILTER (WHERE status = 'crashed') AS crashed,
                           COUNT(*) FILTER (WHERE status = 'aborted') AS aborted,
                           COUNT(*) FILTER (WHERE status = 'running') AS running,
                           COALESCE(AVG(GREATEST(finished_at - started_at, 0))
                               FILTER (WHERE finished_at IS NOT NULL), 0) AS avg_seconds,
                           COALESCE(PERCENTILE_CONT(0.95) WITHIN GROUP
                               (ORDER BY GREATEST(finished_at - started_at, 0))
                               FILTER (WHERE finished_at IS NOT NULL), 0) AS p95_seconds
                    FROM agent_turns WHERE started_at >= ?
                    """,
                    (since,),
                ).fetchone()
                failed = int(row["crashed"] or 0) + int(row["aborted"] or 0)
                return {
                    "status": "warning" if failed else "passed",
                    "facts": {
                        "window_seconds": int(time.time()) - since,
                        "total": int(row["total"] or 0),
                        "crashed": int(row["crashed"] or 0),
                        "aborted": int(row["aborted"] or 0),
                        "running": int(row["running"] or 0),
                        "average_seconds": round(float(row["avg_seconds"] or 0), 2),
                        "p95_seconds": round(float(row["p95_seconds"] or 0), 2),
                    },
                }
            if kind == "model_failures":
                rows = connection.execute(
                    """
                    SELECT archive.payload
                    FROM turn_archives AS archive
                    JOIN agent_turns AS turn ON turn.turn_id = archive.turn_id
                    WHERE turn.started_at >= ? AND archive.expires_at > ?
                    ORDER BY turn.started_at DESC
                    LIMIT 500
                    """,
                    (since, int(time.time())),
                ).fetchall()
                counts = _summarize_model_routes(rows)
                abnormal = sum(
                    counts[key]
                    for key in (
                        "failed",
                        "fallbacks",
                        "parameter_incompatible",
                        "network",
                        "auth",
                        "billing",
                        "rate_limit",
                        "model_unavailable",
                        "provider",
                        "empty_response",
                    )
                )
                return {
                    "status": (
                        "unknown"
                        if not rows
                        else "warning"
                        if abnormal
                        else "passed"
                    ),
                    "facts": {
                        "window_seconds": int(time.time()) - since,
                        "archived_turns_examined": len(rows),
                        **counts,
                        "raw_prompts_persisted": False,
                        "raw_responses_persisted": False,
                    },
                }
            if kind == "deliveries":
                rows = connection.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM deliveries WHERE updated_at >= ? GROUP BY status
                    """,
                    (since,),
                ).fetchall()
                counts = {str(row["status"]): int(row["count"]) for row in rows}
                bad = counts.get("failed", 0) + counts.get("ambiguous", 0)
                return {
                    "status": "warning" if bad else "passed",
                    "facts": {"window_seconds": int(time.time()) - since, **counts},
                }
            if kind == "jobs":
                rows = connection.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM durable_jobs WHERE updated_at >= ? GROUP BY status
                    """,
                    (since,),
                ).fetchall()
                counts = {str(row["status"]): int(row["count"]) for row in rows}
                bad = counts.get("failed", 0)
                waiting = counts.get("pending", 0)
                return {
                    "status": "warning" if bad or waiting > 20 else "passed",
                    "facts": {"window_seconds": int(time.time()) - since, **counts},
                }
            if kind == "storage":
                row = connection.execute(
                    """
                    SELECT PG_DATABASE_SIZE(CURRENT_DATABASE()) AS database_bytes,
                           (SELECT COUNT(*) FROM media_blobs) AS media_count,
                           (SELECT COUNT(*) FROM durable_jobs
                              WHERE status IN ('pending','running')) AS active_jobs,
                           (SELECT COUNT(*) FROM durable_jobs
                              WHERE kind LIKE 'archive.%' AND status = 'failed')
                              AS failed_archives
                    """
                ).fetchone()
                failed_archives = int(row["failed_archives"] or 0)
                return {
                    "status": "warning" if failed_archives else "passed",
                    "facts": {
                        "database_bytes": int(row["database_bytes"] or 0),
                        "media_count": int(row["media_count"] or 0),
                        "active_jobs": int(row["active_jobs"] or 0),
                        "failed_archives": failed_archives,
                    },
                }
            if kind == "alerts":
                rows = connection.execute(
                    """
                    SELECT severity, COUNT(*) AS count
                    FROM alert_events WHERE status = 'firing' GROUP BY severity
                    """
                ).fetchall()
                counts = {str(row["severity"]): int(row["count"]) for row in rows}
                active = sum(counts.values())
                return {
                    "status": "warning" if active else "passed",
                    "facts": {"active": active, "by_severity": counts},
                }
            raise ValueError(f"unsupported runtime snapshot: {kind}")
        finally:
            connection.close()


class IncidentDiagnosticService:
    def __init__(
        self,
        fleet: FleetControlService,
        store: DiagnosticStore,
        targets: tuple[dict[str, str], ...] = (),
        *,
        local_host_id: str = "local",
    ) -> None:
        self.fleet = fleet
        self.store = store
        self.targets = tuple(DiagnosticTarget(**item) for item in targets)
        self.local_host_id = local_host_id
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(6.0),
            follow_redirects=False,
            trust_env=False,
        )
        self._run_counter = Counter(
            "gaoji_cluster_diagnostics_total",
            "Experimental diagnostic runs by template and outcome.",
            ("template", "status"),
            registry=fleet.metrics_registry,
        )
        self._evidence_counter = Counter(
            "gaoji_cluster_diagnostic_evidence_total",
            "Structured diagnostic evidence by check and status.",
            ("check", "status"),
            registry=fleet.metrics_registry,
        )
        self._duration = Histogram(
            "gaoji_cluster_diagnostic_duration_seconds",
            "End-to-end diagnostic run latency.",
            ("template",),
            registry=fleet.metrics_registry,
            buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 180),
        )

    async def close(self) -> None:
        await self._http.aclose()

    def templates(self) -> list[dict[str, str]]:
        return [item.as_dict() for item in TEMPLATES]

    def recent(self, *, limit: int = 30) -> list[dict[str, Any]]:
        return self.store.recent(limit=limit)

    def detail(self, run_id: int) -> dict[str, Any] | None:
        return self.store.get(run_id)

    def _target(self, kind: str, target_id: str) -> DiagnosticTarget | None:
        if target_id:
            return next(
                (
                    item
                    for item in self.targets
                    if item.target_id == target_id and item.kind == kind
                ),
                None,
            )
        return next((item for item in self.targets if item.kind == kind), None)

    async def run(
        self,
        *,
        template_key: str,
        host_id: str,
        target_id: str = "",
        subject: str = "",
        requested_by: str = "gaoji",
    ) -> dict[str, Any]:
        template = TEMPLATE_BY_KEY.get(template_key)
        if template is None:
            raise ValueError("unknown diagnostic template")
        host = host_id.strip() or template.default_host
        if host == "local":
            host = self.local_host_id
        if self.fleet.inventory_policy(host) is None:
            raise PermissionError("host is outside the configured inventory")
        started_at = int(time.time())
        started_monotonic = time.monotonic()
        run_id = await asyncio.to_thread(
            self.store.create_run,
            template=template.key,
            host_id=host,
            subject=subject,
            requested_by=requested_by,
            started_at=started_at,
        )
        evidence: list[EvidenceDraft] = []
        try:
            async def collect() -> dict[str, Any]:
                phase_one = await self._phase_one(template.key, host, target_id)
                evidence.extend(phase_one[:MAX_DIAGNOSTIC_PROBES])
                if (
                    self._needs_second_phase(evidence)
                    and len(evidence) < MAX_DIAGNOSTIC_PROBES
                ):
                    remaining = MAX_DIAGNOSTIC_PROBES - len(evidence)
                    phase_two = await self._phase_two(template.key, host, evidence)
                    evidence.extend(phase_two[:remaining])
                for item in evidence:
                    if item.phase > MAX_DIAGNOSTIC_PHASES:
                        raise RuntimeError("diagnostic phase limit exceeded")
                    await asyncio.to_thread(self.store.add_evidence, run_id, item)
                    self._evidence_counter.labels(
                        check=item.check_name,
                        status=item.status,
                    ).inc()
                return self._conclusion(template, evidence)

            conclusion = await asyncio.wait_for(
                collect(),
                timeout=DIAGNOSTIC_RUN_TIMEOUT_SECONDS,
            )
            await asyncio.to_thread(
                self.store.finish_run,
                run_id,
                status=str(conclusion["run_status"]),
                confidence=str(conclusion["confidence"]),
                summary=str(conclusion["summary"]),
                conclusion=conclusion,
                probe_count=len(evidence),
                finished_at=int(time.time()),
            )
            self._run_counter.labels(
                template=template.key,
                status=str(conclusion["run_status"]),
            ).inc()
        except Exception as exc:
            await asyncio.to_thread(
                self.store.finish_run,
                run_id,
                status="failed",
                confidence="unknown",
                summary="排障流程自身执行失败，不能据此判断目标状态。",
                conclusion={"error": type(exc).__name__},
                probe_count=len(evidence),
                finished_at=int(time.time()),
            )
            self._run_counter.labels(template=template.key, status="failed").inc()
            raise
        finally:
            self._duration.labels(template=template.key).observe(
                max(time.monotonic() - started_monotonic, 0.0)
            )
        result = await asyncio.to_thread(self.store.get, run_id)
        if result is None:
            raise RuntimeError("diagnostic result was not persisted")
        return result

    async def _phase_one(
        self,
        template: str,
        host: str,
        target_id: str,
    ) -> list[EvidenceDraft]:
        checks: list[Callable[[], Awaitable[list[EvidenceDraft]]]] = []
        if template == "model_connectivity":
            checks = [
                lambda: self._probe(self._target("model", target_id), phase=1),
                lambda: self._unit(host, "gaoji.service", phase=1),
                lambda: self._runtime("traces", host, phase=1),
                lambda: self._runtime("model_failures", host, phase=1),
            ]
        elif template == "admin_502":
            checks = [
                lambda: self._probe(self._target("admin", target_id), phase=1),
                lambda: self._unit(host, "nginx.service", phase=1),
                lambda: self._unit(host, "gaoji.service", phase=1),
                lambda: self._unit(host, "gaoji-cluster-control.service", phase=1),
                lambda: self._database(host, phase=1),
            ]
        elif template == "qq_no_reply":
            checks = [
                lambda: self._unit(host, "docker-napcat.service", phase=1),
                lambda: self._unit(host, "gaoji.service", phase=1),
                lambda: self._runtime("deliveries", host, phase=1),
                lambda: self._runtime("traces", host, phase=1),
                lambda: self._runtime("jobs", host, phase=1),
            ]
        elif template == "reply_latency":
            checks = [
                lambda: self._runtime("traces", host, phase=1),
                lambda: self._runtime("jobs", host, phase=1),
                lambda: self._runtime("deliveries", host, phase=1),
                lambda: self._database(host, phase=1),
            ]
        elif template == "host_unreachable":
            checks = [
                lambda: self._host(host, phase=1),
                lambda: self._fleet_host(host, phase=1),
            ]
        elif template == "storage_pressure":
            checks = [
                lambda: self._host(host, phase=1),
                lambda: self._runtime("alerts", host, phase=1),
                lambda: self._runtime("storage", host, phase=1),
            ]
        results = await asyncio.gather(*(check() for check in checks))
        return [item for group in results for item in group]

    async def _phase_two(
        self,
        template: str,
        host: str,
        evidence: list[EvidenceDraft],
    ) -> list[EvidenceDraft]:
        if template in {"model_connectivity", "reply_latency"}:
            return await self._logs(host, "gaoji.service", phase=2)
        if template == "admin_502":
            unit = next(
                (
                    str(item.target_ref).removeprefix(f"host:{host}/unit:")
                    for item in evidence
                    if item.check_name == "service_status"
                    and item.status in {"failed", "warning"}
                ),
                "nginx.service",
            )
            return await self._logs(host, unit, phase=2)
        if template == "qq_no_reply":
            unit = next(
                (
                    str(item.target_ref).removeprefix(f"host:{host}/unit:")
                    for item in evidence
                    if item.check_name == "service_status"
                    and item.status in {"failed", "warning"}
                ),
                "gaoji.service",
            )
            return await self._logs(host, unit, phase=2)
        return []

    @staticmethod
    def _needs_second_phase(evidence: list[EvidenceDraft]) -> bool:
        return any(item.status in {"failed", "warning", "unknown"} for item in evidence)

    async def _probe(
        self,
        target: DiagnosticTarget | None,
        *,
        phase: int,
    ) -> list[EvidenceDraft]:
        now = int(time.time())
        if target is None:
            return [
                EvidenceDraft(
                    phase,
                    "gaoji-fixed-probe",
                    "probe-v1",
                    "",
                    "probe:unconfigured",
                    "fixed_target",
                    "unknown",
                    {"reason": "no approved target is configured"},
                    None,
                    now,
                    0,
                )
            ]
        if target.observer_host != self.local_host_id:
            return [
                EvidenceDraft(
                    phase,
                    "gaoji-fixed-probe",
                    "probe-v1",
                    self.local_host_id,
                    f"probe:{target.target_id}",
                    "fixed_target",
                    "unknown",
                    {
                        "reason": "approved observer is not this control-service host",
                        "approved_observer": target.observer_host,
                        "actual_observer": self.local_host_id,
                    },
                    None,
                    now,
                    0,
                    f"target:{target.target_id}",
                )
            ]
        parsed = urlsplit(target.url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        loop = asyncio.get_running_loop()
        try:
            records = await asyncio.wait_for(
                loop.getaddrinfo(
                    parsed.hostname,
                    port,
                    type=socket.SOCK_STREAM,
                ),
                timeout=3.0,
            )
            addresses = sorted({str(item[4][0]) for item in records})[:8]
            dns = EvidenceDraft(
                phase,
                "gaoji-fixed-probe",
                "probe-v1",
                target.observer_host,
                f"probe:{target.target_id}",
                "dns_resolution",
                "passed",
                {"hostname": parsed.hostname, "addresses": addresses},
                now,
                now,
                30,
                f"target:{target.target_id}",
            )
        except (OSError, TimeoutError, asyncio.TimeoutError, socket.gaierror) as exc:
            return [
                EvidenceDraft(
                    phase,
                    "gaoji-fixed-probe",
                    "probe-v1",
                    target.observer_host,
                    f"probe:{target.target_id}",
                    "dns_resolution",
                    "failed",
                    {"hostname": parsed.hostname, "error": type(exc).__name__},
                    now,
                    int(time.time()),
                    30,
                    f"target:{target.target_id}",
                )
            ]
        started = time.monotonic()
        try:
            async with self._http.stream(
                "GET",
                target.url,
                headers={"Accept": "application/json"},
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 64 * 1024:
                        break
                status_code = response.status_code
                content_type = response.headers.get("content-type", "")[:120]
            facts: dict[str, Any] = {
                "http_status": status_code,
                "content_type": content_type,
                "elapsed_ms": max(int((time.monotonic() - started) * 1000), 0),
            }
            try:
                payload = json.loads(bytes(body[: 64 * 1024]))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                if isinstance(payload.get("data"), list):
                    facts["item_count"] = len(payload["data"])
                error = payload.get("error")
                if isinstance(error, dict):
                    facts["error_type"] = str(error.get("type") or "")[:80]
                    facts["error_code"] = str(error.get("code") or "")[:80]
            status = "passed" if 200 <= status_code < 300 else "failed"
            if status_code in {408, 425, 429, 503}:
                status = "warning"
            http_item = EvidenceDraft(
                phase,
                "gaoji-fixed-probe",
                "probe-v1",
                target.observer_host,
                f"probe:{target.target_id}",
                "http_request",
                status,
                facts,
                int(time.time()),
                int(time.time()),
                30,
                f"target:{target.target_id}",
            )
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            http_item = EvidenceDraft(
                phase,
                "gaoji-fixed-probe",
                "probe-v1",
                target.observer_host,
                f"probe:{target.target_id}",
                "http_request",
                "failed",
                {
                    "error": type(exc).__name__,
                    "elapsed_ms": max(int((time.monotonic() - started) * 1000), 0),
                },
                int(time.time()),
                int(time.time()),
                30,
                f"target:{target.target_id}",
            )
        return [dns, http_item]

    async def _unit(self, host: str, unit: str, *, phase: int) -> list[EvidenceDraft]:
        policy = self.fleet.inventory_policy(host) or {}
        readable = policy.get("readable_units", [])
        now = int(time.time())
        if unit not in readable:
            return [
                EvidenceDraft(
                    phase,
                    "gaoji",
                    "diagnostic-v1",
                    host,
                    f"host:{host}/unit:{unit}",
                    "service_status",
                    "unknown",
                    {"reason": "unit is not in the approved readable scope"},
                    None,
                    now,
                    0,
                )
            ]
        payload = await self.fleet.unit_status(host, unit)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        unit_data = data.get("unit") if isinstance(data.get("unit"), dict) else {}
        active_state = str(unit_data.get("active_state") or "unknown")
        result_status = str(payload.get("status") or "unknown")
        if payload.get("ok") and active_state == "active":
            status = "passed" if result_status == "fresh" else "warning"
        elif payload.get("ok"):
            status = "failed"
        else:
            status = "unknown"
        return [
            EvidenceDraft(
                phase,
                str(payload.get("source_backend") or "ops"),
                "catalog-v1",
                host,
                f"host:{host}/unit:{unit}",
                "service_status",
                status,
                {
                    "query_status": result_status,
                    "active_state": active_state,
                    "sub_state": str(unit_data.get("sub_state") or ""),
                    "load_state": str(unit_data.get("load_state") or ""),
                    "error_code": (payload.get("error") or {}).get("code")
                    if isinstance(payload.get("error"), dict)
                    else "",
                },
                payload.get("observed_at"),
                int(payload.get("received_at") or now),
                30,
                f"ops:units.status:{host}:{unit}",
            )
        ]

    async def _host(self, host: str, *, phase: int) -> list[EvidenceDraft]:
        now = int(time.time())
        payload = await self.fleet.host_facts(host)
        status = "passed" if payload.get("status") == "fresh" else "unknown"
        if payload.get("status") == "stale":
            status = "warning"
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        facts = data.get("facts") if isinstance(data.get("facts"), dict) else {}
        return [
            EvidenceDraft(
                phase,
                str(payload.get("source_backend") or "ops"),
                "catalog-v1",
                host,
                f"host:{host}",
                "host_facts",
                status,
                {
                    "query_status": payload.get("status"),
                    "kernel": facts.get("kernel"),
                    "uptime_seconds": facts.get("uptime_seconds"),
                    "system_closure": facts.get("system_closure"),
                },
                payload.get("observed_at"),
                int(payload.get("received_at") or now),
                30,
                f"ops:host.facts:{host}",
            )
        ]

    async def _fleet_host(self, host: str, *, phase: int) -> list[EvidenceDraft]:
        now = int(time.time())
        payload = await self.fleet.fleet_overview()
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        hosts = data.get("hosts") if isinstance(data.get("hosts"), list) else []
        item = next(
            (
                row
                for row in hosts
                if isinstance(row, dict)
                and str(row.get("host") or row.get("host_id") or "") == host
            ),
            None,
        )
        if item is None:
            status = "unknown"
            facts: dict[str, Any] = {"reason": "host has no current fleet observation"}
        else:
            agent = item.get("agent") if isinstance(item.get("agent"), dict) else {}
            exporter = item.get("exporter") if isinstance(item.get("exporter"), dict) else {}
            agent_state = str(agent.get("state") or "unknown")
            exporter_state = str(exporter.get("state") or "unknown")
            if agent_state == "reachable" and exporter_state in {"up", "unknown", ""}:
                status = "passed"
            elif agent_state == "unreachable" or exporter_state == "down":
                status = "failed"
            else:
                status = "warning"
            facts = {
                "agent_state": agent_state,
                "exporter_state": exporter_state,
                "failed_units": agent.get("failed_units"),
            }
        return [
            EvidenceDraft(
                phase,
                str(payload.get("source_backend") or "ops"),
                "catalog-v1",
                "ops-hub",
                f"host:{host}",
                "fleet_observers",
                status,
                facts,
                payload.get("observed_at"),
                int(payload.get("received_at") or now),
                30,
                "ops:fleet.overview",
            )
        ]

    async def _database(self, host: str, *, phase: int) -> list[EvidenceDraft]:
        result = await asyncio.to_thread(self.store.database_snapshot)
        now = int(time.time())
        return [
            EvidenceDraft(
                phase,
                "gaoji-postgres",
                "topology-v1",
                host,
                "database:primary",
                "database_topology",
                str(result["status"]),
                dict(result["facts"]),
                now,
                now,
                10,
                "postgres:topology",
            )
        ]

    async def _runtime(self, kind: str, host: str, *, phase: int) -> list[EvidenceDraft]:
        now = int(time.time())
        try:
            result = await asyncio.to_thread(self.store.runtime_snapshot, kind)
            status = str(result["status"])
            facts = dict(result["facts"])
        except Exception as exc:
            status = "unknown"
            facts = {"error": type(exc).__name__}
        return [
            EvidenceDraft(
                phase,
                "gaoji-postgres",
                "runtime-v1",
                host,
                f"gaoji:{kind}",
                f"runtime_{kind}",
                status,
                facts,
                now,
                now,
                15,
                f"postgres:{kind}",
            )
        ]

    async def _logs(self, host: str, unit: str, *, phase: int) -> list[EvidenceDraft]:
        now = int(time.time())
        try:
            payload = await self.fleet.unit_logs(host, unit, 80, 3600)
        except Exception as exc:
            return [
                EvidenceDraft(
                    phase,
                    "ops",
                    "catalog-v1",
                    host,
                    f"host:{host}/unit:{unit}",
                    "service_log_signals",
                    "unknown",
                    {"error": type(exc).__name__},
                    None,
                    now,
                    0,
                    f"ops:units.logs:{host}:{unit}",
                )
            ]
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        entries = data.get("entries") if isinstance(data.get("entries"), list) else []
        messages = [
            str(item.get("message") or "")
            for item in entries
            if isinstance(item, dict)
        ]
        lowered = [message.lower() for message in messages]
        error_like = sum(
            bool(re.search(r"\b(error|failed|exception|fatal|crash)\b", message))
            for message in lowered
        )
        timeout_like = sum("timeout" in message or "timed out" in message for message in lowered)
        auth_like = sum(
            any(token in message for token in ("unauthorized", "forbidden", "401", "403"))
            for message in lowered
        )
        status = "warning" if error_like or timeout_like or auth_like else "passed"
        if not payload.get("ok"):
            status = "unknown"
        return [
            EvidenceDraft(
                phase,
                "ops",
                "catalog-v1",
                host,
                f"host:{host}/unit:{unit}",
                "service_log_signals",
                status,
                {
                    "entry_count": len(entries),
                    "error_like": error_like,
                    "timeout_like": timeout_like,
                    "auth_like": auth_like,
                    "raw_logs_persisted": False,
                },
                payload.get("observed_at"),
                int(payload.get("received_at") or now),
                0,
                f"ops:units.logs:{host}:{unit}",
            )
        ]

    @staticmethod
    def _conclusion(
        template: DiagnosticTemplate,
        evidence: list[EvidenceDraft],
    ) -> dict[str, Any]:
        failed = [item for item in evidence if item.status == "failed"]
        warnings = [item for item in evidence if item.status == "warning"]
        unknown = [item for item in evidence if item.status == "unknown"]
        passed = [item for item in evidence if item.status == "passed"]
        model_failures = next(
            (
                item
                for item in evidence
                if item.check_name == "runtime_model_failures"
            ),
            None,
        )
        http_passed = any(
            item.check_name == "http_request" and item.status == "passed"
            for item in evidence
        )
        if failed:
            first = failed[0]
            summaries = {
                "dns_resolution": "固定目标的 DNS 解析失败，后续 HTTP 结果不能代表服务本体。",
                "http_request": "DNS 已有结果，但固定 HTTP 探测失败，问题更接近服务或访问路径。",
                "service_status": "受管 systemd 服务没有处于 active 状态。",
                "fleet_observers": "至少一个独立主机观察信号报告不可达。",
                "database_topology": "数据库健康或可写拓扑检查失败。",
            }
            summary = summaries.get(first.check_name, "排障检查发现了直接失败证据。")
            confidence = "confirmed" if first.check_name in summaries else "supported"
            run_status = "completed"
        elif warnings:
            if (
                http_passed
                and model_failures is not None
                and int(model_failures.facts.get("parameter_incompatible") or 0) > 0
            ):
                summary = "模型接口当前可达，但近期存在请求参数或工具选择兼容问题。"
            else:
                summary = "没有直接失败证据，但发现需要关注的异常迹象。"
            confidence = "supported"
            run_status = "inconclusive"
        elif passed and not unknown:
            summary = "当前固定检查均通过，本次没有复现所描述的问题。"
            confidence = "contradicted"
            run_status = "completed"
        else:
            summary = "现有证据不足，不能可靠判断原因。"
            confidence = "unknown"
            run_status = "inconclusive"
        return {
            "template": template.key,
            "title": template.title,
            "run_status": run_status,
            "confidence": confidence,
            "summary": summary,
            "counts": {
                "passed": len(passed),
                "failed": len(failed),
                "warning": len(warnings),
                "unknown": len(unknown),
            },
            "failed_checks": [item.check_name for item in failed],
            "open_checks": [item.check_name for item in (*warnings, *unknown)],
            "limits": {
                "max_probes": MAX_DIAGNOSTIC_PROBES,
                "max_phases": MAX_DIAGNOSTIC_PHASES,
            },
        }
