"""Durable wake admission: six per hour, then one bounded next-hour batch."""
from contextlib import nullcontext
import hashlib
import json
import secrets

from .program_actions import ProgramDenied, settings

MAX_BATCH_COUNT, MAX_BATCH_BYTES = 64, 65536


def allocate(cursor, current, generation, now, encoded, *, deferred):
    hour = now//3600
    key = hashlib.sha256(json.dumps([current.endpoint.key,current.token_hash,current.account_epoch,
        current.token_generation,current.endpoint_generation,current.route_epoch,generation,hour]).encode()).hexdigest() if deferred else None
    wake = cursor.execute('SELECT * FROM qq_official_program_wakes WHERE batch_key=?',(key,)).fetchone() if key else None
    if wake is None:
        wake = cursor.execute('''INSERT INTO qq_official_program_wakes
            (wake_id,account_key,endpoint_key,batch_key,due_at,funded,created_at) VALUES (?,?,?,?,?,?,?) RETURNING *''',
            (secrets.token_hex(16),current.endpoint.account.key,current.endpoint.key,key,
             (hour+1)*3600 if deferred else now,not deferred,now)).fetchone()
    size = cursor.execute('''SELECT COUNT(*) AS n,COALESCE(SUM(octet_length(payload_json)),0) AS bytes
        FROM qq_official_program_actions WHERE wake_id=?''',(wake['wake_id'],)).fetchone()
    if wake['state']!='queued' or size['n']>=MAX_BATCH_COUNT or size['bytes']+len(encoded.encode())>MAX_BATCH_BYTES:
        raise ProgramDenied('program_wake_buffer_full',429)
    return dict(wake_id=wake['wake_id'],state='coalesced' if deferred else 'queued',expires_at=wake['due_at']+300)


class ProgramWakes:
    def __init__(self, actions):
        self.actions,self.transaction,self.clock = actions,actions.transaction,actions.clock

    def live_inputs(self, cursor, wake_id):
        rows = cursor.execute('''SELECT * FROM qq_official_program_actions WHERE wake_id=?
            AND state IN ('queued','coalesced') ORDER BY created_at,action_id''',(wake_id,)).fetchall()
        live = []
        for row in rows:
            try:
                self.actions.check(cursor,row)
                live.append(dict(row))
            except PermissionError as error:
                cursor.execute("UPDATE qq_official_program_actions SET state='cancelled',error_code=? WHERE action_id=?",
                    (str(error),row['action_id']))
        return live

    def activate(self, cursor, current):
        """Give an owed next-hour batch its slot before admitting new requests."""
        now = int(self.clock())
        batches = cursor.execute('''SELECT * FROM qq_official_program_wakes WHERE endpoint_key=?
            AND state='queued' AND NOT funded AND due_at<=? ORDER BY due_at,wake_id LIMIT 64''',
            (current.endpoint.key,now)).fetchall()
        for wake in batches:
            live = self.live_inputs(cursor,wake['wake_id'])
            if not live:
                cursor.execute("UPDATE qq_official_program_wakes SET state='cancelled',error_code='program_wake_no_live_inputs',finished_at=? WHERE wake_id=?",
                    (now,wake['wake_id']))
                continue
            limit = settings(cursor,current.scope_key,live[0]['program_name'])['program_wake_limit']
            used = cursor.execute("SELECT used FROM program_quota WHERE scope_key=? AND kind='wake' AND hour=?",
                (current.scope_key,now//3600)).fetchone()
            if (used['used'] if used else 0)>=limit:
                continue
            cursor.execute("""INSERT INTO program_quota(scope_key,kind,hour,used) VALUES (?,'wake',?,1)
                ON CONFLICT(scope_key,kind,hour) DO UPDATE SET used=program_quota.used+1""",(current.scope_key,now//3600))
            cursor.execute('UPDATE qq_official_program_wakes SET funded=TRUE WHERE wake_id=?',(wake['wake_id'],))
            cursor.execute("UPDATE qq_official_program_actions SET state='queued' WHERE wake_id=? AND state='coalesced'",(wake['wake_id'],))

    def submit(self, current, *, generation, program, request_id, text, source_command_id=None, transaction_cursor=None):
        if type(text) is not str or not text.strip() or len(text)>4000:
            raise ProgramDenied('invalid_program_text',400)
        with (nullcontext(transaction_cursor) if transaction_cursor is not None else self.transaction()) as cursor:
            self.actions.sessions.check(cursor,current)
            if generation!=self.actions._capture(cursor,current):
                raise ProgramDenied('conversation_stopped_or_replaced')
            self.activate(cursor,current)
            result = self.actions.reserve(current,generation=generation,kind='wake',program=program,
                request_id=request_id,payload={'text':text},transaction_cursor=cursor,queue_wake=True,source_command_id=source_command_id)
            if source_command_id is not None:
                cursor.execute('UPDATE qq_official_background_commands SET notification_action_id=? WHERE command_id=?',
                    (result[0]['action_id'],source_command_id))
            return result
