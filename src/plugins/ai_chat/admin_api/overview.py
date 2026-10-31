from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Header

from .context import AdminRouteContext, authorize
from .model_views import _model_overview


def create_router(context: AdminRouteContext) -> APIRouter:
    router = context.new_router()
    services = context.services
    control_store = context.control_store

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
        }

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

    return router
