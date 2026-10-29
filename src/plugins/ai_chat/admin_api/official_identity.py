"""Same-session console confirmation of possession proved on actual C2C transport."""
from fastapi import HTTPException,Request
from pydantic import BaseModel,ConfigDict
from src.plugins.ai_chat.admin_security import require_admin
from src.plugins.ai_chat.adapters.qq_official.authority import ConsoleAuthorization
from .context import AdminRouteContext


class EmptyRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')


def create_router(context:AdminRouteContext):
    router=context.new_router()
    def source(request):
        admin=require_admin()
        if request.query_params:raise HTTPException(400,'Official identity binding unavailable')
        getter=context.services.official_identity_service
        value=getter() if callable(getter) else None
        if value is None:raise HTTPException(503,'Official identity binding unavailable')
        return value,ConsoleAuthorization(admin['account_id'],admin['version'],admin['session_hash'])
    async def call(request,operation,identity=None):
        service,authorization=source(request)
        try:
            method=getattr(service,operation)
            args=(authorization,) if identity is None else (identity,authorization)
            result=await service.runtime._calls(method,*args)
        except Exception:
            raise HTTPException(403,'Official identity binding unavailable') from None
        if operation!='status':context.event_broker.publish_runtime('qq-official')
        from fastapi.responses import JSONResponse
        return JSONResponse(result,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
    @router.post('/api/qq-official/admin-binding/challenges')
    async def create(request:Request,body:EmptyRequest):return await call(request,'create')
    @router.get('/api/qq-official/admin-binding/challenges/{identity}')
    async def status(request:Request,identity:str):return await call(request,'status',identity)
    @router.post('/api/qq-official/admin-binding/challenges/{identity}/confirm')
    async def confirm(request:Request,identity:str,body:EmptyRequest):return await call(request,'confirm',identity)
    return router
