"""Reserve usage before each wake model request; unknown usage is never free."""
from ...model_budget import ModelCallBudget
from .program_actions import ProgramDenied


class ProgramUsage:
    def __init__(self, execution, run, usage, calls, *, current):
        self.execution,self.run,self.usage,self.calls = execution,run,usage,calls
        self.receipts = []
        self.budget = ModelCallBudget(1_000_000,max_requests=24,before_request=current,reserve_request=self.reserve)

    async def reserve(self, reservation):
        event = await self.calls(self._reserve,reservation)
        self.receipts.append((event,reservation))

    def _reserve(self, reservation):
        with self.execution.transaction() as cursor:
            _,inputs = self.execution.check(cursor,self.run)
            scope,now = inputs[0]['scope_key'],int(self.execution.clock())
            cursor.execute('SELECT pg_advisory_xact_lock(hashtextextended(?,0))',('official-media-usage:'+scope,))
            quota = self.usage.status(scope,now=now)
            bound = reservation.reserved
            if (not quota.allowed or quota.input_limit>0 and quota.input_tokens+bound>quota.input_limit
                    or quota.output_limit>0 and quota.output_tokens+bound>quota.output_limit):
                raise ProgramDenied('program_model_quota_exhausted',429)
            return cursor.execute('''INSERT INTO usage_events
                (scope_key,source,provider,model,input_tokens,output_tokens,cached_tokens,occurred_at,program_wake_id)
                VALUES (?,'program_wake_bound',?,?,?,?,0,?,?) RETURNING usage_id''',
                (scope,reservation.provider,reservation.model,bound,bound,now,self.run['wake_id'])).fetchone()['usage_id']

    def settle(self):
        # Historical usage, like accepted QQ receipts, remains true after stop
        # or lease loss. Only this request's reserved row can be settled.
        with self.execution.transaction() as cursor:
            for event,reservation in self.receipts:
                if reservation.counts is not None:
                    incoming,outgoing,cached = reservation.counts
                    cursor.execute('''UPDATE usage_events SET source='program_wake',input_tokens=?,output_tokens=?,cached_tokens=?
                        WHERE usage_id=? AND program_wake_id=? AND source='program_wake_bound' ''',
                        (incoming,outgoing,cached,event,self.run['wake_id']))
