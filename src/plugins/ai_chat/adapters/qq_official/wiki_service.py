"""Host-composed official wiki worker on the existing account durable worker."""
from ...agent_loop import AgentTrace, ask_deepseek_json
from ...group_wiki import WikiLeaseLost, WikiSuperseded
from ...group_wiki_worker import GroupWikiWorker, WIKI_SYSTEM_PROMPT, generation_evidence
from .wiki_commands import OfficialWikiStore, WikiCommands, _deny


class OfficialWikiService:
    def __init__(self,*,runtime,token,app,collection_allowed=_deny,generate=None,configuration=None):
        self.runtime,self.app,self.token=runtime,app,token
        self.configuration=configuration
        self.store=OfficialWikiStore(runtime.store.database,ledger=app.message_ledger,context=app.context_store,
            jobs=runtime.jobs,token=token,admission=runtime.store.admitted,owners=runtime.store.authority,
            clock=runtime.store.clock,collection_allowed=collection_allowed)
        self.worker=GroupWikiWorker(self.store,db_call=runtime._calls,generate=generate or self._generate,
            permitted=self._permitted,logger=app.logger,hour=configuration.hour if configuration else app.settings.group_wiki_hour,
            timezone=configuration.timezone if configuration else app.settings.group_wiki_timezone,
            check_seconds=configuration.check_seconds if configuration else app.settings.group_wiki_check_seconds,
            clock=runtime.store.clock,scope_label=lambda _:'official-group')
        self.next_maintenance=0

    async def _permitted(self,scope,bot_id):
        def check():
            with self.store._transaction() as cursor:
                config=self.store._guard(cursor,scope)
                return config['bot_id']==bot_id
        try:return await self.runtime._calls(check)
        except WikiLeaseLost:return False

    async def _generate(self,payload):
        # The existing GroupWikiWorker installs its source guard for every RPC.
        profile=self.app.model_catalog.resolve_preference(self.configuration.profile if self.configuration else self.app.settings.group_wiki_profile)
        trace=AgentTrace(provider=profile.provider_identity,model=profile.model,profile=profile.name)
        try:
            return await ask_deepseek_json(WIKI_SYSTEM_PROMPT,generation_evidence(payload),
                profile=profile,trace=trace,recover_empty_response=False)
        finally:
            if self.app.usage_store is not None and (trace.input_tokens or trace.output_tokens):
                await self.runtime._calls(self.app.usage_store.record,scope_key=payload['scope_key'],source='group-wiki',
                    provider=trace.provider,model=trace.model,input_tokens=trace.input_tokens,
                    output_tokens=trace.output_tokens,cached_tokens=trace.cached_tokens)

    def commands_for(self,*,life,inbound):
        return WikiCommands(store=self.store,life=life,inbound=inbound,database_calls=self.runtime._calls)

    def register(self,worker):
        worker.register(self.store.job_kind,self.handle_job,timeout_seconds=210)

    async def handle_job(self,job):
        try:return await self.worker.handle_job(job)
        except (WikiLeaseLost,WikiSuperseded):return {'status':'superseded'}
        except Exception:raise RuntimeError('official_wiki_generation_unavailable') from None

    async def maintain(self):
        now=self.store._now()
        if now<self.next_maintenance:return
        self.next_maintenance=now+self.worker.check_seconds
        await self.worker.schedule_once(now=now)

    def close(self):self.store.close()
