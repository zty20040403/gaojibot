"""Protected, read-only official account diagnostics, no request-selected source."""
from __future__ import annotations
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from .context import AdminRouteContext, authorize


def create_router(context: AdminRouteContext):
    router=context.new_router()

    @router.get('/api/qq-official/status')
    async def official_status(request: Request):
        authorize()
        if request.query_params:
            raise HTTPException(400,'Official diagnostics unavailable')
        getter=context.services.official_observability
        try:
            service=getter() if callable(getter) else None
            value=service.snapshot() if service is not None else None
        except Exception:
            value=None
        if value is None:raise HTTPException(503,'Official diagnostics unavailable')
        return JSONResponse(value,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
    @router.get('/api/qq-official/details')
    async def official_details(request: Request):
        authorize()
        pairs=list(request.query_params.multi_items())
        if len({k for k,_ in pairs})!=len(pairs) or any(k not in ('section','limit','cursor') for k,_ in pairs):
            raise HTTPException(400,'Official diagnostics unavailable')
        try:
            limit=int(request.query_params.get('limit','20'))
            if str(limit)!=request.query_params.get('limit','20'):raise ValueError()
            getter=context.services.official_observability
            service=getter() if callable(getter) else None
            if service is None:raise ValueError()
            value=await service.details(section=request.query_params.get('section','executions'),limit=limit,
                cursor=request.query_params.get('cursor'))
        except Exception:
            raise HTTPException(400,'Official diagnostics unavailable') from None
        return JSONResponse(value,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
    return router
