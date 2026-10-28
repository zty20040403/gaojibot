"""Admin-only project metadata controls, separate from member workbench access."""

import asyncio
from typing import Literal

from fastapi import Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ..admin_control import AdminVersionConflict
from ..project_admin import ProjectAdmin
from ..projects import ProjectConflict, ProjectQuotaExceeded
from .context import authorize, mutation_context, mutation_payload
from .contracts import AdminMutationContext


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(ge=1)


class Pin(Revision):
    pinned: StrictBool


class Quota(Revision):
    quota_bytes: int = Field(ge=64 * 1024**2, le=100 * 1024**4, multiple_of=1024**2)


class ScopeLimits(Quota):
    scope_version: int = Field(ge=1)
    max_projects: int = Field(ge=1, le=1000)


def create_router(context):
    router = context.new_router()
    runtime = context.services.project_runtime

    def store():
        if runtime is None or not runtime.ready or runtime.closed or runtime.store is None:
            raise HTTPException(status_code=503, detail="项目运行环境尚未就绪")
        return ProjectAdmin(runtime.store, context.control_store)

    async def read(method, *args, **kwargs):
        try:
            return await asyncio.to_thread(method, *args, **kwargs)
        except LookupError:
            raise HTTPException(status_code=404, detail="项目不存在或已清除") from None

    @router.get("/api/projects")
    async def listing(query: str = Query(default="", max_length=200),
                      state: Literal["", "active", "stopped", "hibernated", "archived", "trash"] = "",
                      scope: str = Query(default="", max_length=300),
                      offset: int = Query(default=0, ge=0), limit: int = Query(default=5, ge=1, le=100)):
        authorize()
        if runtime is None or not runtime.ready or runtime.closed:
            return context.versioned("projects", {"configured": runtime is not None, "available": False,
                "items": [], "total": 0, "counts": {}, "offset": offset, "limit": limit})
        return {**await read(store().list, query=query, state=state, scope=scope, offset=offset, limit=limit),
                "configured": True, "available": True}

    @router.get("/api/projects/{project_id}")
    async def detail(project_id: int = Path(ge=1), section: Literal["versions", "previews", "events"] = "versions",
                     offset: int = Query(default=0, ge=0), limit: int = Query(default=5, ge=1, le=100)):
        authorize()
        return await read(store().detail, project_id, section=section, offset=offset, limit=limit)

    async def change(project_id, action, body, mutation, **extra):
        authorize()
        if mutation.expected_version is None:
            raise HTTPException(status_code=428, detail="修改项目必须携带 If-Match 资源版本")
        values = body.model_dump(exclude={"expected_version"}) | extra
        try:
            result = await asyncio.to_thread(store().mutate, project_id, action=action,
                expected_version=body.expected_version, resource_version=mutation.expected_version,
                actor=mutation.actor, values=values)
        except AdminVersionConflict as exc:
            raise HTTPException(status_code=409, detail={"code": "resource_version_conflict",
                "resource": exc.resource_key, "expected_version": exc.expected, "current_version": exc.current}) from None
        except (ProjectConflict, ProjectQuotaExceeded) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        await asyncio.to_thread(context.event_broker.publish, "projects", "audit")
        return mutation_payload(result, change=result.value)

    @router.put("/api/projects/{project_id}/pin")
    async def pin(body: Pin, project_id: int = Path(ge=1), mutation: AdminMutationContext = Depends(mutation_context)):
        return await change(project_id, "pin", body, mutation)

    @router.put("/api/projects/{project_id}/quota")
    async def quota(body: Quota, project_id: int = Path(ge=1), mutation: AdminMutationContext = Depends(mutation_context)):
        return await change(project_id, "quota", body, mutation)

    @router.put("/api/projects/{project_id}/scope-limits")
    async def scope_limits(body: ScopeLimits, project_id: int = Path(ge=1), mutation: AdminMutationContext = Depends(mutation_context)):
        return await change(project_id, "scope_limits", body, mutation)

    @router.put("/api/projects/{project_id}/versions/{version_no}/pin")
    async def version_pin(body: Pin, project_id: int = Path(ge=1), version_no: int = Path(ge=1),
                          mutation: AdminMutationContext = Depends(mutation_context)):
        return await change(project_id, "version_pin", body, mutation, version_no=version_no)

    @router.post("/api/projects/{project_id}/previews/{preview_id}/revoke")
    async def revoke(body: Revision, project_id: int = Path(ge=1),
                     preview_id: str = Path(min_length=22, max_length=200, pattern=r"^[A-Za-z0-9_-]+$"),
                     mutation: AdminMutationContext = Depends(mutation_context)):
        return await change(project_id, "preview_revoke", body, mutation, preview_id=preview_id)

    return router
