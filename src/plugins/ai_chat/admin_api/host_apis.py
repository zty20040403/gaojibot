"""Read-only API call audit, without credential configuration or payloads."""
from fastapi import Query

from ..host_api_calls import HostAPICalls
from .context import authorize


def create_router(context):
    router = context.new_router()
    database = context.services.database
    store = HostAPICalls(database) if database is not None else None

    @router.get('/api/host-apis')
    def calls(limit: int = Query(100,ge=1,le=500),before: int = Query(0,ge=0),
              scope_key: str = Query('',max_length=1000),api_name: str = Query('',max_length=48)):
        authorize()
        if store is None:
            return {'available':False,'items':[],'counts':{},'next_cursor':None}
        return {'available':True,**store.snapshot(limit=limit,before=before,scope_key=scope_key,api_name=api_name)}

    return router
