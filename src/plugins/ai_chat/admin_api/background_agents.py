"""Inspect and stop background jobs in the authenticated console."""
from fastapi import Depends, Header, HTTPException
from .context import authorize, mutation_context, mutation_payload
from .contracts import AdminMutationContext


def create_router(context):
    router = context.new_router()
    store = context.services.background_jobs

    @router.get('/api/background-agents')
    def recent(authorization: str | None = Header(default=None)):
        authorize(authorization)
        return {'items': store.list(limit=100) if store is not None else []}

    @router.post('/api/background-agents/{job_id}/cancel')
    def cancel(job_id: int, authorization: str | None = Header(default=None),
               mutation_info: AdminMutationContext = Depends(mutation_context)):
        authorize(authorization)
        row = store.get(job_id) if store is not None else None
        if row is None:
            raise HTTPException(404, '后台任务不存在')
        result = context.mutate(mutation_info, 'backgroundAgents', action='background.cancel',
            target=f'job#{job_id}', operation=lambda _: store.cancel(job_id, row['endpoint_key'], row['actor_key']))
        context.event_broker.publish('backgroundAgents', 'tasks')
        return mutation_payload(result, job_id=job_id)

    return router
