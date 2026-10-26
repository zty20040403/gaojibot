from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException

from ..tool_policy import (
    TOOL_POLICIES,
    admin_tool_manifest,
    set_tool_enabled,
    tool_enabled,
)
from .context import AdminRouteContext, authorize, mutation_context, mutation_payload
from .contracts import AdminMutationContext, ToolEnabledRequest


def create_router(context: AdminRouteContext) -> APIRouter:
    router = context.new_router()
    control_store = context.control_store
    event_broker = context.event_broker
    mutate = context.mutate
    versioned = context.versioned

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

    return router
