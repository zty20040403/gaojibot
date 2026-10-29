"""Host composition for existing account worker kinds and source guards."""
import asyncio
import inspect
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.adapters.qq_official.native_schedules import OccurrenceProof
from src.plugins.ai_chat.adapters.qq_official.proactive_ingress import ProactiveCandidate


class NativeBackgroundService:
    def __init__(self,*,runtime,token,domain=None,proactive=None,wiki=None):
        self.runtime,self.token,self.domain,self.proactive,self.wiki=runtime,token,domain,proactive,wiki
        self.previous_commands=getattr(runtime,'native_commands',None)
        self.previous_guard=runtime.outbox.source_guard
        self._next_materialize=0

    def attach(self):
        self.runtime.outbox.source_guard=self.plan_guard
        self.runtime.native_commands=self

    def bind(self,*,life,inbound,platform):
        if (platform.execution.handle is not life.handle or platform.execution.job is not life.job
                or platform.execution.token is not self.token or life.token is not self.token):
            raise MigrationDenied('native_command_source_changed')
        callbacks={}
        if self.domain is not None:
            from src.plugins.ai_chat.adapters.qq_official.routine_commands import RoutineCommands
            callbacks['routine']=RoutineCommands(domain=self.domain,life=life,inbound=inbound,database_calls=self.runtime._calls)
        if self.wiki is not None:callbacks['wiki']=self.wiki.commands_for(life=life,inbound=inbound)
        return callbacks

    def register(self,worker):
        if self.wiki is not None:self.wiki.register(worker)

    def plan_guard(self,c,*,plan,operation,source_proof=None,**kwargs):
        native=c.execute('SELECT 1 FROM qq_official_native_schedule_occurrences WHERE plan_id=?',(plan['plan_id'],)).fetchone()
        proactive=c.execute('SELECT 1 FROM qq_official_proactive_observations WHERE plan_id=?',(plan['plan_id'],)).fetchone()
        if type(source_proof) is OccurrenceProof or native:
            if self.domain is None:raise MigrationDenied('native_background_disabled')
            return self.domain.plan_guard(c,plan=plan,operation=operation,source_proof=source_proof,**kwargs)
        if type(source_proof) is ProactiveCandidate or proactive:
            if self.proactive is None:raise MigrationDenied('proactive_disabled')
            return self.proactive.plan_guard(c,plan=plan,operation=operation,source_proof=source_proof,**kwargs)
        if self.previous_guard is not None:
            return self.previous_guard(c,plan=plan,operation=operation,source_proof=source_proof,**kwargs)
        return True

    async def execute(self,job):
        def native():
            with self.runtime.store.transaction() as c:
                return c.execute('SELECT 1 FROM qq_official_native_schedule_occurrences WHERE job_id=? AND account_key=?',
                    (job.job_id,self.token.account_key)).fetchone() is not None
        if await self.runtime._calls(native):
            if self.domain is None:return {'status':'suppressed','reason':'native_background_disabled'}
            return await self.domain.execute(job)
        # Lookup selects an existing migration domain; that domain still checks
        # its real persisted item and exact job. Payload alone never authorizes.
        migration=self.runtime.migration_service
        if migration is None:raise MigrationDenied('background_source_unavailable')
        for kind in ('reminder','routine'):
            from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
            if job.kind==account_job_kind(kind,self.token.account_key):
                domain=migration.domains.get(kind)
                if domain is not None:return await domain.execute(job)
        raise MigrationDenied('background_source_unavailable')

    async def on_ingested(self,job):
        if self.proactive is not None:return await self.proactive.on_ingested(job)
        return None

    async def maintain(self):
        if self.wiki is not None:await self.wiki.maintain()
        now=asyncio.get_running_loop().time()
        if self.domain is not None and now>=self._next_materialize:
            await self.runtime._calls(self.domain.materialize_due)
            await self.runtime._calls(self.domain.reconcile)
            self._next_materialize=now+1

    async def close(self):
        try:
            if self.wiki is not None:
                value=self.wiki.close()
                if inspect.isawaitable(value):await value
        finally:
            if getattr(self.runtime,'native_commands',None) is self:
                self.runtime.native_commands=self.previous_commands
            # Restore only our own wrapper; never overwrite a later host guard.
            if self.runtime.outbox.source_guard==self.plan_guard:
                self.runtime.outbox.source_guard=self.previous_guard
