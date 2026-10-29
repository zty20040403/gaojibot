"""Authenticated exact-request migration control; no identity merging endpoint."""
from __future__ import annotations

import re
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..admin_security import SESSION_COOKIE
from .context import AdminRouteContext, authorize


class StrictRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)


class MappingRequest(StrictRequest):
    source_endpoint: str=Field(min_length=1,max_length=1000)
    source_scope: str=Field(min_length=1,max_length=1000)
    source_actor: str=Field(min_length=1,max_length=1280)
    target_endpoint: str=Field(min_length=1,max_length=1000)
    target_actor: str=Field(min_length=1,max_length=1280)
    evidence_inbox_id: str=Field(min_length=1,max_length=64)
    expected_revision: int=Field(ge=0,le=2**63-2)
    expires_at: int=Field(ge=0,le=2**63-1)


class MappingRevoke(StrictRequest):
    expected_revision: int=Field(ge=1,le=2**63-2)


class ProjectGrantRequest(StrictRequest):
    source_endpoint: str=Field(min_length=1,max_length=1000)
    source_scope: str=Field(min_length=1,max_length=1000)
    source_actor: str=Field(min_length=1,max_length=1280)
    target_endpoint: str=Field(min_length=1,max_length=1000)
    target_actor: str=Field(min_length=1,max_length=1280)
    issuer_actor: str=Field(min_length=1,max_length=1280)
    project_id: int=Field(ge=1,le=2**63-1)
    mapping_id: str=Field(pattern=r'^[a-f0-9]{32}$')
    mapping_revision: int=Field(ge=1,le=2**63-1)
    capabilities: list[Literal['project.read','project.write','project.execute','project.manage','project.deliver']]=Field(min_length=1,max_length=5)
    expires_at: int=Field(ge=1,le=2**63-1)
    expected_revision: int=Field(default=0,ge=0,le=2**63-2)


class ItemRequest(StrictRequest):
    kind: Literal['reminder','routine','subagent','project_task','file']
    key: str=Field(min_length=1,max_length=128,pattern=r'^[a-z0-9:]+$')


class PlanRequest(StrictRequest):
    source_endpoint: str=Field(min_length=1,max_length=1000)
    source_scope: str=Field(min_length=1,max_length=1000)
    target_endpoint: str=Field(min_length=1,max_length=1000)
    items: list[ItemRequest]=Field(max_length=100)
    deadline: int=Field(ge=1,le=2**63-1)


class HistoryPreviewRequest(StrictRequest):
    preview_digest: str=Field(pattern=r'^[a-f0-9]{64}$')
    chapter_ids: list[int]=Field(default_factory=list,max_length=20)
    public_note_ids: list[int]=Field(default_factory=list,max_length=30)


class VerificationRequest(StrictRequest):
    preview_digest: str=Field(pattern=r'^[a-f0-9]{64}$')
    kind: Literal['text','file']
    inbox_id: str=Field(pattern=r'^[a-f0-9]{32}$')
    artifact_id: str | None=Field(default=None,pattern=r'^[a-f0-9]{32}$')


class TransitionRequest(StrictRequest):
    action: Literal['refresh','freeze','drain','quarantine','switch','verify','complete','rollback']
    preview_digest: str=Field(pattern=r'^[a-f0-9]{64}$')
    activity_id: str | None=Field(default=None,pattern=r'^[a-f0-9]{32}$')
    finish: bool=False


def create_router(context: AdminRouteContext):
    router=context.new_router()

    def service(request):
        authorize()
        if request.query_params:
            raise HTTPException(400,'Migration unavailable')
        getter=context.services.official_migration_service
        try:
            value=getter() if callable(getter) else None
        except Exception:
            value=None
        if value is None:
            raise HTTPException(404,'Migration unavailable')
        return value

    def identity(value):
        if not re.fullmatch('[a-f0-9]{32}',value):
            raise HTTPException(404,'Migration unavailable')
        return value

    def failed(action):
        from ..observability import telemetry
        from ..adapters.qq_official.official_metrics import official_metrics, observe
        observe(official_metrics(telemetry.registry), 'migration', action)

    async def mutate(request,action,payload,preview=''):
        value=service(request)
        try:
            return await value.mutate(cookie=request.cookies.get(SESSION_COOKIE,''),
                csrf=request.headers.get('x-csrf-token',''),action=action,request=payload,preview_digest=preview)
        except Exception:
            failed(action)
            raise HTTPException(409,'Migration unavailable') from None

    @router.post('/api/qq-official/migrations/mappings')
    async def mapping_put(request:Request,body:MappingRequest):
        return await mutate(request,'mapping_put',body.model_dump())

    @router.post('/api/qq-official/migrations/project-grants')
    async def project_grant(request:Request,body:ProjectGrantRequest):
        return await mutate(request,'project_grant_stage',body.model_dump())

    @router.post('/api/qq-official/migrations/project-grants/{grant_id}/revoke')
    async def project_grant_revoke(grant_id:str,request:Request,body:MappingRevoke):
        return await mutate(request,'project_grant_revoke',dict(grant_id=identity(grant_id),**body.model_dump()))

    @router.post('/api/qq-official/migrations/mappings/{mapping_id}/revoke')
    async def mapping_revoke(mapping_id:str,request:Request,body:MappingRevoke):
        return await mutate(request,'mapping_revoke',dict(mapping_id=identity(mapping_id),**body.model_dump()))

    @router.post('/api/qq-official/migrations')
    async def plan(request:Request,body:PlanRequest):
        return await mutate(request,'plan',body.model_dump())

    @router.get('/api/qq-official/migrations/{migration_id}')
    async def read(migration_id:str,request:Request):
        value=service(request)
        key=identity(migration_id)
        try:
            return await value.read(cookie=request.cookies.get(SESSION_COOKIE,''),migration_id=key)
        except Exception:
            raise HTTPException(404,'Migration unavailable') from None

    @router.post('/api/qq-official/migrations/{migration_id}/history-preview')
    async def history_preview(migration_id:str,request:Request,body:HistoryPreviewRequest):
        return await mutate(request,'history_preview',dict(migration_id=identity(migration_id),
            chapter_ids=body.chapter_ids,public_note_ids=body.public_note_ids),body.preview_digest)

    @router.post('/api/qq-official/migrations/{migration_id}/verification')
    async def verification(migration_id:str,request:Request,body:VerificationRequest):
        value=service(request)
        key=identity(migration_id)
        if (body.kind=='file') != (body.artifact_id is not None):
            raise HTTPException(422,'Invalid verification fields')
        try:
            return await value.request_verification(cookie=request.cookies.get(SESSION_COOKIE,''),
                csrf=request.headers.get('x-csrf-token',''),migration_id=key,**body.model_dump())
        except Exception:
            failed('verify')
            raise HTTPException(409,'Migration unavailable') from None

    @router.post('/api/qq-official/migrations/{migration_id}/actions')
    async def transition(migration_id:str,request:Request,body:TransitionRequest):
        if ((body.action=='quarantine')!=(body.activity_id is not None)
                or 'finish' in body.model_fields_set and body.action!='rollback'
                or 'activity_id' in body.model_fields_set and body.action!='quarantine'):
            raise HTTPException(422,'Invalid transition fields')
        payload={'migration_id':identity(migration_id)}
        if body.action=='quarantine':
            payload['activity_id']=body.activity_id
        if body.action=='rollback':
            payload['finish']=body.finish
        return await mutate(request,body.action,payload,body.preview_digest)

    return router
