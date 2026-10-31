"""Inspect and stop background jobs, and choose their model, in the authenticated console."""
import time

from fastapi import Depends, Header, HTTPException
import httpx
from pydantic import BaseModel

from ..codex_engine import EFFORTS, BackgroundModelSettings, upstream_profile
from .context import authorize, mutation_context, mutation_payload
from .contracts import AdminMutationContext


class BackgroundModelRequest(BaseModel):
    model: str
    effort: str


def create_router(context):
    router = context.new_router()
    store = context.services.background_jobs
    services = context.services
    model_settings = BackgroundModelSettings(services.model_preferences, services.settings)
    listed = {'at': 0.0, 'models': []}

    async def available_models():
        # What the upstream serves, for the console's picker; a failure only empties the list.
        if time.monotonic() - listed['at'] < 300:
            return listed['models']
        try:
            profile = upstream_profile(services.model_catalog, services.settings)
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                response = await client.get(profile.base_url.rstrip('/') + '/models',
                                            headers={'Authorization': 'Bearer ' + profile.api_key})
                response.raise_for_status()
                listed['models'] = sorted(str(item['id']) for item in response.json().get('data', []))
        except Exception:
            listed['models'] = []
        listed['at'] = time.monotonic()
        return listed['models']

    @router.get('/api/background-agents')
    async def recent(authorization: str | None = Header(default=None)):
        authorize(authorization)
        model, effort = model_settings.get()
        return {'items': store.list(limit=100) if store is not None else [],
                'settings': {'model': model, 'effort': effort, 'efforts': list(EFFORTS),
                             'models': await available_models()}}

    @router.put('/api/background-agents/settings')
    def set_model(selection: BackgroundModelRequest, authorization: str | None = Header(default=None),
                  mutation_info: AdminMutationContext = Depends(mutation_context)):
        authorize(authorization)
        model, effort = model_settings.get()

        def update(_version):
            model_settings.set(selection.model, selection.effort)
            return {'model': selection.model.strip(), 'effort': selection.effort}
        try:
            result = context.mutate(mutation_info, 'backgroundAgents', action='background.model.set',
                target='background', before={'model': model, 'effort': effort}, operation=update)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        context.event_broker.publish('backgroundAgents', 'settings')
        return mutation_payload(result, model=selection.model.strip(), effort=selection.effort)

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
