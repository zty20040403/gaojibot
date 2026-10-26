from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException

from ..sandbox import SANDBOX_ID_PATTERN, SandboxError
from .context import AdminRouteContext, authorize, mutation_context, mutation_payload
from .contracts import AdminMutationContext, SandboxActionRequest


def create_router(context: AdminRouteContext) -> APIRouter:
    router = context.new_router()
    services = context.services
    control_store = context.control_store
    event_broker = context.event_broker
    mutate = context.mutate
    versioned = context.versioned
    sandbox_action_lock = asyncio.Lock()

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

    return router
