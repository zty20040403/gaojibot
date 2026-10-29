"""One physical OCR call charged through the existing UsageStore.

An uncertain provider call retains its conservative input/output bounds. Known
usage only settles this exact internal reservation; it cannot authorize another
call. No database transaction spans provider I/O and no new queue is created.
"""
from .media_refs import MediaDenied


class OCRUsage:
    def __init__(self, session, source):
        self.session, self.source = session, source
        self.rows = []

    async def current(self):
        await self.session.resolver.require(self.source)

    async def reserve(self, reservation):
        session, usage = self.session, self.session.service.app.usage_store
        if usage is None:
            raise MediaDenied('ocr_usage_unavailable')
        def write():
            runtime = session.service.runtime
            with runtime.deliveries.transaction() as cursor:
                now = int(runtime.store.clock())
                row = session.platform.execution.current(cursor, now)
                session._check_row(row, int(runtime.store.clock()))
                # Serialize these additional OCR calls for the actual scope;
                # ordinary chat retains its existing UsageStore admission rules.
                scope = session.inbound.scope.key
                cursor.execute('SELECT pg_advisory_xact_lock(hashtextextended(?,0))',
                               ('official-media-usage:' + scope,))
                quota = usage.status(scope, now=now)
                charge = reservation.reserved
                if (not quota.allowed
                    or quota.input_limit > 0 and quota.input_tokens + charge > quota.input_limit
                    or quota.output_limit > 0 and quota.output_tokens + charge > quota.output_limit):
                    raise MediaDenied('ocr_quota_exhausted')
                session._check_row(row, int(runtime.store.clock()))
                event = usage.record(scope_key=scope, source='official_ocr_bound',
                    provider=reservation.provider, model=reservation.model,
                    input_tokens=charge, output_tokens=charge,
                    turn_id=row['journal_turn_id'], occurred_at=now, transaction_cursor=cursor)
                return event
        event = await session.service.runtime._calls(write)
        self.rows.append((event, reservation))

    async def settle(self):
        for event, reservation in self.rows:
            if reservation.counts is None:
                continue
            incoming, outgoing, cached = reservation.counts
            def write():
                # Historical receipt, not an execution permit. A source revoked
                # while the provider was in flight still retains its real cost.
                with self.session.service.runtime.deliveries.transaction() as cursor:
                    cursor.execute('''UPDATE usage_events SET source='official_ocr',
                        input_tokens=?,output_tokens=?,cached_tokens=?
                        WHERE usage_id=? AND scope_key=? AND source='official_ocr_bound' ''',
                        (incoming,outgoing,cached,event,self.session.inbound.scope.key))
            await self.session.service.runtime._calls(write)
