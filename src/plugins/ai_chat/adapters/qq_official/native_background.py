"""Account background composition: proactive replies and their source guard."""
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.adapters.qq_official.proactive_ingress import ProactiveCandidate


class NativeBackgroundService:
    def __init__(self,*,runtime,token,proactive=None):
        self.runtime,self.token,self.proactive=runtime,token,proactive
        self.previous_guard=runtime.outbox.source_guard

    def attach(self):
        self.runtime.outbox.source_guard=self.plan_guard

    def plan_guard(self,c,*,plan,operation,source_proof=None,**kwargs):
        proactive=c.execute('SELECT 1 FROM qq_official_proactive_observations WHERE plan_id=?',(plan['plan_id'],)).fetchone()
        if type(source_proof) is ProactiveCandidate or proactive:
            if self.proactive is None:raise MigrationDenied('proactive_disabled')
            return self.proactive.plan_guard(c,plan=plan,operation=operation,source_proof=source_proof,**kwargs)
        if self.previous_guard is not None:
            return self.previous_guard(c,plan=plan,operation=operation,source_proof=source_proof,**kwargs)
        return True

    async def on_ingested(self,job):
        if self.proactive is not None:return await self.proactive.on_ingested(job)
        return None

    async def close(self):
        # Restore only our own wrapper; never overwrite a later host guard.
        if self.runtime.outbox.source_guard==self.plan_guard:
            self.runtime.outbox.source_guard=self.previous_guard
