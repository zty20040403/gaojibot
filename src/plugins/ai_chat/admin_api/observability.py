from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query

from .context import AdminRouteContext, authorize, mutation_context, mutation_payload
from .contracts import AdminMutationContext, AlertNotificationControlRequest


def create_router(context: AdminRouteContext) -> APIRouter:
    router = context.new_router()
    services = context.services
    event_broker = context.event_broker
    mutate = context.mutate
    versioned = context.versioned

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

    return router


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
