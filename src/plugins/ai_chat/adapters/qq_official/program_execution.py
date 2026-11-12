"""Claim and finish a wake without replaying an interrupted model invocation."""
import json
import secrets

from .lease import assert_epoch
from .program_actions import ProgramDenied
from .program_delivery import rendered_say, say
from .vm_sessions import source


class ProgramExecution:
    def __init__(self, wakes):
        self.wakes,self.actions = wakes,wakes.actions
        self.transaction,self.clock = wakes.transaction,wakes.clock
        self.token = self.actions.sessions.lease

    def pending(self):
        with self.transaction() as cursor:
            assert_epoch(cursor,self.token)
            return [dict(row) for row in cursor.execute('''SELECT w.*,
                (SELECT conversation_generation FROM qq_official_program_actions a WHERE a.wake_id=w.wake_id LIMIT 1) AS conversation_generation
                FROM qq_official_program_wakes w WHERE account_key=? AND (state='queued' AND due_at<=? OR state='answered')
                ORDER BY due_at,wake_id LIMIT 64''',(self.token.account_key,int(self.clock()))).fetchall()]

    def recover(self):
        with self.transaction() as cursor:
            assert_epoch(cursor,self.token)
            cursor.execute("""UPDATE qq_official_program_wakes SET state='ambiguous',
                error_code='program_wake_interrupted',finished_at=? WHERE account_key=? AND state='running'""",
                (int(self.clock()),self.token.account_key))

    def claim(self, wake_id):
        with self.transaction() as cursor:
            assert_epoch(cursor,self.token)
            row = cursor.execute('SELECT * FROM qq_official_program_wakes WHERE wake_id=? AND account_key=?',
                (wake_id,self.token.account_key)).fetchone()
            if row is None or row['state']!='queued' or row['due_at']>int(self.clock()):
                return None
            live = self.wakes.live_inputs(cursor,wake_id)
            if not live:
                cursor.execute("UPDATE qq_official_program_wakes SET state='cancelled',error_code='program_wake_no_live_inputs',finished_at=? WHERE wake_id=?",
                    (int(self.clock()),wake_id))
                return None
            if not row['funded']:
                self.wakes.activate(cursor,source(live[0]))
                if not cursor.execute('SELECT funded FROM qq_official_program_wakes WHERE wake_id=?',(wake_id,)).fetchone()['funded']:
                    return None
            return dict(cursor.execute("""UPDATE qq_official_program_wakes SET state='running',run_id=?,
                input_ids_json=?,started_at=? WHERE wake_id=? RETURNING *""",
                (secrets.token_hex(16),json.dumps([item['action_id'] for item in live]),int(self.clock()),wake_id)).fetchone())

    def check(self, cursor, run):
        assert_epoch(cursor,self.token)
        row = cursor.execute('SELECT * FROM qq_official_program_wakes WHERE wake_id=? AND account_key=?',
            (run['wake_id'],self.token.account_key)).fetchone()
        if row is None or row['run_id']!=run['run_id'] or row['state'] not in {'running','answered'}:
            raise ProgramDenied('program_wake_replaced')
        ids = json.loads(row['input_ids_json'])
        inputs = []
        for action_id in ids:
            item = cursor.execute('SELECT * FROM qq_official_program_actions WHERE action_id=? AND wake_id=?',
                (action_id,row['wake_id'])).fetchone()
            if (item is None or item['state'] not in {'queued','coalesced'}
                    or item['endpoint_key']!=row['endpoint_key'] or item['kind']!='wake'):
                raise ProgramDenied('program_wake_input_replaced')
            self.actions.check(cursor,item)
            inputs.append(dict(item))
        if not inputs:
            raise ProgramDenied('program_wake_no_live_inputs')
        return dict(row),inputs

    def current(self, run):
        with self.transaction() as cursor:
            return self.check(cursor,run)

    def answer(self, run, text):
        with self.transaction() as cursor:
            row,inputs = self.check(cursor,run)
            if row['state']!='running':
                raise ProgramDenied('program_wake_already_answered',409)
            if text!='[silence]':
                rendered_say(source(inputs[0]).endpoint,text)
            cursor.execute("UPDATE qq_official_program_wakes SET answer_text=?,state='answered' WHERE wake_id=?",(text,row['wake_id']))

    def publish(self, run):
        with self.transaction() as cursor:
            row,inputs = self.check(cursor,run)
            if row['state']!='answered':
                raise ProgramDenied('program_wake_not_answered',409)
            if row['reply_action_id'] is not None:
                return row['reply_action_id']
            if row['answer_text']=='[silence]':
                cursor.execute("UPDATE qq_official_program_wakes SET state='completed',finished_at=? WHERE wake_id=?",(int(self.clock()),row['wake_id']))
                return None
            first = inputs[0]
            action,_ = say(self.actions,source(first),generation=first['conversation_generation'],program=first['program_name'],
                request_id='host:wake:'+row['run_id'],text=row['answer_text'],transaction_cursor=cursor)
            cursor.execute('UPDATE qq_official_program_wakes SET reply_action_id=? WHERE wake_id=?',(action,row['wake_id']))
            return action

    def finish(self, run, state, error):
        if state not in {'failed','cancelled','ambiguous'}:
            raise ValueError('invalid_wake_terminal_state')
        with self.transaction() as cursor:
            assert_epoch(cursor,self.token)
            cursor.execute("""UPDATE qq_official_program_wakes SET state=?,error_code=?,finished_at=?
                WHERE wake_id=? AND account_key=? AND run_id=? AND state IN ('running','answered')""",
                (state,error,int(self.clock()),run['wake_id'],self.token.account_key,run['run_id']))

    def settle_delivery(self, run):
        with self.transaction() as cursor:
            assert_epoch(cursor,self.token)
            row = cursor.execute('''SELECT d.status,d.wait_reason FROM qq_official_program_wakes w
                JOIN qq_official_program_actions a ON a.action_id=w.reply_action_id JOIN deliveries d ON d.plan_id=a.plan_id
                WHERE w.wake_id=? AND w.account_key=? AND w.run_id=? AND d.part_index=0''',
                (run['wake_id'],self.token.account_key,run['run_id'])).fetchone()
            if row and row['status'] in {'committed','failed','cancelled','ambiguous'}:
                cursor.execute('UPDATE qq_official_program_wakes SET state=?,error_code=?,finished_at=? WHERE wake_id=?',
                    ('completed' if row['status']=='committed' else row['status'],row['wait_reason'] or '',int(self.clock()),run['wake_id']))
                return True
            return False


def check_reply(cursor, actions, action):
    """A merged answer must remain authorized by EVERY included program."""
    row = cursor.execute('SELECT * FROM qq_official_program_wakes WHERE reply_action_id=?',(action['action_id'],)).fetchone()
    if row is not None:
        from .program_wakes import ProgramWakes
        _,inputs = ProgramExecution(ProgramWakes(actions)).check(cursor,row)
        if (action['endpoint_key']!=row['endpoint_key'] or action['request_id']!='host:wake:'+row['run_id']
                or action['program_name']!=inputs[0]['program_name']
                or json.loads(action['payload_json']).get('text')!=row['answer_text']):
            raise ProgramDenied('program_wake_source_mismatch')
