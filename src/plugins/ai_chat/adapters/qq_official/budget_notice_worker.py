"""Bounded account reconciliation; only the sender can confirm external effects."""
import asyncio

from . import budget_notices
from .lease import LeaseLost,assert_epoch


class BudgetNoticeWorker:
    def __init__(self,runtime,token):
        self.runtime,self.token = runtime,token

    def reconcile(self):
        now = int(self.runtime.store.clock())
        created = False
        with self.runtime.store.transaction() as cursor:
            assert_epoch(cursor,self.token)
            rows = cursor.execute('''SELECT b.* FROM conversation_budget_days b JOIN qq_official_endpoints e USING(endpoint_key)
                WHERE e.account_key=? AND b.notice_state IN ('pending','sending') ORDER BY b.day_start,b.scope_key LIMIT 32''',
                (self.token.account_key,)).fetchall()
            for row in rows:
                if row['notice_plan_id'] is not None:
                    delivery = cursor.execute('SELECT * FROM deliveries WHERE plan_id=?',(row['notice_plan_id'],)).fetchone()
                    if delivery is None:status = 'failed'
                    elif delivery['status']=='pending':
                        reason = budget_notices.denial(cursor,token=self.token,delivery=delivery,now=now,admission=self.runtime.outbox.admission)
                        if reason is None:continue
                        self.runtime.outbox._finish(cursor,self.token,delivery,'cancelled',reason,now)
                        status = 'cancelled'
                    else:
                        status = {'committed':'committed','failed':'failed','cancelled':'cancelled','ambiguous':'unknown'}.get(delivery['status'])
                        if status is None:continue  # In flight/accepted is not known success or failure.
                else:
                    try:
                        ref = budget_notices.enqueue(cursor,self.token,row,admission=self.runtime.outbox.admission,now=now)
                    except PermissionError as error:
                        if str(error)=='budget_notice_identity_unavailable':continue  # Await authenticated READY, within original TTL.
                        status = 'cancelled'
                    else:
                        created |= ref.created
                        continue
                cursor.execute('UPDATE conversation_budget_days SET notice_state=? WHERE scope_key=? AND day_start=?',
                    (status,row['scope_key'],row['day_start']))
        return created

    async def run(self):
        while True:
            try:
                if await self.runtime._calls(self.reconcile):self.runtime.sender.wake()
            except LeaseLost:raise
            except Exception as error:
                self.runtime.logger.warning('Budget notice reconciliation failed error=%s',type(error).__name__)
            await asyncio.sleep(0.5)
