"""Account-owned private question lifecycle; source ask is a separate boundary."""
import asyncio

from .lease import LeaseLost
from .question_authority import QuestionAuthority
from .question_model import QuestionModel
from .question_worker import QuestionWorker
from .questions import Questions


class QuestionRuntime:
    def __init__(self,runtime,token,ingress):
        self.runtime,self.token = runtime,token
        self.questions = Questions(QuestionAuthority(ingress,admission=runtime.store.admitted),runtime.jobs)
        self.worker,self.model = None,None
        self.closing,self.next_sweep = False,0.0

    @property
    def available(self):return not self.closing and self.worker is not None

    def _recover(self):
        with self.runtime.store.transaction() as cursor:return self.questions.recover(cursor,self.token)

    async def attach(self):
        # Even an account now running without AI must retire its old questions.
        # Recovery precedes workers and inbound frames; no old RAM/epoch resumes.
        await self.runtime._calls(self._recover)
        app = self.runtime.application
        if app is None:return False
        if (any(getattr(app,name,None) is None for name in ('model_billing','usage_store','model_catalog','model_preferences','message_ledger','conversations'))
            or app.model_billing.database!=self.runtime.database):raise RuntimeError('question_model_or_accounting_unavailable')
        self.model = QuestionModel(self.runtime,self.token,self.questions)
        self.worker = QuestionWorker(self.runtime,self.token,self.questions,self.model,
            ensure_current=lambda:self.runtime._ensure_current(self.token))
        return True

    def submit(self,binding,reference,question):
        if not self.available:raise RuntimeError('question_model_unavailable')
        with self.runtime.store.transaction() as cursor:return self.questions.submit(cursor,self.token,binding,reference,question)

    def _sweep(self):
        with self.runtime.store.transaction() as cursor:
            self.runtime.store.epoch_guard(cursor,self.token)
            rows = cursor.execute("SELECT * FROM qq_official_conversation_questions WHERE account_key=? AND state IN ('queued','running') LIMIT 129 FOR UPDATE",
                (self.token.account_key,)).fetchall()
            if len(rows)>128:raise RuntimeError('question_live_limit_exceeded')
            for row in rows:self.questions.retire(cursor,self.token,dict(row))

    async def maintain(self):
        if self.closing or asyncio.get_running_loop().time()<self.next_sweep:return
        await self.runtime._ensure_current(self.token); await self.runtime._calls(self._sweep)
        self.next_sweep = asyncio.get_running_loop().time()+5

    async def run(self):
        if not self.available:raise RuntimeError('question_model_unavailable')
        await self.worker.worker.run_forever()

    async def close(self):
        self.closing = True
        # _serve drains/cancels the account's worker/model/socket/billing tasks
        # first. Then close pending leftovers without rewriting finished facts.
        try:await self.runtime._calls(self._recover)
        except LeaseLost:pass
        finally:
            if self.runtime.question_service is self:self.runtime.question_service = None
