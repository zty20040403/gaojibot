"""Logged-in administrator projection, never a model/VM billing-control API."""
import time
from typing import Literal
from fastapi import HTTPException,Query

from ..billing_audit import snapshot
from .context import authorize


def create_router(context):
    router = context.new_router()

    @router.get('/api/model-costs')
    def costs(dimension: Literal['scope','job','program','actor'] = 'scope',days: int = Query(7,ge=1,le=365),
              scope_key: str = Query('',max_length=1000),as_of: int | None = Query(None,ge=0,le=2**63-1),
              limit: int = Query(50,ge=1,le=100),after: str = Query('',max_length=30000)):
        authorize()
        now = int(time.time())
        as_of = now if as_of is None else as_of
        if as_of>now:raise HTTPException(422,'不能查询未来账单窗口')
        database = context.services.database
        if database is None:return {'available':False,'items':[],'totals':None,'next_cursor':None}
        try:
            return {'available':True,**snapshot(database,dimension=dimension,days=days,as_of=as_of,
                scope_key=scope_key,limit=limit,after=after)}
        except ValueError:
            raise HTTPException(422,'无效的账单窗口或分页游标') from None

    return router
