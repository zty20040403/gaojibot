"""Administrator-only group wiki settings and evidence-checked projections."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ..conversation_scope import ConversationScope
from .context import AdminRouteContext, authorize, mutation_context, mutation_payload
from .contracts import AdminMutationContext


class WikiSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bot_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    enabled: StrictBool
    newcomer_enabled: StrictBool
    expected_version: int = Field(ge=0)


def create_router(context: AdminRouteContext) -> APIRouter:
    router = context.new_router()
    store = context.services.group_wiki_store

    def group_scope(group_id: int) -> ConversationScope:
        if not 0 < group_id < 10**20:
            raise HTTPException(status_code=422, detail="群号必须为有效正整数")
        if store is None:
            raise HTTPException(status_code=503, detail="群百科未启用")
        return ConversationScope("onebot-v11", "group", str(group_id))

    @router.get("/api/wiki")
    async def overview() -> dict:
        authorize()
        def read():
            return context.versioned("wiki", {"configured": store is not None,
                "groups": store.admin_groups() if store else [],
                "hour": getattr(context.services.settings, "group_wiki_hour", 3),
                "timezone": getattr(context.services.settings, "group_wiki_timezone", "Asia/Shanghai")})
        return await asyncio.to_thread(read)

    @router.get("/api/wiki/{group_id}")
    async def detail(group_id: int, query: str = Query(default="", max_length=200),
                     offset: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=100)) -> dict:
        authorize()
        scope = group_scope(group_id)
        def read():
            page = store.page(scope, query=query)
            return context.versioned("wiki", {**page, "settings": store.settings(scope),
                "total": len(page["items"]), "items": page["items"][offset:offset + limit],
                "offset": offset, "jobs": store.admin_jobs(scope)})
        return await asyncio.to_thread(read)

    @router.put("/api/wiki/{group_id}/settings")
    async def settings(group_id: int, body: WikiSettingsUpdate,
                       mutation: AdminMutationContext = Depends(mutation_context)) -> dict:
        authorize()
        scope = group_scope(group_id)
        if mutation.expected_version is None:
            raise HTTPException(status_code=428, detail="修改群百科必须携带 If-Match 资源版本")
        def write():
            before = store.settings(scope)
            try:
                return context.mutate(mutation, "wiki", action="wiki.configure", target=scope.key,
                    before=before, operation=lambda _version: store.configure(scope, **body.model_dump()))
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from None
        try:
            result = await asyncio.to_thread(write)
        finally:
            await asyncio.to_thread(context.event_broker.publish, "wiki", "jobs", "audit")
        return mutation_payload(result, settings=result.value)

    return router
