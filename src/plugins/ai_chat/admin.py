from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal, Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from .admin_control import (
    AdminControlStore,
    AdminVersionConflict,
    MutationResult,
    parse_expected_version,
)
from .admin_dashboard import ADMIN_FAVICON_SVG, admin_asset_path, dashboard_html
from .admin_fleet import register_fleet_admin_routes
from .admin_security import register_security_routes, register_http_executor, secure_route, require_admin, SESSION_COOKIE
from src.bot_security.service import MobileAuthorization, audit_actor
from src.bot_security.store import SecurityError
from .conversation_scope import ConversationScope
from .model_catalog import SUPPORTED_REASONING_EFFORTS
from .local_model import LocalModelControlError
from .sandbox import SANDBOX_ID_PATTERN, SandboxError
from .tool_policy import (
    TOOL_POLICIES,
    admin_tool_manifest,
    configure_tool_overrides,
    set_tool_enabled,
    tool_enabled,
)


class SubAgentModelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    policy: dict[str, Any]


class SubAgentRevisionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    instruction: str = Field(min_length=1, max_length=12000)
    step_keys: list[str] = Field(min_length=1, max_length=14)
    file_delivery_required: bool | None = None


@dataclass(frozen=True)
class AdminServices:
    version: str
    started_at: int
    delivery_store: Any = None
    usage_store: Any = None
    running_tasks: Any = None
    job_store: Any = None
    job_worker: Any = None
    subagent_store: Any = None
    subagent_coordinator: Any = None
    bridge_router: Any = None
    bridge_state: Any = None
    browser_manager: Any = None
    background_tasks: Any = None
    model_catalog: Any = None
    llm_gateway: Any = None
    local_model: Any = None
    model_preferences: Any = None
    reasoning_preferences: Any = None
    user_profiles: Any = None
    message_ledger: Any = None
    settings: Any = None
    sandbox_manager: Any = None
    sticker_inventory: Any = None
    media_library: Any = None
    source_store: Any = None
    media_cleanup: Any = None
    vision_worker: Any = None
    turn_journal: Any = None
    database: Any = None
    telemetry: Any = None
    alert_store: Any = None
    alert_preferences: Any = None
    fleet_client: Any = None
    mobile_authorization: MobileAuthorization | None = None


@dataclass(frozen=True)
class AdminMutationContext:
    expected_version: int | None
    actor: str


class ModelSelectionRequest(BaseModel):
    profile: str | None = None


class LocalModelControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["start", "stop"]
    request_id: UUID


class ReasoningEffortRequest(BaseModel):
    effort: str | None = None


class GroupEnabledRequest(BaseModel):
    enabled: bool


class AlertNotificationControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class SandboxActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["start", "stop", "destroy"]
    confirmation: str = Field(default="", max_length=32)


class CleanupConfirmationRequest(BaseModel):
    confirmation_token: str


class ToolEnabledRequest(BaseModel):
    enabled: bool


class MediaReviewRequest(BaseModel):
    state: str


class ContextFeedbackRequest(BaseModel):
    verdict: str
    note: str = ""


class AdminEventBroker:
    def __init__(
        self,
        version_provider: Callable[[], dict[str, int]] | None = None,
    ) -> None:
        self._sequence = 0
        self._subscribers: dict[
            asyncio.Queue[dict[str, object]], asyncio.AbstractEventLoop | None
        ] = {}
        self._version_provider = version_provider
        self._lock = threading.Lock()

    def subscribe(self) -> asyncio.Queue[dict[str, object]]:
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=32)
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        with self._lock:
            self._subscribers[queue] = loop
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, object]]) -> None:
        with self._lock:
            self._subscribers.pop(queue, None)

    @property
    def has_subscribers(self) -> bool:
        with self._lock:
            return bool(self._subscribers)

    def publish(self, *resources: str) -> None:
        self._publish(resources, include_versions=True)

    def publish_runtime(self, *resources: str) -> None:
        self._publish(resources, include_versions=False)

    def _publish(
        self,
        resources: tuple[str, ...],
        *,
        include_versions: bool,
    ) -> None:
        normalized = sorted({str(item).strip() for item in resources if item})
        if not normalized:
            return
        with self._lock:
            self._sequence += 1
            payload: dict[str, object] = {
                "sequence": self._sequence,
                "type": "resources.changed",
                "resources": normalized,
                "timestamp": int(time.time()),
            }
            if include_versions and self._version_provider is not None:
                versions = self._version_provider()
                payload["versions"] = {
                    resource: versions.get(resource, 0) for resource in normalized
                }
            subscribers = tuple(self._subscribers.items())
        for queue, loop in subscribers:
            if loop is None:
                self._offer(queue, payload)
            elif not loop.is_closed():
                loop.call_soon_threadsafe(self._offer, queue, payload)

    @staticmethod
    def _offer(
        queue: asyncio.Queue[dict[str, object]],
        payload: dict[str, object],
    ) -> None:
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            return


_DATABASE_RESOURCE_MAP: dict[str, tuple[str, ...]] = {
    "conversations": ("groups",),
    "principals": ("groups",),
    "principal_identities": ("groups",),
    "messages": ("groups", "context-debug"),
    "deliveries": ("deliveries", "subagents", "overview", "observability"),
    "delivery_attempts": ("deliveries", "subagents", "overview", "observability"),
    "usage_events": ("usage", "overview", "observability"),
    "agent_turns": ("traces", "context-debug", "observability"),
    "turn_journal_events": ("traces", "context-debug", "observability"),
    "turn_context_plans": ("traces", "context-debug"),
    "turn_context_feedback": ("context-debug",),
    "turn_edges": ("traces", "context-debug"),
    "turn_digests": ("traces", "context-debug"),
    "durable_jobs": ("jobs", "media", "context-debug", "overview"),
    "media_blobs": ("media", "stickers"),
    "message_media": ("media", "stickers"),
    "media_jobs": ("media",),
    "vision_jobs": ("media",),
    "media_cleanup_runs": ("media",),
    "content_sources": ("sources",),
    "message_sources": ("sources",),
    "alert_events": ("alerts", "observability"),
    "alert_notifications": ("alerts", "observability"),
    "diagnostic_runs": ("fleet", "observability"),
    "diagnostic_evidence": ("fleet", "observability"),
    "subagent_tasks": ("subagents", "tasks", "overview"),
    "subagent_runs": ("subagents", "tasks", "overview"),
    "subagent_events": ("subagents", "tasks", "overview"),
    "subagent_artifacts": ("subagents",),
    "subagent_checkpoints": ("subagents", "tasks", "overview"),
    "subagent_run_contexts": ("subagents", "tasks", "overview"),
    "subagent_controls": ("subagents", "tasks", "overview"),
    "subagent_sessions": ("subagents", "tasks"),
    "subagent_deliveries": ("subagents", "tasks", "deliveries"),
    "subagent_external_calls": ("subagents", "tasks"),
    "subagent_evidence": ("subagents", "tasks"),
    "bridge_sources": ("overview",),
    "bridge_deliveries": ("overview",),
    "bridge_cursors": ("overview",),
    "fleet_backend_states": ("fleet", "overview"),
    "fleet_observations": ("fleet",),
    "fleet_operations": ("fleet",),
    "fleet_operation_events": ("fleet",),
    "fleet_approvals": ("fleet",),
}


def _changed_database_resources(
    previous: dict[str, tuple[int, int, int]],
    current: dict[str, tuple[int, int, int]],
) -> set[str]:
    if not previous:
        return set()
    changed: set[str] = set()
    for table, counters in current.items():
        if previous.get(table) != counters:
            changed.update(_DATABASE_RESOURCE_MAP.get(table, ()))
    return changed


class AdminRealtimeMonitor:
    """Turn runtime and database changes into one low-cost SSE change feed."""

    def __init__(self, services: AdminServices, broker: AdminEventBroker) -> None:
        self.services = services
        self.broker = broker
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        database_counters: dict[str, tuple[int, int, int]] | None = None
        preference_signature: tuple[tuple[str, str], ...] | None = None
        task_signature: tuple[tuple[str, str], ...] | None = None
        first = True
        next_process = 0.0
        next_external = 0.0
        next_fleet = 0.0
        try:
            while self.broker.has_subscribers:
                changed: set[str] = set()
                counters = await asyncio.to_thread(self._database_counters)
                if counters is not None:
                    changed.update(
                        _changed_database_resources(
                            database_counters or {}, counters
                        )
                    )
                    database_counters = counters

                current_preferences = self._preference_signature()
                if (
                    preference_signature is not None
                    and current_preferences != preference_signature
                ):
                    changed.add("groups")
                preference_signature = current_preferences

                current_tasks = self._task_signature()
                if task_signature is not None and current_tasks != task_signature:
                    changed.update(("tasks", "sandboxes", "overview"))
                task_signature = current_tasks

                now = time.monotonic()
                if first:
                    changed.update(
                        (
                            "overview",
                            "observability",
                            "alerts",
                            "deliveries",
                            "usage",
                            "tasks",
                            "subagents",
                            "jobs",
                            "sandboxes",
                            "stickers",
                            "media",
                            "sources",
                            "databases",
                            "fleet",
                            "groups",
                            "tools",
                            "traces",
                            "context-debug",
                            "audit",
                        )
                    )
                    first = False
                if now >= next_process:
                    changed.update(("overview", "observability", "sandboxes", "local-model"))
                    next_process = now + 2.0
                if now >= next_external:
                    changed.update(("alerts", "databases", "stickers"))
                    next_external = now + 5.0
                if now >= next_fleet:
                    changed.add("fleet")
                    next_fleet = now + 10.0
                if changed:
                    self.broker.publish_runtime(*changed)
                await asyncio.sleep(1.0)
        finally:
            self._task = None

    def _database_counters(self) -> dict[str, tuple[int, int, int]] | None:
        database = self.services.database
        if database is None or not callable(getattr(database, "store_connection", None)):
            return None
        try:
            connection = database.store_connection()
        except Exception:
            return None
        try:
            rows = connection.execute(
                """
                SELECT relname, n_tup_ins, n_tup_upd, n_tup_del
                FROM pg_stat_user_tables
                WHERE schemaname = current_schema()
                """
            ).fetchall()
        except Exception:
            return None
        finally:
            connection.close()
        return {
            str(row["relname"]): (
                int(row["n_tup_ins"]),
                int(row["n_tup_upd"]),
                int(row["n_tup_del"]),
            )
            for row in rows
        }

    def _preference_signature(self) -> tuple[tuple[str, str], ...]:
        values: list[tuple[str, str]] = []
        for store in (
            self.services.model_preferences,
            self.services.reasoning_preferences,
        ):
            items = getattr(store, "items", None)
            if callable(items):
                values.extend((str(key), str(value)) for key, value in items())
        return tuple(sorted(values))

    def _task_signature(self) -> tuple[tuple[str, str], ...]:
        running_tasks = self.services.running_tasks
        if running_tasks is None:
            return ()
        return tuple(
            sorted(
                (str(item.task_id), str(item.summary))
                for item in running_tasks.list_all()
            )
        )


def register_admin(
    app: Any,
    services: AdminServices,
    *,
    path: str = "/bot-admin",
    token: str = "",  # Obsolete: accepted only to reject old callers without reopening token login.
) -> None:
    prefix = "/" + path.strip("/")
    mobile = services.mobile_authorization
    origin = str(getattr(services.settings, "admin_origin", "") or "")
    router = APIRouter(prefix=prefix, route_class=secure_route(mobile, prefix=prefix, origin=origin))
    register_security_routes(router, mobile, services, prefix=prefix, origin=origin)
    control_store = AdminControlStore(services.database)
    configure_tool_overrides(control_store.tool_overrides())
    event_broker = AdminEventBroker(control_store.versions)
    if services.subagent_store is not None:
        services.subagent_store.set_change_listener(
            lambda _task_id: event_broker.publish_runtime(
                "subagents", "tasks", "overview"
            )
        )
    realtime_monitor = AdminRealtimeMonitor(services, event_broker)

    def authorize(authorization: Optional[str] = Header(default=None)) -> None:
        require_admin()

    def authorize_management(authorization: Optional[str] = None) -> None:
        authorize(authorization)

    def mutation_context(
        if_match: Optional[str] = Header(default=None, alias="If-Match"),
        admin_actor: Optional[str] = Header(default=None, alias="X-Admin-Actor"),
    ) -> AdminMutationContext:
        try:
            expected_version = parse_expected_version(if_match)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return AdminMutationContext(
            expected_version=expected_version,
            actor=audit_actor(),
        )

    def mutate(
        context: AdminMutationContext,
        resource_key: str,
        *,
        action: str,
        target: str = "",
        before: object = None,
        operation: Callable[[int], Any],
    ) -> MutationResult:
        try:
            return control_store.mutate(
                resource_key,
                expected_version=context.expected_version,
                actor=context.actor,
                action=action,
                target=target,
                before=before,
                operation=operation,
            )
        except AdminVersionConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "resource_version_conflict",
                    "resource": exc.resource_key,
                    "expected_version": exc.expected,
                    "current_version": exc.current,
                },
            ) from None

    def mutation_payload(result: MutationResult, **payload: object) -> dict[str, object]:
        return {
            "ok": True,
            **payload,
            "resource": result.resource_key,
            "resource_version": result.resource_version,
        }

    def require_changed(changed: bool, status_code: int, detail: str) -> bool:
        if not changed:
            raise HTTPException(status_code=status_code, detail=detail)
        return True

    def versioned(resource_key: str, payload: dict[str, object]) -> dict[str, object]:
        return {
            **payload,
            "api_version": "v1",
            "resource": resource_key,
            "resource_version": control_store.version(resource_key),
        }

    def alert_notification_control() -> dict[str, object]:
        settings = services.settings
        configured_default = bool(
            getattr(settings, "alert_notify_enabled", False)
        )
        preferences = services.alert_preferences
        override = (
            preferences.enabled_override()
            if preferences is not None
            and callable(getattr(preferences, "enabled_override", None))
            else None
        )
        enabled = (
            preferences.effective_enabled(configured_default)
            if preferences is not None
            and callable(getattr(preferences, "effective_enabled", None))
            else configured_default
        )
        alertmanager_url = str(
            getattr(settings, "alertmanager_url", "") or ""
        )
        group_id = int(getattr(settings, "alert_notify_group_id", 0) or 0)
        return {
            "configured": bool(alertmanager_url and group_id > 0),
            "enabled": bool(enabled),
            "configured_default": configured_default,
            "enabled_override": override,
            "group_id": group_id,
            "check_seconds": int(
                getattr(settings, "alert_notify_check_seconds", 30) or 30
            ),
        }

    @router.get("", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard() -> str:
        return dashboard_html(prefix, services.version)

    @router.get("/assets/{asset_path:path}", include_in_schema=False)
    async def dashboard_asset(asset_path: str) -> FileResponse:
        resolved = admin_asset_path(asset_path)
        if resolved is None:
            raise HTTPException(status_code=404, detail="admin asset not found")
        return FileResponse(
            resolved,
            headers={"Cache-Control": "public, max-age=300"},
        )

    @router.get("/favicon.svg", include_in_schema=False)
    async def favicon() -> Response:
        return Response(
            content=ADMIN_FAVICON_SVG,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @router.get("/api/resource-versions")
    def resource_versions(
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        return {
            "api_version": "v1",
            "persistent": control_store.persistent,
            "versions": control_store.versions(),
        }

    @router.get("/api/local-model")
    def local_model_status(authorization: Optional[str] = Header(default=None)) -> dict[str, object]:
        authorize(authorization)
        runtime = services.local_model
        if runtime is None:
            return versioned("local-model", {"configured": False, "can_control": False})
        snapshot = runtime.snapshot()
        health = services.llm_gateway.health_snapshot().get(runtime.profile.name, {}) if services.llm_gateway else {}
        selected = getattr(services.settings, "model_simple_chat_profile", "") == runtime.profile.name
        can_control = bool(mobile and snapshot["control_configured"])
        return versioned("local-model", {
            **snapshot, "configured": True, "can_control": can_control,
            "control_reason": "" if can_control else "启停需要管理员账户、QQ 口令授权和 WSL 管理接口凭据",
            "simple_chat_selected": selected,
            "serving_simple_chat": bool(selected and snapshot["ready"] and health.get("status") != "open"),
            "circuit_state": health.get("status", "unknown"),
            "circuit_breaker_enabled": runtime.profile.circuit_breaker_enabled,
            "request_count": health.get("request_count", 0),
            "average_latency_ms": health.get("average_latency_ms"),
            "metrics_since": services.started_at,
        })

    @router.post("/api/local-model/control")
    async def local_model_control(
        body: LocalModelControlRequest,
        authorization: Optional[str] = Header(default=None),
        context: AdminMutationContext = Depends(mutation_context),
    ) -> dict[str, object]:
        authorize(authorization)
        if context.expected_version is None:
            raise HTTPException(status_code=428, detail="启停操作必须携带 If-Match 资源版本")
        runtime = services.local_model
        if runtime is None or not runtime.control_configured:
            raise HTTPException(status_code=503, detail="尚未配置 WSL 千问管理接口")
        try:
            result = await asyncio.to_thread(
                mutate, context, "local-model",
                action="local_model." + body.action, target=runtime.profile.name,
                before={"state": runtime.snapshot()["state"]},
                operation=lambda _version: runtime.control(body.action, str(body.request_id)),
            )
        except LocalModelControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        finally:
            event_broker.publish("local-model", "overview", "audit")
        return mutation_payload(result, **result.value)

    @router.get("/api/overview", dependencies=[])
    def overview(
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        deliveries = (
            services.delivery_store.stats()
            if services.delivery_store is not None
            else {}
        )
        tasks = (
            len(services.running_tasks.list_all())
            if services.running_tasks is not None
            else 0
        )
        durable_jobs = (
            services.job_store.stats()
            if services.job_store is not None
            else {}
        )
        subagent_tasks = (
            services.subagent_store.stats()
            if services.subagent_store is not None
            else {}
        )
        return {
            "version": services.version,
            "uptime_seconds": max(int(time.time()) - services.started_at, 0),
            "deliveries": deliveries,
            "running_tasks": tasks,
            "durable_jobs": durable_jobs,
            "subagent_tasks": subagent_tasks,
            "background_tasks": (
                {
                    "running": list(services.background_tasks.running()),
                    "failures": services.background_tasks.failures(),
                }
                if services.background_tasks is not None
                else {"running": [], "failures": {}}
            ),
            "models": _model_overview(
                services.model_catalog,
                services.llm_gateway,
            ),
            "bridges": (
                services.bridge_state.stats()
                if services.bridge_state is not None
                else {}
            ),
            "browser": (
                services.browser_manager.stats()
                if services.browser_manager is not None
                else {"active_sessions": 0, "persistent_profiles": 0}
            ),
        }

    @router.get("/api/events")
    async def events(
        request: Request,
        authorization: Optional[str] = Header(default=None),
    ) -> StreamingResponse:
        authorize(authorization)

        async def stream() -> AsyncIterator[str]:
            queue = event_broker.subscribe()
            realtime_monitor.start()
            ready = {
                "sequence": 0,
                "type": "ready",
                "resources": [],
                "timestamp": int(time.time()),
                "versions": control_store.versions(),
            }
            try:
                yield "retry: 2000\n"
                yield f"data: {json.dumps(ready, separators=(',', ':'))}\n\n"
                while not await request.is_disconnected():
                    try:
                        if mobile is None:
                            break
                        active = await asyncio.to_thread(mobile.store.session, request.cookies.get(SESSION_COOKIE, ""))
                        if active["role"] != "admin":
                            break
                    except SecurityError:
                        break
                    try:
                        payload = await asyncio.wait_for(
                            queue.get(),
                            timeout=15.0,
                        )
                    except TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    encoded = json.dumps(payload, separators=(",", ":"))
                    yield f"data: {encoded}\n\n"
            finally:
                event_broker.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "close",
                "X-Accel-Buffering": "no",
            },
        )

    @router.get("/api/databases")
    def databases(
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.database is None:
            return {
                "available": False,
                "overall": "unconfigured",
                "checked_at": int(time.time()),
                "writable_node": None,
                "nodes": [],
                "pool": {},
            }
        return services.database.topology_snapshot()

    register_fleet_admin_routes(router, services, authorize, versioned, authorize_management)

    @router.get("/api/platforms")
    async def platforms(
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        return {
            "mirrors": (
                services.bridge_router.describe()
                if services.bridge_router is not None
                else []
            ),
            "evidence": (
                services.bridge_state.stats()
                if services.bridge_state is not None
                else {}
            ),
        }

    @router.get("/api/deliveries")
    def deliveries(
        limit: int = Query(default=100, ge=1, le=500),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.delivery_store is None:
            return {"items": [], "configured": False}
        items = []
        for item in services.delivery_store.recent_summaries(limit=limit):
            payload = asdict(item)
            payload["handle"] = item.handle
            items.append(payload)
        return {"items": items, "configured": True}

    @router.post("/api/deliveries/{delivery_id}/retry")
    async def retry_delivery(
        delivery_id: int,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        result = mutate(
            mutation_info,
            "deliveries",
            action="delivery.retry",
            target=f"delivery#{delivery_id}",
            operation=lambda _version: require_changed(
                bool(
                    services.delivery_store is not None
                    and services.delivery_store.requeue(delivery_id)
                ),
                409,
                "delivery cannot be retried from its current state",
            ),
        )
        event_broker.publish("deliveries", "overview")
        return mutation_payload(result, delivery_id=delivery_id)

    @router.post("/api/deliveries/{delivery_id}/cancel")
    async def cancel_delivery(
        delivery_id: int,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        result = mutate(
            mutation_info,
            "deliveries",
            action="delivery.cancel",
            target=f"delivery#{delivery_id}",
            operation=lambda _version: require_changed(
                bool(
                    services.delivery_store is not None
                    and services.delivery_store.cancel(delivery_id)
                ),
                409,
                "delivery cannot be cancelled from its current state",
            ),
        )
        event_broker.publish("deliveries", "overview")
        return mutation_payload(result, delivery_id=delivery_id)

    @router.get("/api/usage")
    def usage(
        days: int = Query(default=14, ge=1, le=365),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.usage_store is None:
            return {"items": [], "configured": False}
        return {
            "items": services.usage_store.daily_summary(days=days),
            "configured": True,
        }

    @router.get("/api/tasks")
    async def tasks(
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.running_tasks is None:
            return {"items": []}
        return {
            "items": [
                {
                    "task_id": item.task_id,
                    "conversation_id": item.conversation_id,
                    "group_id": item.group_id,
                    "user_id": item.user_id,
                    "message_id": item.message_id,
                    "summary": item.summary,
                    "elapsed_seconds": item.elapsed_seconds,
                }
                for item in services.running_tasks.list_all()
            ]
        }

    @router.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(
        task_id: str,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        result = mutate(
            mutation_info,
            "tasks",
            action="task.cancel",
            target=task_id,
            operation=lambda _version: require_changed(
                bool(
                    services.running_tasks is not None
                    and services.running_tasks.cancel_any(task_id) is not None
                ),
                404,
                "task not found",
            ),
        )
        event_broker.publish("tasks", "overview")
        return mutation_payload(result, task_id=task_id)

    @router.get("/api/subagents")
    def subagents(
        limit: int = Query(default=100, ge=1, le=500),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.subagent_store is None:
            return {"items": [], "roles": [], "counts": {}, "configured": False}
        roles = (
            services.subagent_coordinator.manifest()
            if services.subagent_coordinator is not None
            else []
        )
        return {
            "items": [
                {
                    "task_id": item.task_id,
                    "handle": item.handle,
                    "trace_id": item.trace_id,
                    "scope_key": item.scope_key,
                    "conversation_id": item.conversation_id,
                    "requester_user_id": item.requester_user_id,
                    "trigger_message_id": item.trigger_message_id,
                    "objective": item.objective,
                    "status": item.status,
                    "plan": item.plan,
                    "result": item.result,
                    "last_error": item.last_error,
                    "cancel_requested": item.cancel_requested,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                    "finished_at": item.finished_at,
                }
                for item in services.subagent_store.recent(limit=limit)
            ],
            "roles": roles,
            "counts": services.subagent_store.stats(),
            "configured": services.subagent_coordinator is not None,
            "scheduler": services.subagent_coordinator.scheduler.snapshot() if services.subagent_coordinator else {},
            "model_options": [{"name": p.name, "model": p.model, "vision": p.capabilities.vision}
                for p in services.model_catalog.profiles if p.configured and p.capabilities.tools],
        }

    @router.get("/api/subagents/{task_id}")
    def subagent_detail(
        task_id: int,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.subagent_store is None:
            raise HTTPException(status_code=404, detail="Sub-Agent task store is unavailable")
        task = services.subagent_store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Sub-Agent task not found")
        control = services.subagent_store.control(task_id)
        final = services.delivery_store.find_by_key(f"subagent-final:{task_id}:{control['revision']}") if services.delivery_store else None
        from .agent.progress import task_progress
        progress = task_progress(task, services.subagent_store.runs(task_id), services.subagent_store.task_evidence(task_id),
            services.subagent_store.external_calls(task_id), services.subagent_store.deliveries(task_id), final,
            revision=control["revision"])
        return {
            "progress": progress,
            "control": {key: control[key] for key in ("version", "revision", "policy")},
            "background": bool(control["dispatch"]),
            "final_delivery": {"delivery_id": final.delivery_id, "status": final.status, "attempts": final.attempts,
                "updated_at": final.updated_at, "last_error": final.last_error} if final else None,
            "artifact_deliveries": services.subagent_store.deliveries(task_id),
            "external_waits": [{"run_id": item["run_id"], "call_id": item["call_id"],
                "status": item["status"], "remote_path": item["remote_path"], "updated_at": item["updated_at"]}
                for item in services.subagent_store.external_calls(task_id)],
            "task": {
                "task_id": task.task_id,
                "handle": task.handle,
                "trace_id": task.trace_id,
                "scope_key": task.scope_key,
                "conversation_id": task.conversation_id,
                "requester_user_id": task.requester_user_id,
                "trigger_message_id": task.trigger_message_id,
                "objective": task.objective,
                "status": task.status,
                "plan": task.plan,
                "result": task.result,
                "last_error": task.last_error,
                "cancel_requested": task.cancel_requested,
                "created_at": task.created_at,
                "updated_at": task.updated_at,
                "finished_at": task.finished_at,
            },
            "runs": [
                {
                    "run_id": run.run_id,
                    "handle": run.handle,
                    "step_key": run.step_key,
                    "role": run.role,
                    "objective": run.objective,
                    "deliverable": run.deliverable,
                    "dependencies": list(run.dependencies),
                    "allowed_tools": list(run.allowed_tools),
                    "model_profile": run.model_profile,
                    "status": run.status,
                    "attempt": run.attempt,
                    "result": run.result,
                    "last_error": run.last_error,
                    "created_at": run.created_at,
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                }
                for run in services.subagent_store.runs(task_id)
            ],
            "checkpoints": services.subagent_store.checkpoints(task_id),
            "run_contexts": services.subagent_store.run_contexts(task_id),
            "events": services.subagent_store.events(task_id),
        }

    @router.get("/api/subagents/{task_id}/evidence/{evidence_id}")
    def subagent_evidence(task_id: int, evidence_id: str,
                          authorization: Optional[str] = Header(default=None)) -> dict[str, object]:
        authorize(authorization)
        if services.subagent_store is None:
            raise HTTPException(status_code=404, detail="Task evidence unavailable")
        item = next((value for value in services.subagent_store.task_evidence(task_id)
                     if value["evidence_id"] == evidence_id), None)
        if item is None:
            raise HTTPException(status_code=404, detail="Evidence outside current task revision")
        return {key: item[key] for key in ("evidence_id", "task_id", "revision", "run_id", "tool_name",
            "arguments", "payload", "payload_hash", "complete", "recorded_at")}

    @router.put("/api/subagents/{task_id}/models")
    def subagent_models(task_id: int, payload: SubAgentModelUpdate,
        mutation_info: AdminMutationContext = Depends(mutation_context), authorization: Optional[str] = Header(default=None)):
        authorize(authorization)
        def operation(_version):
            if services.subagent_coordinator is None:
                raise HTTPException(503, "Sub-Agent unavailable")
            try:
                return services.subagent_coordinator.configure_models(task_id, payload.policy, payload.expected_version)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        result = mutate(mutation_info, "subagents", action="subagent.models", target=f"task#{task_id}", operation=operation)
        event_broker.publish("subagents", "audit")
        return mutation_payload(result, task_id=task_id)

    @router.post("/api/subagents/{task_id}/revise")
    def subagent_revise(task_id: int, payload: SubAgentRevisionUpdate,
        mutation_info: AdminMutationContext = Depends(mutation_context), authorization: Optional[str] = Header(default=None)):
        authorize(authorization)
        def operation(_version):
            if services.subagent_coordinator is None:
                raise HTTPException(503, "Sub-Agent unavailable")
            task = services.subagent_store.get(task_id)
            if task is None:
                raise HTTPException(404, "Task not found")
            try:
                return services.subagent_coordinator.revise(task_id, scope_key=task.scope_key,
                    requester_user_id=task.requester_user_id, instruction=payload.instruction,
                    step_keys=payload.step_keys, expected_version=payload.expected_version,
                    file_delivery_required=payload.file_delivery_required)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        result = mutate(mutation_info, "subagents", action="subagent.revise", target=f"task#{task_id}", operation=operation)
        event_broker.publish("subagents", "jobs", "audit")
        return mutation_payload(result, task_id=task_id)

    @router.post("/api/subagents/{task_id}/cancel")
    def cancel_subagent_task(
        task_id: int,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        result = mutate(
            mutation_info,
            "subagents",
            action="subagent.cancel",
            target=f"task#{task_id}",
            operation=lambda _version: require_changed(
                bool(
                    services.subagent_coordinator is not None
                    and services.subagent_coordinator.cancel(task_id)
                ),
                409,
                "Sub-Agent task is not cancellable",
            ),
        )
        event_broker.publish("subagents", "tasks", "overview")
        return mutation_payload(result, task_id=task_id)

    @router.get("/api/jobs")
    def durable_jobs(
        limit: int = Query(default=100, ge=1, le=500),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.job_store is None:
            return {"items": [], "counts": {}, "configured": False}
        return {
            "items": [
                {
                    "job_id": item.job_id,
                    "handle": item.handle,
                    "kind": item.kind,
                    "scope_key": item.scope_key,
                    "status": item.status,
                    "priority": item.priority,
                    "attempts": item.attempts,
                    "max_attempts": item.max_attempts,
                    "next_attempt_at": item.next_attempt_at,
                    "lease_owner": item.lease_owner,
                    "lease_until": item.lease_until,
                    "last_error": item.last_error,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                    "finished_at": item.finished_at,
                }
                for item in services.job_store.recent_summaries(limit=limit)
            ],
            "counts": services.job_store.stats(),
            "configured": True,
        }

    @router.post("/api/jobs/{job_id}/cancel")
    def cancel_durable_job(
        job_id: int,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        result = mutate(
            mutation_info,
            "jobs",
            action="job.cancel",
            target=f"job#{job_id}",
            operation=lambda _version: require_changed(
                bool(
                    services.job_worker.cancel(job_id)
                    if services.job_worker is not None
                    else (
                        services.job_store is not None
                        and services.job_store.cancel(job_id)
                    )
                ),
                404,
                "job is not cancellable",
            ),
        )
        event_broker.publish("jobs", "overview")
        return mutation_payload(result, job_id=job_id)

    @router.post("/api/jobs/{job_id}/retry")
    def retry_durable_job(
        job_id: int,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        result = mutate(
            mutation_info,
            "jobs",
            action="job.retry",
            target=f"job#{job_id}",
            operation=lambda _version: require_changed(
                bool(
                    services.job_store is not None
                    and services.job_store.requeue(job_id)
                ),
                404,
                "job is not retryable",
            ),
        )
        event_broker.publish("jobs", "overview")
        return mutation_payload(result, job_id=job_id)

    @router.get("/api/sandboxes")
    async def sandboxes(
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.sandbox_manager is None:
            return versioned("sandboxes", {
                "items": [],
                "active_commands": 0,
                "backend": "none",
                "configured": False,
                "available": False,
            })
        try:
            snapshot = await services.sandbox_manager.admin_snapshot()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return versioned("sandboxes", {
                "items": [],
                "active_commands": 0,
                "backend": getattr(services.sandbox_manager, "backend", "oci"),
                "configured": True,
                "available": False,
                "error": str(exc)[:500],
            })

        tasks_by_conversation: dict[str, list[dict[str, object]]] = {}
        if services.running_tasks is not None:
            for item in services.running_tasks.list_all():
                tasks_by_conversation.setdefault(
                    str(item.conversation_id), []
                ).append(
                    {
                        "task_id": item.task_id,
                        "summary": item.summary,
                        "elapsed_seconds": item.elapsed_seconds,
                    }
                )
        for raw_item in snapshot.get("items", []):
            if not isinstance(raw_item, dict):
                continue
            owner = str(raw_item.get("owner", ""))
            raw_item["agent_tasks"] = tasks_by_conversation.get(owner, [])
        return versioned("sandboxes", {
            **snapshot,
            "configured": True,
            "available": True,
        })

    sandbox_action_lock = asyncio.Lock()

    @router.post("/api/sandboxes/{sandbox_id}/action")
    async def sandbox_action(
        sandbox_id: str,
        body: SandboxActionRequest,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.sandbox_manager is None:
            raise HTTPException(status_code=503, detail="sandbox manager unavailable")
        if mutation_info.expected_version is None:
            raise HTTPException(
                status_code=428,
                detail="sandbox actions require an If-Match resource version",
            )
        if SANDBOX_ID_PATTERN.fullmatch(sandbox_id) is None:
            raise HTTPException(status_code=400, detail="invalid sandbox id")

        async with sandbox_action_lock:
            current_version = control_store.version("sandboxes")
            if mutation_info.expected_version != current_version:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "resource_version_conflict",
                        "resource": "sandboxes",
                        "expected_version": mutation_info.expected_version,
                        "current_version": current_version,
                    },
                )
            snapshot = await services.sandbox_manager.admin_snapshot()
            item = next(
                (
                    raw
                    for raw in snapshot.get("items", [])
                    if isinstance(raw, dict)
                    and str(raw.get("sandbox_id") or "") == sandbox_id
                ),
                None,
            )
            if item is None:
                raise HTTPException(status_code=404, detail="sandbox not found")
            if body.action in {"stop", "destroy"} and item.get("activities"):
                raise HTTPException(
                    status_code=409,
                    detail="sandbox is executing a command",
                )

            owner = str(item.get("owner") or "")
            before = {
                "status": str(item.get("status") or ""),
                "running": bool(item.get("running")),
                "purpose": str(item.get("purpose") or "task"),
                "workspace_size_bytes": int(item.get("workspace_size_bytes") or 0),
                "workspace_file_count": int(item.get("workspace_file_count") or 0),
            }
            try:
                if body.action == "start":
                    await services.sandbox_manager.start_owned(owner, sandbox_id)
                elif body.action == "stop":
                    await services.sandbox_manager.stop_owned(owner, sandbox_id)
                else:
                    await services.sandbox_manager.destroy(owner, sandbox_id)
            except SandboxError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from None

            value = {
                "sandbox_id": sandbox_id,
                "action": body.action,
                "status": "destroyed" if body.action == "destroy" else (
                    "running" if body.action == "start" else "stopped"
                ),
            }
            result = mutate(
                mutation_info,
                "sandboxes",
                action=f"sandbox.{body.action}",
                target=sandbox_id,
                before=before,
                operation=lambda _version: value,
            )
        event_broker.publish("sandboxes", "overview", "audit")
        return mutation_payload(result, **value)

    @router.get("/api/stickers")
    def stickers(
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if not callable(services.sticker_inventory):
            return {
                "counts": {},
                "items": [],
                "configured": False,
                "available": False,
            }
        try:
            inventory = services.sticker_inventory()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return {
                "counts": {},
                "items": [],
                "configured": True,
                "available": False,
                "error": str(exc)[:500],
            }
        return {
            **inventory,
            "configured": True,
            "available": True,
        }

    @router.get("/api/tools")
    def tools(
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        return versioned(
            "tools",
            {
                "items": admin_tool_manifest(),
                "configured": True,
                "available": True,
            },
        )

    @router.put("/api/tools/{tool_name}/enabled")
    def set_tool_permission(
        tool_name: str,
        selection: ToolEnabledRequest,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if tool_name not in TOOL_POLICIES:
            raise HTTPException(status_code=404, detail="工具不存在")

        def update_tool(next_version: int) -> dict[str, object]:
            persisted = control_store.set_tool_override(
                tool_name,
                selection.enabled,
                actor=mutation_info.actor,
                resource_version=next_version,
            )
            set_tool_enabled(tool_name, selection.enabled)
            return persisted

        result = mutate(
            mutation_info,
            "tools",
            action="tool.enabled.set",
            target=tool_name,
            before={"enabled": tool_enabled(tool_name)},
            operation=update_tool,
        )
        event_broker.publish("tools", "overview")
        return mutation_payload(
            result,
            tool_name=tool_name,
            enabled=selection.enabled,
        )

    @router.get("/api/group-models")
    def group_models(
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        return versioned(
            "groups",
            _group_model_overview(
                services.model_catalog,
                services.model_preferences,
                services.reasoning_preferences,
                services.settings,
                services.message_ledger,
                services.user_profiles,
            ),
        )

    @router.put("/api/group-models/{group_id}/default")
    async def set_group_model(
        group_id: int,
        selection: ModelSelectionRequest,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if group_id <= 0:
            raise HTTPException(status_code=422, detail="群号必须是正整数")
        if services.model_preferences is None:
            raise HTTPException(status_code=503, detail="模型偏好存储不可用")
        profile = _admin_model_profile(services.model_catalog, selection.profile)

        def update_group_model(_version: int) -> dict[str, object]:
            _preserve_admin_model_selections(
                services.model_catalog,
                services.model_preferences,
                services.settings,
                group_id,
            )
            if profile is None:
                services.model_preferences.clear_group_default(group_id)
            else:
                services.model_preferences.set_group_default(group_id, profile.name)
            return {"profile": profile.name if profile is not None else None}

        result = mutate(
            mutation_info,
            "groups",
            action="group.model.set",
            target=str(group_id),
            before={"profile": services.model_preferences.get_group_default(group_id)},
            operation=update_group_model,
        )
        event_broker.publish("groups", "overview")
        return mutation_payload(
            result,
            scope="group",
            group_id=group_id,
            profile=profile.name if profile is not None else None,
        )

    @router.put("/api/group-models/{group_id}/enabled")
    async def set_group_enabled(
        group_id: int,
        selection: GroupEnabledRequest,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if group_id <= 0:
            raise HTTPException(status_code=422, detail="群号必须是正整数")
        if services.model_preferences is None:
            raise HTTPException(status_code=503, detail="群状态存储不可用")
        result = mutate(
            mutation_info,
            "groups",
            action="group.enabled.set",
            target=str(group_id),
            before={
                "enabled": services.model_preferences.get_group_enabled_override(
                    group_id
                )
            },
            operation=lambda _version: (
                services.model_preferences.set_group_enabled(
                    group_id,
                    selection.enabled,
                )
                or {"enabled": selection.enabled}
            ),
        )
        event_broker.publish("groups", "overview")
        return mutation_payload(
            result,
            group_id=group_id,
            enabled=selection.enabled,
        )

    @router.put("/api/group-models/{group_id}/vision-auto-describe")
    async def set_group_vision_auto_describe(
        group_id: int,
        selection: GroupEnabledRequest,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if group_id <= 0:
            raise HTTPException(status_code=422, detail="群号必须是正整数")
        if services.model_preferences is None:
            raise HTTPException(status_code=503, detail="群状态存储不可用")
        result = mutate(
            mutation_info,
            "groups",
            action="group.vision-auto-describe.set",
            target=str(group_id),
            before={
                "enabled": (
                    services.model_preferences.get_group_vision_auto_describe_override(
                        group_id
                    )
                )
            },
            operation=lambda _version: (
                services.model_preferences.set_group_vision_auto_describe(
                    group_id,
                    selection.enabled,
                )
                or {"vision_auto_describe": selection.enabled}
            ),
        )
        event_broker.publish("groups", "media", "overview")
        return mutation_payload(
            result,
            group_id=group_id,
            vision_auto_describe=selection.enabled,
        )

    @router.put("/api/group-models/{group_id}/users/{user_id}")
    async def set_group_user_model(
        group_id: int,
        user_id: int,
        selection: ModelSelectionRequest,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if group_id <= 0 or user_id <= 0:
            raise HTTPException(status_code=422, detail="群号和 QQ 号必须是正整数")
        if services.model_preferences is None:
            raise HTTPException(status_code=503, detail="模型偏好存储不可用")
        profile = _admin_model_profile(services.model_catalog, selection.profile)
        conversation_id = f"group:{group_id}:user:{user_id}"

        def update_user_model(_version: int) -> dict[str, object]:
            if profile is None:
                services.model_preferences.clear(conversation_id)
            else:
                services.model_preferences.set(conversation_id, profile.name)
            return {"profile": profile.name if profile is not None else None}

        result = mutate(
            mutation_info,
            "groups",
            action="group.user-model.set",
            target=f"{group_id}:{user_id}",
            before={
                "profile": services.model_preferences.get_explicit(conversation_id)
                if callable(getattr(services.model_preferences, "get_explicit", None))
                else dict(services.model_preferences.items()).get(conversation_id)
            },
            operation=update_user_model,
        )
        event_broker.publish("groups", "overview")
        return mutation_payload(
            result,
            scope="group_user",
            group_id=group_id,
            user_id=user_id,
            profile=profile.name if profile is not None else None,
        )

    @router.put("/api/group-models/{group_id}/reasoning-effort")
    async def set_group_reasoning_effort(
        group_id: int,
        selection: ReasoningEffortRequest,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if group_id <= 0:
            raise HTTPException(status_code=422, detail="群号必须是正整数")
        preferences = services.reasoning_preferences
        if preferences is None:
            raise HTTPException(status_code=503, detail="推理强度存储不可用")
        effort = _admin_reasoning_effort(selection.effort)

        def update_group_effort(_version: int) -> dict[str, object]:
            if effort is None:
                preferences.clear_group_member_default(group_id)
            else:
                preferences.set_group_member_default(group_id, effort)
            return {"effort": effort}

        before = preferences.get_group_member_default(group_id)
        result = mutate(
            mutation_info,
            "groups",
            action="group.reasoning-effort.set",
            target=str(group_id),
            before={"effort": before},
            operation=update_group_effort,
        )
        event_broker.publish("groups", "overview")
        return mutation_payload(
            result,
            scope="group_members",
            group_id=group_id,
            effort=effort,
        )

    @router.put(
        "/api/group-models/{group_id}/users/{user_id}/reasoning-effort"
    )
    async def set_group_user_reasoning_effort(
        group_id: int,
        user_id: int,
        selection: ReasoningEffortRequest,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if group_id <= 0 or user_id <= 0:
            raise HTTPException(status_code=422, detail="群号和 QQ 号必须是正整数")
        preferences = services.reasoning_preferences
        if preferences is None:
            raise HTTPException(status_code=503, detail="推理强度存储不可用")
        effort = _admin_reasoning_effort(selection.effort)
        conversation_id = f"group:{group_id}:user:{user_id}"

        def update_user_effort(_version: int) -> dict[str, object]:
            if effort is None:
                preferences.clear(conversation_id)
            else:
                preferences.set(conversation_id, effort)
            return {"effort": effort}

        result = mutate(
            mutation_info,
            "groups",
            action="group.user-reasoning-effort.set",
            target=f"{group_id}:{user_id}",
            before={
                "effort": _explicit_preference(preferences, conversation_id)
            },
            operation=update_user_effort,
        )
        event_broker.publish("groups", "overview")
        return mutation_payload(
            result,
            scope="group_user",
            group_id=group_id,
            user_id=user_id,
            effort=effort,
        )

    @router.get("/api/media")
    def media(
        limit: int = Query(default=100, ge=1, le=500),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.media_library is None:
            return {
                "counts": {},
                "items": [],
                "jobs": [],
                "vision": {"counts": {}, "jobs": []},
                "configured": False,
                "available": False,
            }
        try:
            snapshot = services.media_library.admin_snapshot(limit=limit)
            vision = (
                services.vision_worker.admin_snapshot(limit=limit)
                if services.vision_worker is not None
                else {"counts": {}, "jobs": []}
            )
            cleanup = (
                services.media_cleanup.preview()
                if services.media_cleanup is not None
                else {
                    "candidate_count": 0,
                    "candidate_bytes": 0,
                    "ordinary_links": 0,
                    "confirmation_token": "",
                    "recent_runs": [],
                }
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return {
                "counts": {},
                "items": [],
                "jobs": [],
                "vision": {"counts": {}, "jobs": []},
                "configured": True,
                "available": False,
                "error": str(exc)[:500],
            }
        return {
            **snapshot,
            "vision": vision,
            "cleanup": cleanup,
            "configured": True,
            "available": True,
            "api_version": "v1",
            "resource": "media",
            "resource_version": control_store.version("media"),
        }

    @router.put("/api/media/{media_id}/review")
    def review_media(
        media_id: int,
        review: MediaReviewRequest,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if media_id <= 0:
            raise HTTPException(status_code=422, detail="媒体编号必须是正整数")
        if review.state not in {"approved", "pending", "rejected"}:
            raise HTTPException(status_code=422, detail="审核状态无效")
        if services.media_library is None or not callable(
            getattr(services.media_library, "set_review_state", None)
        ):
            raise HTTPException(status_code=503, detail="媒体审核服务不可用")
        try:
            result = mutate(
                mutation_info,
                "media",
                action="media.review.set",
                target=f"media#{media_id}",
                before={"requested_state": review.state},
                operation=lambda _version: services.media_library.set_review_state(
                    media_id,
                    review.state,
                ),
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)[:500]) from None
        event_broker.publish("media", "stickers", "overview")
        reviewed = result.value if isinstance(result.value, dict) else {}
        return mutation_payload(result, **reviewed)

    @router.get("/api/sources")
    def sources(
        limit: int = Query(default=100, ge=1, le=500),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.source_store is None:
            return {
                "counts": {},
                "platforms": [],
                "items": [],
                "configured": False,
                "available": False,
            }
        try:
            snapshot = services.source_store.admin_snapshot(limit=limit)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return {
                "counts": {},
                "platforms": [],
                "items": [],
                "configured": True,
                "available": False,
                "error": str(exc)[:500],
            }
        return {
            **snapshot,
            "configured": True,
            "available": True,
        }

    @router.delete("/api/media/legacy-images")
    async def cleanup_legacy_images(
        confirmation: CleanupConfirmationRequest,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.media_cleanup is None:
            raise HTTPException(status_code=503, detail="旧图片清理服务不可用")
        try:
            result = mutate(
                mutation_info,
                "media",
                action="media.legacy-cleanup",
                target="legacy-images",
                operation=lambda _version: services.media_cleanup.apply(
                    confirmation.confirmation_token,
                ),
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)[:500]) from None
        event_broker.publish("media", "overview")
        report = result.value if isinstance(result.value, dict) else {}
        return mutation_payload(result, **report)

    @router.get("/api/context-plans")
    def context_plans(
        limit: int = Query(default=100, ge=1, le=500),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.turn_journal is None:
            return {"items": [], "configured": False, "available": False}
        try:
            items = services.turn_journal.recent_context_plans(limit=limit)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return {
                "items": [],
                "configured": True,
                "available": False,
                "error": str(exc)[:500],
            }
        return {"items": items, "configured": True, "available": True}

    @router.get("/api/context-debug")
    def context_debug(
        limit: int = Query(default=100, ge=1, le=500),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.turn_journal is None:
            return versioned(
                "context-debug",
                {
                    "items": [],
                    "historian": _historian_snapshot(services.job_store),
                    "configured": False,
                    "available": False,
                },
            )
        try:
            plans = services.turn_journal.recent_context_plans(limit=limit)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return versioned(
                "context-debug",
                {
                    "items": [],
                    "historian": _historian_snapshot(services.job_store),
                    "configured": True,
                    "available": False,
                    "error": str(exc)[:500],
                },
            )
        return versioned(
            "context-debug",
            {
                "items": [_context_debug_summary(item) for item in plans],
                "historian": _historian_snapshot(services.job_store),
                "configured": True,
                "available": True,
            },
        )

    @router.get("/api/context-debug/{turn_id}")
    def context_debug_detail(
        turn_id: int,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.turn_journal is None:
            raise HTTPException(status_code=503, detail="上下文日志不可用")
        plans = services.turn_journal.recent_context_plans(limit=500)
        plan = next(
            (item for item in plans if int(item.get("turn_id") or 0) == turn_id),
            None,
        )
        if plan is None:
            raise HTTPException(status_code=404, detail="上下文决策不存在或已隐藏")
        messages = _context_evidence_messages(services.message_ledger, plan)
        return versioned(
            "context-debug",
            {
                "item": _context_debug_detail(plan, messages),
                "historian": _historian_snapshot(services.job_store),
            },
        )

    @router.put("/api/context-debug/{turn_id}/feedback")
    def update_context_feedback(
        turn_id: int,
        feedback: ContextFeedbackRequest,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.turn_journal is None:
            raise HTTPException(status_code=503, detail="上下文日志不可用")
        verdict = feedback.verdict.strip().casefold()
        if verdict not in {"correct", "off_topic"}:
            raise HTTPException(status_code=422, detail="反馈只能是答对了或答非所问")
        plans = services.turn_journal.recent_context_plans(limit=500)
        plan = next(
            (item for item in plans if int(item.get("turn_id") or 0) == turn_id),
            None,
        )
        if plan is None:
            raise HTTPException(status_code=404, detail="上下文决策不存在或已隐藏")
        result = mutate(
            mutation_info,
            "context-debug",
            action="context.feedback.set",
            target=f"turn#{turn_id}",
            before=plan.get("feedback"),
            operation=lambda resource_version: (
                services.turn_journal.set_context_feedback(
                    turn_id,
                    verdict=verdict,
                    note=feedback.note,
                    actor=mutation_info.actor,
                    resource_version=resource_version,
                )
            ),
        )
        event_broker.publish("context-debug", "audit")
        return mutation_payload(result, feedback=result.value)

    @router.get("/api/traces")
    def traces(
        limit: int = Query(default=100, ge=1, le=500),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.turn_journal is None:
            return versioned(
                "traces",
                {"items": [], "configured": False, "available": False},
            )
        try:
            items = services.turn_journal.recent_trace_summaries(limit=limit)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return versioned(
                "traces",
                {
                    "items": [],
                    "configured": True,
                    "available": False,
                    "error": str(exc)[:500],
                },
            )
        return versioned(
            "traces",
            {"items": items, "configured": True, "available": True},
        )

    @router.get("/api/audit")
    def audit_log(
        limit: int = Query(default=200, ge=1, le=500),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        return {
            "api_version": "v1",
            "persistent": control_store.persistent,
            "items": control_store.audit(limit=limit),
            "versions": control_store.versions(),
        }

    @router.get(
        "/api/turn-replay/{scope_kind}/{scope_native_id}/{turn_ordinal}"
    )
    def turn_replay(
        scope_kind: str,
        scope_native_id: str,
        turn_ordinal: int,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.turn_journal is None:
            raise HTTPException(status_code=503, detail="回合日志不可用")
        if scope_kind not in {"group", "private"} or turn_ordinal <= 0:
            raise HTTPException(status_code=422, detail="回放范围或回合号无效")
        try:
            scope = ConversationScope(
                "onebot-v11",
                scope_kind,  # type: ignore[arg-type]
                scope_native_id,
            )
            steps = services.turn_journal.replay_steps(scope, turn_ordinal)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)[:500]) from None
        if not steps:
            raise HTTPException(status_code=404, detail="当前范围看不到这个回合")
        return {
            "scope_key": scope.key,
            "turn_handle": f"t#{turn_ordinal}",
            "mode": "audit-only",
            "steps": list(steps),
        }

    @router.get("/api/observability")
    async def observability(
        limit: int = Query(default=50, ge=1, le=200),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        process: dict[str, object] = {
            "generated_at": int(time.time()),
            "window": "process",
            "totals": {},
            "models": [],
            "tools": [],
            "deliveries": [],
            "stages": [],
            "fallback_routes": [],
        }
        if services.telemetry is not None:
            try:
                process = services.telemetry.admin_snapshot(
                    running_tasks=services.running_tasks,
                    delivery_store=services.delivery_store,
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                pass

        traces: list[dict[str, object]] = []
        traces_available = services.turn_journal is not None
        if traces_available:
            try:
                traces = services.turn_journal.recent_trace_summaries(limit=limit)
            except (OSError, RuntimeError, TypeError, ValueError):
                traces_available = False

        settings = services.settings
        prometheus_url = str(getattr(settings, "prometheus_url", "") or "")
        alertmanager_url = str(getattr(settings, "alertmanager_url", "") or "")
        prometheus, alerts, alert_history = await asyncio.gather(
            _prometheus_health(prometheus_url),
            _alertmanager_alerts(alertmanager_url),
            _alert_history_snapshot(
                services.alert_store,
                days=1,
                limit=limit,
            ),
        )
        return {
            "process": process,
            "prometheus": prometheus,
            "alertmanager": alerts,
            "alert_history": alert_history,
            "traces": {
                "configured": services.turn_journal is not None,
                "available": traces_available,
                "items": traces,
            },
        }

    @router.get("/api/alerts")
    async def alert_history(
        days: int = Query(default=1, ge=1, le=365),
        limit: int = Query(default=200, ge=1, le=500),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        payload = await _alert_history_snapshot(
            services.alert_store,
            days=days,
            limit=limit,
        )
        payload["notification_control"] = alert_notification_control()
        return versioned("alerts", payload)

    @router.put("/api/alert-notifications/control")
    async def set_alert_notification_control(
        selection: AlertNotificationControlRequest,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        preferences = services.alert_preferences
        if preferences is None or not callable(
            getattr(preferences, "set_enabled", None)
        ):
            raise HTTPException(
                status_code=503,
                detail="QQ 告警通知状态存储不可用",
            )
        before = alert_notification_control()

        def update_notification_control(_version: int) -> dict[str, object]:
            preferences.set_enabled(selection.enabled)
            return alert_notification_control()

        result = mutate(
            mutation_info,
            "alerts",
            action="alert-notifications.set",
            target=str(before["group_id"]),
            before=before,
            operation=update_notification_control,
        )
        event_broker.publish("alerts", "observability")
        return mutation_payload(
            result,
            notification_control=result.value,
        )

    legacy_api_prefix = f"{prefix}/api"
    for route in tuple(router.routes):
        if not isinstance(route, APIRoute) or not route.path.startswith(
            f"{legacy_api_prefix}/"
        ):
            continue
        router.add_api_route(
            "/api/v1" + route.path.removeprefix(legacy_api_prefix),
            route.endpoint,
            methods=sorted(route.methods or {"GET"}),
            name=f"v1_{route.name}",
            response_class=route.response_class,
            status_code=route.status_code,
            include_in_schema=True,
        )

    app.include_router(router)
    if mobile is not None:
        register_http_executor(app, mobile, prefix)


def _context_debug_summary(plan: dict[str, Any]) -> dict[str, object]:
    decisions = [
        item
        for item in plan.get("recall_candidates", [])
        if isinstance(item, dict)
    ]
    selected = [item for item in decisions if bool(item.get("selected"))]
    candidate_count = sum(
        int(item.get("omitted_count") or 0)
        if item.get("handle") == "audit#omitted"
        else 1
        for item in decisions
    )
    selected_sources = {str(item.get("source") or "") for item in selected}
    budget = plan.get("adaptive_budget")
    budget = budget if isinstance(budget, dict) else {}
    used = budget.get("used")
    used = used if isinstance(used, dict) else {}
    route = plan.get("recall_route")
    route = route if isinstance(route, dict) else {}
    return {
        "turn_id": int(plan.get("turn_id") or 0),
        "turn_handle": str(plan.get("turn_handle") or ""),
        "scope_key": str(plan.get("scope_key") or ""),
        "created_at": int(plan.get("created_at") or 0),
        "status": str(plan.get("status") or ""),
        "model": str(plan.get("model") or ""),
        "profile": str(plan.get("profile") or ""),
        "current_topic": str(
            plan.get("topic_query") or plan.get("objective") or "未识别话题"
        )[:1000],
        "route": str(
            route.get("effective_mode")
            or route.get("mode")
            or "legacy"
        ),
        "route_confidence": float(route.get("confidence") or 0.0),
        "focus_message_id": plan.get("focus_message_id"),
        "confidence": float(plan.get("confidence") or 0.0),
        "selected_candidates": len(selected),
        "candidate_count": candidate_count,
        "context_tokens": int(used.get("total") or 0),
        "memory_usage": {
            "group": "group_memory" in selected_sources,
            "user": "user_memory" in selected_sources,
        },
        "feedback": plan.get("feedback"),
    }


def _context_debug_detail(
    plan: dict[str, Any],
    messages: list[dict[str, object]],
) -> dict[str, object]:
    summary = _context_debug_summary(plan)
    decisions = [
        dict(item)
        for item in plan.get("recall_candidates", [])
        if isinstance(item, dict)
    ]
    if not decisions:
        focus_id = int(plan.get("focus_message_id") or 0)
        related = {
            int(item)
            for item in plan.get("related_message_ids", [])
            if str(item).isdigit()
        }
        for item in plan.get("candidates", []):
            if not isinstance(item, dict):
                continue
            message_id = int(item.get("message_id") or 0)
            selected = message_id == focus_id or message_id in related
            decisions.append(
                {
                    "handle": f"msg#{message_id}" if message_id else "candidate",
                    "source": item.get("source") or "reference_resolver",
                    "selected": selected,
                    "raw_score": float(item.get("score") or 0.0),
                    "adjusted_score": float(item.get("score") or 0.0),
                    "decision_codes": [
                        "selected_by_reference_resolver"
                        if selected
                        else "not_selected_by_reference_resolver"
                    ],
                    "reason_codes": item.get("reason_codes") or [],
                    "scores": {
                        "lexical": item.get("lexical_score", 0.0),
                        "semantic": item.get("semantic_score", 0.0),
                        "relation": item.get("relation_score", 0.0),
                        "recency": item.get("recency_score", 0.0),
                    },
                    "evidence_ids": [message_id] if message_id else [],
                }
            )
    decisions.sort(
        key=lambda item: (
            bool(item.get("selected")),
            float(item.get("adjusted_score") or 0.0),
        ),
        reverse=True,
    )
    budget = plan.get("adaptive_budget")
    budget = budget if isinstance(budget, dict) else {}
    selected_sources = {
        str(item.get("source") or "")
        for item in decisions
        if bool(item.get("selected"))
    }
    return {
        **summary,
        "objective": str(plan.get("objective") or ""),
        "current_message_id": int(plan.get("current_message_id") or 0),
        "current_principal_id": plan.get("current_principal_id"),
        "topic_id": plan.get("topic_id"),
        "topic_message_ids": plan.get("topic_message_ids") or [],
        "reason_codes": plan.get("reason_codes") or [],
        "resolver_version": str(plan.get("resolver_version") or ""),
        "context_hash": str(plan.get("context_hash") or ""),
        "recall_route": plan.get("recall_route") or {},
        "token_budget": {
            key: value
            for key, value in budget.items()
            if key != "used"
        },
        "token_usage": budget.get("used") or {},
        "evidence_guard": plan.get("evidence_guard") or {},
        "memory_usage": {
            "group": {
                "used": "group_memory" in selected_sources,
                "handles": [
                    item.get("handle")
                    for item in decisions
                    if item.get("selected")
                    and item.get("source") == "group_memory"
                ],
            },
            "user": {
                "used": "user_memory" in selected_sources,
                "handles": [
                    item.get("handle")
                    for item in decisions
                    if item.get("selected")
                    and item.get("source") == "user_memory"
                ],
            },
        },
        "evidence_messages": messages,
        "candidates": decisions,
        "feedback": plan.get("feedback"),
    }


def _context_evidence_messages(
    ledger: Any,
    plan: dict[str, Any],
) -> list[dict[str, object]]:
    if ledger is None or not callable(getattr(ledger, "visible_messages_by_ids", None)):
        return []
    scope_key = str(plan.get("scope_key") or "")
    parts = scope_key.split(":", 2)
    if len(parts) != 3 or parts[1] not in {"group", "private"}:
        return []
    selected_ids = {
        int(item)
        for item in (
            plan.get("current_message_id"),
            plan.get("focus_message_id"),
            *(plan.get("related_message_ids") or []),
            *(plan.get("topic_message_ids") or []),
        )
        if item is not None and str(item).isdigit() and int(item) > 0
    }
    for candidate in plan.get("recall_candidates", []):
        if not isinstance(candidate, dict) or not candidate.get("selected"):
            continue
        selected_ids.update(
            int(item)
            for item in candidate.get("evidence_ids", [])
            if str(item).isdigit() and int(item) > 0
        )
    try:
        scope = ConversationScope(parts[0], parts[1], parts[2])
        records = ledger.visible_messages_by_ids(scope, tuple(sorted(selected_ids)))
    except (OSError, RuntimeError, TypeError, ValueError):
        return []
    return [
        {
            "message_id": int(item.canonical_message_id),
            "native_message_id": str(item.native_message_id),
            "sender_user_id": str(item.sender_native_user_id),
            "sender_principal_id": item.sender_principal_id,
            "sender_display": str(item.sender_display),
            "direction": str(item.direction),
            "text": str(item.prompt_text)[:4000],
            "occurred_at": int(item.occurred_at),
            "reply_to_message_id": item.reply_to_canonical_message_id,
            "roles": [
                role
                for role, message_id in (
                    ("current", plan.get("current_message_id")),
                    ("focus", plan.get("focus_message_id")),
                )
                if message_id is not None
                and int(message_id) == int(item.canonical_message_id)
            ],
        }
        for item in records
    ]


def _historian_snapshot(job_store: Any) -> dict[str, object]:
    if job_store is None or not callable(getattr(job_store, "recent_summaries", None)):
        return {
            "configured": False,
            "backlog": 0,
            "pending": 0,
            "running": 0,
            "retrying": 0,
            "failed": 0,
            "items": [],
        }
    try:
        jobs = [
            item
            for item in job_store.recent_summaries(limit=500)
            if str(getattr(item, "kind", "")) == "context.historian_capture"
        ]
    except (OSError, RuntimeError, TypeError, ValueError):
        return {
            "configured": True,
            "available": False,
            "backlog": 0,
            "pending": 0,
            "running": 0,
            "retrying": 0,
            "failed": 0,
            "items": [],
        }
    pending = [item for item in jobs if item.status == "pending"]
    running = [item for item in jobs if item.status == "running"]
    retrying = [item for item in pending if int(item.attempts) > 0]
    failed = [item for item in jobs if item.status == "failed"]
    noteworthy = [
        item
        for item in jobs
        if item.status in {"pending", "running", "failed"}
        or int(item.attempts) > 1
    ][:20]
    return {
        "configured": True,
        "available": True,
        "backlog": len(pending) + len(running),
        "pending": len(pending),
        "running": len(running),
        "retrying": len(retrying),
        "failed": len(failed),
        "items": [
            {
                "job_id": int(item.job_id),
                "handle": str(item.handle),
                "scope_key": str(item.scope_key),
                "status": str(item.status),
                "attempts": int(item.attempts),
                "max_attempts": int(item.max_attempts),
                "next_attempt_at": item.next_attempt_at,
                "last_error": str(item.last_error)[:500],
                "updated_at": int(item.updated_at),
            }
            for item in noteworthy
        ],
    }


async def _prometheus_health(base_url: str) -> dict[str, object]:
    if not base_url:
        return {"configured": False, "available": False, "up": None}
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(
                f"{base_url.rstrip('/')}/api/v1/query",
                params={"query": 'up{job="gaoji"}'},
            )
            response.raise_for_status()
            payload = response.json()
        result = payload.get("data", {}).get("result", [])
        values = [
            float(item.get("value", [0, 0])[1])
            for item in result
            if isinstance(item, dict)
        ]
        return {
            "configured": True,
            "available": True,
            "up": bool(values) and all(value >= 1 for value in values),
            "targets": len(values),
            "checked_at": int(time.time()),
        }
    except (httpx.HTTPError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "configured": True,
            "available": False,
            "up": None,
            "error": str(exc)[:240],
            "checked_at": int(time.time()),
        }


async def _alertmanager_alerts(base_url: str) -> dict[str, object]:
    if not base_url:
        return {
            "configured": False,
            "available": False,
            "active_count": 0,
            "items": [],
        }
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(
                f"{base_url.rstrip('/')}/api/v2/alerts",
                params={
                    "active": "true",
                    "silenced": "false",
                    "inhibited": "false",
                },
            )
            response.raise_for_status()
            payload = response.json()
        raw_alerts = payload if isinstance(payload, list) else []
        items = [_safe_alert(item) for item in raw_alerts if isinstance(item, dict)]
        return {
            "configured": True,
            "available": True,
            "active_count": len(items),
            "items": items[:100],
            "checked_at": int(time.time()),
        }
    except (httpx.HTTPError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "configured": True,
            "available": False,
            "active_count": 0,
            "items": [],
            "error": str(exc)[:240],
            "checked_at": int(time.time()),
        }


async def _alert_history_snapshot(
    store: Any,
    *,
    days: int,
    limit: int,
) -> dict[str, object]:
    if store is None or not callable(getattr(store, "snapshot", None)):
        return {
            "configured": False,
            "available": False,
            "summary": {
                "current_active": 0,
                "current_incidents": 0,
                "triggered": 0,
                "resolved": 0,
                "incidents": 0,
                "firing_notifications": 0,
                "recovery_notifications": 0,
            },
            "events": [],
            "incidents": [],
            "notifications": [],
        }
    try:
        return await asyncio.to_thread(store.snapshot, days=days, limit=limit)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return {
            "configured": True,
            "available": False,
            "error": str(exc)[:240],
            "summary": {},
            "events": [],
            "incidents": [],
            "notifications": [],
        }


def _safe_alert(raw: dict[str, Any]) -> dict[str, object]:
    labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
    annotations = (
        raw.get("annotations") if isinstance(raw.get("annotations"), dict) else {}
    )
    status = raw.get("status") if isinstance(raw.get("status"), dict) else {}
    return {
        "name": str(labels.get("alertname") or "Alert")[:160],
        "severity": str(labels.get("severity") or "warning")[:32],
        "instance": str(labels.get("instance") or "")[:160],
        "job": str(labels.get("job") or "")[:120],
        "state": str(status.get("state") or "active")[:32],
        "summary": str(annotations.get("summary") or "")[:300],
        "description": str(annotations.get("description") or "")[:500],
        "starts_at": str(raw.get("startsAt") or "")[:64],
        "ends_at": str(raw.get("endsAt") or "")[:64],
        "fingerprint": str(raw.get("fingerprint") or "")[:128],
    }


def _model_overview(
    catalog: Any,
    gateway: Any = None,
) -> dict[str, object]:
    if catalog is None:
        return {"default": "", "profiles": []}
    health = (
        gateway.health_snapshot()
        if gateway is not None and hasattr(gateway, "health_snapshot")
        else {}
    )
    return {
        "default": str(catalog.default_name),
        "profiles": [
            {
                "name": profile.name,
                "provider": profile.provider,
                "protocol": profile.protocol,
                "model": profile.model,
                "configured": profile.configured,
                "reasoning_effort": profile.reasoning_effort or None,
                "supports_reasoning_effort": profile.provider
                in {"openai", "cliproxy"},
                "fallback_profiles": list(profile.fallback_profiles),
                "circuit_breaker_enabled": profile.circuit_breaker_enabled,
                "health": health.get(
                    profile.name,
                    {
                        "status": "unknown",
                        "consecutive_failures": 0,
                        "retry_after_seconds": 0,
                    },
                ),
                "capabilities": {
                    "tools": profile.capabilities.tools,
                    "forced_tool_choice": profile.capabilities.forced_tool_choice,
                    "streaming": profile.capabilities.streaming,
                    "json_mode": profile.capabilities.json_mode,
                    "vision": profile.capabilities.vision,
                },
            }
            for profile in catalog.profiles
        ],
    }


def _admin_model_profile(catalog: Any, requested: str | None) -> Any:
    normalized = str(requested or "").strip()
    if not normalized:
        return None
    if catalog is None:
        raise HTTPException(status_code=503, detail="模型目录不可用")
    try:
        profile = catalog.resolve(normalized)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="没有这个模型配置") from None
    if not profile.configured:
        raise HTTPException(status_code=409, detail="这个模型还没有配置可用的密钥")
    return profile


def _admin_reasoning_effort(requested: str | None) -> str | None:
    normalized = str(requested or "").strip().lower()
    if not normalized:
        return None
    if normalized not in SUPPORTED_REASONING_EFFORTS:
        raise HTTPException(status_code=422, detail="没有这个推理强度")
    return normalized


def _explicit_preference(preferences: Any, key: str) -> str | None:
    getter = getattr(preferences, "get_explicit", None)
    if callable(getter):
        return getter(key)
    return dict(preferences.items()).get(key)


def _preserve_admin_model_selections(
    catalog: Any,
    preferences: Any,
    settings: Any,
    group_id: int,
) -> None:
    """Keep the admin lane independent when the shared member model changes."""
    admin_user_ids = set(
        getattr(settings, "admin_user_ids", set()) or set()
    )
    if not admin_user_ids or catalog is None:
        return

    dynamic_default = (
        preferences.get_group_default(group_id)
        if hasattr(preferences, "get_group_default")
        else None
    )
    deployed_default = (
        (getattr(settings, "group_model_profiles", {}) or {}).get(group_id)
        if settings is not None
        else None
    )
    effective_default = catalog.resolve_preference(
        dynamic_default or deployed_default
    )
    stored_keys = {
        str(conversation_id)
        for conversation_id, _stored in preferences.items()
    }
    for user_id in admin_user_ids:
        conversation_id = f"group:{group_id}:user:{int(user_id)}"
        if conversation_id not in stored_keys:
            preferences.set(conversation_id, effective_default.name)


_GROUP_CONVERSATION_PATTERN = re.compile(r"^group:(\d+):user:(\d+)$")
_GROUP_SETTING_PATTERN = re.compile(
    r"^group:(\d+):(?:default|enabled|vision-auto-describe)$"
)
_GROUP_MEMBER_REASONING_PATTERN = re.compile(
    r"^group:(\d+):member-default$"
)


def _group_model_overview(
    catalog: Any,
    preferences: Any,
    reasoning_preferences: Any,
    settings: Any,
    message_ledger: Any,
    user_profiles: Any = None,
) -> dict[str, object]:
    if catalog is None:
        return {"default": {}, "items": [], "configured": False}

    default_profile = catalog.default
    group_ids: set[int] = set()
    if settings is not None:
        group_ids.update(getattr(settings, "enabled_groups", set()) or set())
        group_ids.update(getattr(settings, "disabled_groups", set()) or set())
        group_ids.update(
            (getattr(settings, "group_model_profiles", {}) or {}).keys()
        )

    if message_ledger is not None:
        try:
            for scope in message_ledger.list_scopes():
                if scope.kind != "group" or scope.platform != "onebot-v11":
                    continue
                try:
                    group_ids.add(int(scope.native_conversation_id))
                except (TypeError, ValueError):
                    continue
        except (OSError, RuntimeError, TypeError, ValueError):
            pass

    if user_profiles is not None:
        try:
            group_ids.update(user_profiles.group_ids())
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            pass

    overrides_by_group: dict[int, dict[int, dict[str, object]]] = {}
    preference_items = preferences.items() if preferences is not None else []
    for conversation_id, stored_preference in preference_items:
        match = _GROUP_CONVERSATION_PATTERN.fullmatch(str(conversation_id))
        if match is None:
            setting_match = _GROUP_SETTING_PATTERN.fullmatch(str(conversation_id))
            if setting_match is not None:
                group_ids.add(int(setting_match.group(1)))
            continue
        group_id = int(match.group(1))
        user_id = int(match.group(2))
        group_ids.add(group_id)
        resolved = catalog.resolve_preference(str(stored_preference))
        direct = catalog.try_resolve(str(stored_preference))
        recognized = direct is not None or any(
            profile.model == str(stored_preference)
            for profile in catalog.profiles
        )
        overrides_by_group.setdefault(group_id, {})[user_id] = {
            "user_id": user_id,
            "stored_preference": str(stored_preference),
            "profile": resolved.name,
            "provider": resolved.provider,
            "model": resolved.model,
            "recognized": recognized,
        }

    reasoning_by_group: dict[int, dict[int, str]] = {}
    member_effort_by_group: dict[int, str] = {}
    reasoning_items = (
        reasoning_preferences.items()
        if reasoning_preferences is not None
        else []
    )
    for conversation_id, stored_effort in reasoning_items:
        normalized_effort = str(stored_effort).strip().lower()
        if normalized_effort not in SUPPORTED_REASONING_EFFORTS:
            continue
        match = _GROUP_CONVERSATION_PATTERN.fullmatch(str(conversation_id))
        if match is not None:
            group_id = int(match.group(1))
            user_id = int(match.group(2))
            group_ids.add(group_id)
            reasoning_by_group.setdefault(group_id, {})[user_id] = (
                normalized_effort
            )
            continue
        default_match = _GROUP_MEMBER_REASONING_PATTERN.fullmatch(
            str(conversation_id)
        )
        if default_match is not None:
            group_id = int(default_match.group(1))
            group_ids.add(group_id)
            member_effort_by_group[group_id] = normalized_effort

    rows: list[dict[str, object]] = []
    admin_user_ids = set(
        getattr(settings, "admin_user_ids", set()) or set()
    )
    for group_id in sorted(group_ids):
        overrides = sorted(
            overrides_by_group.get(group_id, {}).values(),
            key=lambda item: int(item["user_id"]),
        )
        deployed_enabled = (
            bool(settings.is_group_enabled(group_id))
            if settings is not None
            else True
        )
        enabled_override = None
        if preferences is not None and hasattr(
            preferences,
            "get_group_enabled_override",
        ):
            enabled_override = preferences.get_group_enabled_override(group_id)
        enabled = (
            enabled_override
            if enabled_override is not None
            else deployed_enabled
        )
        vision_override = None
        if preferences is not None and hasattr(
            preferences,
            "get_group_vision_auto_describe_override",
        ):
            vision_override = preferences.get_group_vision_auto_describe_override(
                group_id
            )
        vision_deployed = bool(
            getattr(settings, "vision_auto_describe", False)
            if settings is not None
            else False
        )
        vision_auto_describe = (
            vision_override if vision_override is not None else vision_deployed
        )
        deployed_group_default = (
            (getattr(settings, "group_model_profiles", {}) or {}).get(group_id)
            if settings is not None
            else None
        )
        dynamic_group_default = None
        if preferences is not None and hasattr(preferences, "get_group_default"):
            dynamic_group_default = preferences.get_group_default(group_id)
        stored_group_default = dynamic_group_default or deployed_group_default
        group_default = catalog.resolve_preference(stored_group_default)
        member_reasoning_effort = member_effort_by_group.get(group_id)

        observed_members: list[dict[str, object]] = []
        if user_profiles is not None:
            try:
                observed_members = user_profiles.members(group_id)
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                observed_members = []
        members_by_id = {
            int(item["user_id"]): dict(item)
            for item in observed_members
            if int(item.get("user_id", 0)) > 0
        }
        for user_id in {
            *admin_user_ids,
            *overrides_by_group.get(group_id, {}),
            *reasoning_by_group.get(group_id, {}),
        }:
            members_by_id.setdefault(
                int(user_id),
                {
                    "user_id": int(user_id),
                    "nickname": "",
                    "card": "",
                    "display_name": f"QQ {int(user_id)}",
                    "last_seen": 0,
                },
            )

        classified: list[dict[str, object]] = []
        for user_id, member in members_by_id.items():
            explicit = overrides_by_group.get(group_id, {}).get(user_id)
            effective = (
                catalog.resolve_preference(str(explicit["stored_preference"]))
                if explicit is not None
                else group_default
            )
            supports_reasoning_effort = effective.provider in {
                "openai",
                "cliproxy",
            }
            explicit_effort = reasoning_by_group.get(group_id, {}).get(user_id)
            inherited_effort = (
                None
                if user_id in admin_user_ids
                else member_reasoning_effort
            )
            effective_effort = (
                explicit_effort
                or inherited_effort
                or effective.reasoning_effort
                or None
            )
            effort_source = (
                "unsupported"
                if not supports_reasoning_effort
                else "user"
                if explicit_effort is not None
                else "group"
                if inherited_effort is not None
                else "model"
                if effective.reasoning_effort
                else "server"
            )
            classified.append(
                {
                    **member,
                    "is_admin": user_id in admin_user_ids,
                    "explicit_profile": (
                        str(explicit["profile"]) if explicit is not None else None
                    ),
                    "effective_profile": effective.name,
                    "effective_provider": effective.provider,
                    "effective_model": effective.model,
                    "supports_reasoning_effort": supports_reasoning_effort,
                    "explicit_reasoning_effort": explicit_effort,
                    "effective_reasoning_effort": (
                        effective_effort if supports_reasoning_effort else None
                    ),
                    "reasoning_effort_source": effort_source,
                }
            )
        classified.sort(
            key=lambda item: (
                not bool(item["is_admin"]),
                -int(item.get("last_seen", 0)),
                int(item["user_id"]),
            )
        )
        rows.append(
            {
                "group_id": group_id,
                "enabled": enabled,
                "enabled_override": enabled_override,
                "enabled_source": (
                    "dashboard" if enabled_override is not None else "deployment"
                ),
                "vision_auto_describe": vision_auto_describe,
                "vision_auto_describe_override": vision_override,
                "vision_auto_describe_source": (
                    "dashboard" if vision_override is not None else "deployment"
                ),
                "default_profile": group_default.name,
                "default_provider": group_default.provider,
                "default_model": group_default.model,
                "group_override": stored_group_default is not None,
                "dynamic_group_profile": dynamic_group_default,
                "deployed_group_profile": deployed_group_default,
                "group_default_source": (
                    "dashboard"
                    if dynamic_group_default is not None
                    else "deployment"
                    if deployed_group_default is not None
                    else "global"
                ),
                "member_reasoning_effort": member_reasoning_effort,
                "overrides": overrides,
                "admins": [item for item in classified if item["is_admin"]],
                "members": [item for item in classified if not item["is_admin"]],
            }
        )

    return {
        "default": {
            "profile": default_profile.name,
            "provider": default_profile.provider,
            "model": default_profile.model,
        },
        "profiles": _model_overview(catalog)["profiles"],
        "items": rows,
        "configured": True,
    }
