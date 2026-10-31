from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable

from .contracts import AdminServices


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
    "qq_official_accounts": ("qq-official",),
    "qq_official_endpoints": ("qq-official",),
    "qq_official_admin_bindings": ("qq-official",),
    "qq_official_owner_grants": ("qq-official",),
    "delivery_receipts": ("qq-official",),
    "admin_accounts": ("qq-official",),
    "qq_official_inbound_events": ("qq-official",),
    "qq_official_turn_executions": ("qq-official",),
    "qq_official_file_deliveries": ("qq-official",),
    "qq_official_reply_windows": ("qq-official",),
    "qq_official_admin_identity_challenges": ("qq-official",),
    "migration_routes": ("qq-official", "migrations"),
    "conversations": ("groups",),
    "principals": ("groups",),
    "principal_identities": ("groups",),
    "messages": ("groups"),
    "conversation_visibility": ("wiki"),
    "context_compartments": ("wiki"),
    "background_jobs": ("qq-official", "backgroundAgents", "tasks", "overview"),
    "deliveries": ("qq-official", "deliveries", "backgroundAgents", "overview"),
    "delivery_attempts": ("qq-official", "deliveries", "backgroundAgents", "overview"),
    "usage_events": ("usage", "overview"),
    "agent_turns": ("traces"),
    "turn_journal_events": ("traces"),
    "turn_context_plans": ("traces"),
    "turn_edges": ("traces"),
    "turn_digests": ("traces"),
    "durable_jobs": ("jobs", "media", "overview"),
    "media_blobs": ("media", "stickers"),
    "message_media": ("media", "stickers"),
    "media_jobs": ("media",),
    "vision_jobs": ("media",),
    "media_cleanup_runs": ("media",),
    "content_sources": ("sources",),
    "message_sources": ("sources",),
    "alert_events": ("alerts"),
    "alert_notifications": ("alerts"),
    "diagnostic_runs": ("fleet"),
    "diagnostic_evidence": ("fleet"),
    "bridge_sources": ("overview",),
    "bridge_deliveries": ("overview",),
    "bridge_cursors": ("overview",),
    "fleet_backend_states": ("overview"),
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
        official_signature = None
        first = True
        next_process = 0.0
        next_external = 0.0
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

                getter = self.services.official_observability
                try:
                    service = getter() if callable(getter) else None
                    official = service.snapshot() if service is not None else {}
                except Exception:
                    official = {"healthy": False}
                import json
                official.pop('observed_at', None)
                if isinstance(official.get('lease'), dict):official['lease'].pop('checked_at', None)
                current_official = json.dumps(official, sort_keys=True, separators=(',', ':'))
                if official_signature is not None and current_official != official_signature:
                    changed.add('qq-official')
                official_signature = current_official
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
                            "deliveries",
                            "usage",
                            "tasks",
                            "backgroundAgents",
                            "jobs",
                            "sandboxes",
                            "stickers",
                            "media",
                            "sources",
                            "databases",
                            "groups",
                            "tools",
                            "traces",
                            "audit",
                        )
                    )
                    first = False
                if now >= next_process:
                    changed.update(("overview", "sandboxes", "local-model"))
                    next_process = now + 2.0
                if now >= next_external:
                    changed.update(("databases", "stickers"))
                    next_external = now + 5.0
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
