"""Read-only operations audit. Admin login is NOT an alternative approval path."""
from typing import Literal
from fastapi import Query

from ..ops_audit import snapshot
from .context import authorize


Outcome = Literal['','sending','pending','approved','executing','succeeded','failed','denied','expired','cancelled','unknown']


def create_router(context):
    router = context.new_router()

    @router.get('/api/ops-actions')
    def actions(limit: int = Query(50,ge=1,le=100),scope_key: str = Query('',max_length=1000),
                plan: str = Query('',max_length=48),state: Outcome = '',
                before: str = Query('',pattern=r'^(?:[0-9]{1,18}:[a-f0-9]{32})?$')):
        authorize()
        database = context.services.database
        if database is None:
            return {'available':False,'items':[],'counts':{},'next_cursor':None}
        return {'available':True,**snapshot(database,limit=limit,scope_key=scope_key,plan=plan,state=state,before=before)}

    return router
