"""Commit each due reminder or background job together with its next occurrence."""
import json

from ...message_ir import MessageBody, MentionNode, TextNode
from ...schedules import Schedules, next_time
from .renderer import render_plan


class ScheduleRuntime:
    def __init__(self, agents):
        self.agents, self.runtime = agents, agents.runtime
        self.store = Schedules(self.runtime.store.transaction, clock=agents.clock)
        self.next_scan = 0

    async def control(self, turn, name, args):
        inbound, life = turn.context.inbound, turn.context.platform.execution
        def run():
            with self.store.transaction() as cursor:
                row = life.current(cursor, int(self.agents.clock()))
                if name == 'schedule_set':
                    source = {key: row[key] for key in ('account_key', 'endpoint_key', 'scope_key',
                        'canonical_message_id', 'endpoint_generation', 'route_epoch')}
                    source.update(actor_key=inbound.actor.key, native_message_id=inbound.native_message_id,
                        source_execution_id=life.handle.execution_id, profile=turn.model_profile.name)
                    return self.store.create(cursor, source, **args)
                if name == 'schedule_cancel':
                    return {'cancelled': self.store.cancel(cursor, inbound.endpoint.key, inbound.actor.key, int(args['id']))}
                return {'items': self.store.list(cursor, inbound.endpoint.key, inbound.actor.key)}
        return await self.agents.calls(run)

    async def tick(self):
        now = int(self.agents.clock())
        if now < self.next_scan:
            return
        self.next_scan = now + 30
        def scan():
            with self.store.transaction() as cursor:
                rows = cursor.execute('''SELECT id FROM schedules WHERE cancelled_at IS NULL AND next_at<=?
                    AND source_json::jsonb->>'account_key'=? ORDER BY next_at LIMIT 100''',
                    (now, self.agents.token.account_key)).fetchall()
            for row in rows:
                try:
                    self.fire(row['id'], now)
                except (ValueError, PermissionError) as error:
                    self.agents.app.logger.warning('Schedule %s failed: %s', row['id'], error)
                    with self.store.transaction() as cursor:
                        cursor.execute('UPDATE schedules SET last_error=? WHERE id=?', (str(error), row['id']))
                        if isinstance(error, PermissionError):
                            cursor.execute('UPDATE schedules SET cancelled_at=? WHERE id=?', (now, row['id']))
        await self.agents.calls(scan)
        if self.runtime.sender is not None:
            self.runtime.sender.wake()

    def fire(self, identifier, now):
        with self.store.transaction() as cursor:
            row = cursor.execute('''SELECT * FROM schedules WHERE id=? AND cancelled_at IS NULL AND next_at<=?
                FOR UPDATE SKIP LOCKED''', (identifier, now)).fetchone()
            if row is None:
                return
            source = json.loads(row['source_json'])
            endpoint, actor = self.agents.check(cursor, source)
            key = f"schedule:{row['id']}:{row['next_at']}"
            if row['kind'] == 'routine':
                self.agents.store.submit(cursor, kind=self.agents.kind, key=key, source=source,
                    task=row['text'], title=row['text'][:80], profile=source['profile'])
            else:
                nodes = [TextNode(1, '提醒：' + row['text'])]
                if endpoint.kind == 'group':
                    nodes.insert(0, MentionNode(0, actor.native_user_id, ''))
                self.runtime.outbox.enqueue(self.agents.token,
                    rendered=render_plan(MessageBody(tuple(nodes)), endpoint, allowed_mentions=frozenset({actor})),
                    requester=actor, logical_key=key, source_scope_key=row['scope_key'], purpose='background_result',
                    source_native_message_id='', source_canonical_message_id=source['canonical_message_id'],
                    expires_at=now + min(self.runtime.outbox.max_wait_seconds, 86400), transaction_cursor=cursor)
                self.agents.app.usage_store.record(scope_key=row['scope_key'], source='reminder', provider='qq-official',
                    model='', input_tokens=0, output_tokens=0, occurred_at=now, transaction_cursor=cursor)
            due = next_time(cron=row['cron'], now=now) if row['cron'] else None
            cursor.execute("UPDATE schedules SET next_at=?,last_error='' WHERE id=?", (due, identifier))
