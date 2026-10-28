"""Assemble the protected admin API; page handlers live in admin_api."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.routing import APIRoute

from .admin_api import (
    audit,
    context_debug,
    dashboard,
    events,
    fleet,
    media,
    mcp,
    models,
    observability,
    overview,
    projects,
    sandboxes,
    subagents,
    tasks,
    tools,
    wiki,
)
from .admin_api.context import AdminRouteContext
from .admin_api.contracts import AdminServices
from .admin_api.realtime import (
    AdminEventBroker,
    AdminRealtimeMonitor,
)
from .admin_api.realtime import (
    _changed_database_resources as _changed_database_resources,
)
from .admin_control import AdminControlStore
from .admin_security import (
    register_http_executor,
    register_security_routes,
    secure_route,
)
from .tool_policy import configure_tool_overrides

__all__ = ["AdminServices", "AdminEventBroker", "AdminRealtimeMonitor", "register_admin"]


_PAGE_ROUTERS = (
    dashboard,
    overview,
    models,
    tasks,
    subagents,
    sandboxes,
    tools,
    media,
    context_debug,
    audit,
    observability,
    events,
    fleet,
    wiki,
    mcp,
    projects,
)


def register_admin(
    app: Any,
    services: AdminServices,
    *,
    path: str = "/bot-admin",
    token: str = "",  # Obsolete: never reopens token login.
) -> None:
    prefix = "/" + path.strip("/")
    mobile = services.mobile_authorization
    origin = str(getattr(services.settings, "admin_origin", "") or "")
    route_class = secure_route(mobile, prefix=prefix, origin=origin)
    control_store = AdminControlStore(services.database)
    configure_tool_overrides(control_store.tool_overrides())
    event_broker = AdminEventBroker(control_store.versions)
    if services.subagent_store is not None:
        services.subagent_store.set_change_listener(
            lambda _task_id: event_broker.publish_runtime("subagents", "tasks", "overview")
        )
    context = AdminRouteContext(
        services=services,
        prefix=prefix,
        route_class=route_class,
        control_store=control_store,
        event_broker=event_broker,
        realtime_monitor=AdminRealtimeMonitor(services, event_broker),
    )
    router = APIRouter(route_class=route_class)
    security_router = context.new_router()
    register_security_routes(security_router, mobile, services, prefix=prefix, origin=origin)
    legacy_api_prefix = f"{prefix}/api"
    pages = [security_router, *(page.create_router(context) for page in _PAGE_ROUTERS)]
    for page_router in pages:
        # Register aliases before inclusion, which FastAPI may defer lazily.
        for route in tuple(page_router.routes):
            if not isinstance(route, APIRoute) or not route.path.startswith(
                f"{legacy_api_prefix}/"
            ):
                continue
            page_router.add_api_route(
                "/api/v1" + route.path.removeprefix(legacy_api_prefix),
                route.endpoint,
                methods=sorted(route.methods or {"GET"}),
                name=f"v1_{route.name}",
                response_class=route.response_class,
                status_code=route.status_code,
                include_in_schema=True,
            )
        router.include_router(page_router)

    app.include_router(router)
    if mobile is not None:
        register_http_executor(app, mobile, prefix)
