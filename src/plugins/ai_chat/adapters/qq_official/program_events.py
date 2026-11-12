"""Stable, physical-scope cursors for the VM's visible chat event stream."""
from datetime import datetime
import json
from zoneinfo import ZoneInfo

from ...ledger import MessageLedger

SHANGHAI = ZoneInfo('Asia/Shanghai')


class ProgramEvents:
    def __init__(self, sessions):
        self.sessions = sessions

    def _check(self, cursor, current):
        self.sessions.check(cursor,current)
        # The ledger takes this lock BEFORE allocating a canonical id. Holding
        # it through the snapshot prevents a lower, uncommitted id being lost
        # when a reader advances its cursor past a later committed message.
        cursor.execute('SELECT pg_advisory_xact_lock(hashtext(?))',('ledger:'+current.scope_key,))

    def head(self, current):
        with self.sessions.transaction() as cursor:
            self._check(cursor,current)
            row = cursor.execute('''SELECT COALESCE(MAX(m.canonical_message_id),0) AS head
                FROM messages m JOIN conversations c USING(conversation_id)
                WHERE c.scope_key=? AND m.source_endpoint_key=?''',(current.scope_key,current.endpoint.key)).fetchone()
            return row['head']

    def page(self, current, after, *, limit=100):
        if type(after) is not int or not 0<=after<2**63 or type(limit) is not int or not 1<=limit<=100:
            raise ValueError('invalid_program_event_cursor')
        with self.sessions.transaction() as cursor:
            self._check(cursor,current)
            rows = cursor.execute('''SELECT m.*,c.scope_key,
                CASE WHEN r.source_endpoint_key=m.source_endpoint_key AND r.conversation_id=m.conversation_id
                    AND r.canonical_message_id>=v.min_canonical_message_id AND r.message_kind<>'command'
                    THEN r.canonical_message_id END AS visible_reply
                FROM messages_with_captions m JOIN conversations c USING(conversation_id)
                JOIN conversation_visibility v USING(conversation_id)
                LEFT JOIN messages r ON r.canonical_message_id=m.reply_to_canonical_message_id
                WHERE c.scope_key=? AND m.source_endpoint_key=? AND m.canonical_message_id>?
                    AND m.canonical_message_id>=v.min_canonical_message_id AND m.message_kind<>'command'
                ORDER BY m.canonical_message_id LIMIT ?''',(current.scope_key,current.endpoint.key,after,limit)).fetchall()
            return [self.render(row) for row in rows]

    @staticmethod
    def render(row):
        message = MessageLedger._row_to_message(row)
        result = dict(id=message.canonical_message_id,at=datetime.fromtimestamp(message.occurred_at,SHANGHAI).isoformat(),
            sender=message.sender_display,person=message.sender_principal_id,
            reply_to=row['visible_reply'],text=message.prompt_text)
        if message.direction=='outbound':
            provenance = json.loads(row['raw_event_json'] or '{}')
            result['by'] = 'program' if isinstance(provenance,dict) and provenance.get('by')=='program' else 'bot'
        return result
