"""Account-owned, bounded ephemeral control work; never a model or durable replay."""
import asyncio

from .link_authority import LinkAuthority
from .link_feedback import LinkFeedback
from .link_ingress import LinkIngress
from .link_notices import LinkNotices
from .lease import LeaseLost
from .send_types import OutboxInvariantError


class LinkRuntime:
    def __init__(self,runtime,token):
        self.runtime,self.token = runtime,token
        self.ingress = LinkIngress(LinkAuthority(runtime.outbox.pepper,clock=runtime.store.clock,authority=runtime.store.authority),
            admission=runtime.store.admitted)
        self.notices = LinkNotices(self.ingress,runtime.outbox); self.feedback = LinkFeedback(self.ingress,runtime.outbox)
        self.sender = runtime.sender; self.tasks = set(); self.closing = False; self.next_sweep = 0.0

    def _recover(self):
        with self.runtime.store.transaction() as cursor:return self.ingress.reducer.recover(cursor,self.token)

    async def attach(self):
        self.runtime.store.link_ingress = self.ingress
        self.runtime.outbox.link_authority = self.notices; self.runtime.outbox.link_feedback_authority = self.feedback
        await self.runtime._calls(self._recover)  # Old RAM is irrecoverable; do this BEFORE sender receipt recovery.

    def _close_code(self,decision):
        if decision.code is None:return
        with self.runtime.store.transaction() as cursor:
            row = self.ingress.reducer.get(cursor,self.token,decision.challenge_id)
            if row and row['delivery_state']=='pending':self.ingress.reducer.close(cursor,row)

    def _prepare(self,accepted):
        with self.runtime.store.transaction() as cursor:
            if accepted._link_input.code is not None:return self.notices.prepare(cursor,self.token,accepted._link_input)
            return self.feedback.prepare(cursor,self.token,accepted.inbox_id)

    async def _emit(self,accepted):
        try:
            await self.runtime._ensure_current(self.token)
            outcome = await self.sender.emit_ephemeral(self._prepare,accepted)
            if outcome!='success':self.runtime.logger.warning('QQ link control delivery not confirmed: %s',outcome)
        finally:
            # Timeout/cancel can leave a POST in flight. Close authorization,
            # but the sender must still drain any later accepted receipt as fact.
            try:await self.runtime._calls(self._close_code,accepted._link_input)
            except LeaseLost:pass

    def _finished(self,task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.runtime.logger.warning('QQ link control delivery interrupted: %s',type(task.exception()).__name__)

    async def consume(self,accepted):
        if accepted._link_input is None or accepted.inbox_id is None:return
        await self.runtime._ensure_current(self.token)
        if self.closing or len(self.tasks)>=32:
            await self.runtime._calls(self._close_code,accepted._link_input)
            self.runtime.logger.warning('QQ link control delivery unavailable: busy_or_closing')
            return
        task = asyncio.create_task(self._emit(accepted)); self.tasks.add(task); task.add_done_callback(self._finished)

    def _sweep(self):
        with self.runtime.store.transaction() as cursor:
            self.runtime.store.epoch_guard(cursor,self.token)
            rows = cursor.execute("SELECT * FROM qq_official_link_challenges WHERE account_key=? AND state IN ('sending','issued','confirming') LIMIT 21 FOR UPDATE",
                (self.token.account_key,)).fetchall()
            if len(rows)>20:raise OutboxInvariantError('link_challenge_overflow')
            for row in rows:
                if not self.ingress.reducer.current(cursor,self.token,row):self.ingress.reducer.close(cursor,row)

    async def maintain(self):
        if self.closing or asyncio.get_running_loop().time()<self.next_sweep:return
        await self.runtime._ensure_current(self.token); await self.runtime._calls(self._sweep)
        self.next_sweep = asyncio.get_running_loop().time()+5

    async def close(self):
        self.closing = True
        tasks = tuple(self.tasks)
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        try:await self.runtime._calls(self._recover)
        except LeaseLost:pass
        finally:
            if self.runtime.store.link_ingress is self.ingress:self.runtime.store.link_ingress = None
            if self.runtime.outbox.link_authority is self.notices:self.runtime.outbox.link_authority = None
            if self.runtime.outbox.link_feedback_authority is self.feedback:self.runtime.outbox.link_feedback_authority = None
            if self.runtime.link_service is self:self.runtime.link_service = None
