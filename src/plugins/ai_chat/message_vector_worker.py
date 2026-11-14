"""Account-owned derived indexing; no VM channel, actor grants or QQ send effects."""
import asyncio

from .adapters.qq_official.billing import scoped
from .adapters.qq_official.lease import LeaseLost
from .adapters.qq_official.vision_adapter import guarded_wait
from .billing_context import drain
from .message_vectors import MessageVectorJobs
from .workers.durable_jobs import DurableJobWorker


def build_worker(runtime,token):
    app = runtime.application
    if app is None or not getattr(app.settings,'semantic_enabled',False):return None
    if getattr(app,'semantic_recall',None) is None or getattr(app,'model_billing',None) is None:
        raise RuntimeError('semantic_provider_or_accounting_unavailable')
    return MessageVectorWorker(runtime,token)


class MessageVectorWorker:
    def __init__(self,runtime,token):
        self.runtime,self.app,self.token = runtime,runtime.application,token
        self.service = self.app.semantic_recall
        self.catalog = MessageVectorJobs(runtime.database,runtime.jobs,token,self.service.backend,self.service.embedder,
            admission=runtime.store.admitted,clock=runtime.store.clock)
        self.after,self.before = '',{}
        self.worker = DurableJobWorker(runtime.jobs,logger=runtime.logger,concurrency=2,per_scope_limit=1,
            database_calls=runtime._calls,worker_id='semantic:'+token.holder_id)
        self.worker.register(self.catalog.kind,self.execute,timeout_seconds=self.service.embedder.timeout_seconds+30)

    async def schedule_once(self):
        endpoints = await self.runtime._calls(self.catalog.endpoints,after=self.after,limit=4)
        if not endpoints:
            self.after = ''
            return 0
        count = 0
        for endpoint in endpoints:
            self.after = endpoint.key
            before = self.before.get(endpoint.key)
            try:
                count += len(await self.runtime._calls(self.catalog.enqueue,endpoint,limit=16,before=before))
                self.before[endpoint.key] = self.catalog.page_before.get(endpoint.key,before)
            except PermissionError:self.before.pop(endpoint.key,None)  # Denial is not a provider failure.
        return count

    async def execute(self,job):
        try:
            endpoint,document = await self.runtime._calls(self.catalog.current,job)
            if not document.content:
                await self.runtime._calls(self.catalog.save,job,document,[])
                return {'skipped':True}
            async def current():await self.runtime._calls(self.catalog.current,job)
            with scoped(self.app,endpoint,'semantic_index',current=current):
                vector = (await guarded_wait(self.service.embedder.embed([document.content]),current,
                    timeout=self.service.embedder.timeout_seconds+5))[0]
            await self.runtime._calls(self.catalog.save,job,document,vector)
            return {'indexed':True}
        except asyncio.CancelledError:
            # Generic workers defer cancellation. This single-attempt original
            # index job must instead remain terminal, after billing is drained.
            await drain(asyncio.create_task(self.runtime._calls(self.runtime.jobs.mark_failed,
                job.job_id,job.lease_owner,'semantic_interrupted_unknown',retryable=False,lease_generation=job.lease_generation)))
            raise

    async def poll(self):
        while True:
            try:await self.schedule_once()
            except LeaseLost:raise
            except Exception as error:self.runtime.logger.warning('Semantic indexing schedule failed error=%s',type(error).__name__)
            await asyncio.sleep(5)

    async def run(self):
        tasks = [asyncio.create_task(self.poll()),asyncio.create_task(self.worker.run_forever())]
        try:
            done,_ = await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in done:await task
            raise RuntimeError('semantic_worker_stopped')
        finally:
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
