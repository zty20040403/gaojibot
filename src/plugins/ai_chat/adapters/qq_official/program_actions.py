"""Persist program intent without a human principal; no HTTP routes here yet."""
from contextlib import nullcontext
import hashlib
import json
import re
import secrets

from .conversation_control import permitted,state
from .vm_sessions import source

NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,79}')
REQUEST = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')


class ProgramDenied(PermissionError):
    def __init__(self, code, status=403):
        super().__init__(code)
        self.status = status


def settings(cursor, scope_key, program):
    row = cursor.execute('''SELECT programs_enabled,program_say_limit,program_wake_limit
        FROM conversation_settings WHERE scope_key=?''',(scope_key,)).fetchone()
    values = dict(row) if row else dict(programs_enabled=True,program_say_limit=60,program_wake_limit=6)
    unit = cursor.execute('SELECT enabled FROM program_controls WHERE scope_key=? AND program_name=?',
        (scope_key,program)).fetchone()
    if not values['programs_enabled'] or unit is not None and not unit['enabled']:
        raise ProgramDenied('program_disabled')
    return values


class ProgramActions:
    def __init__(self, sessions):
        self.sessions = sessions
        self.transaction,self.clock = sessions.transaction,sessions.clock

    def capture(self, current):
        with self.transaction() as cursor:
            self.sessions.check(cursor,current)
            generation = state(cursor,current.endpoint)['stop_generation']
            if not permitted(cursor,current.endpoint,generation=generation):
                raise ProgramDenied('conversation_stopped_or_replaced')
            return generation

    def check(self, cursor, row):
        """A short transactional fence; never hold it across model/QQ/VM I/O."""
        current = source(row)
        self.sessions.check(cursor,current)
        if (row['scope_key']!=current.scope_key or not permitted(cursor,current.endpoint,
                generation=row['conversation_generation'])):
            raise ProgramDenied('conversation_stopped_or_replaced')
        settings(cursor,current.scope_key,row['program_name'])
        if int(self.clock())>=row['expires_at']:
            raise ProgramDenied('program_action_expired',409)
        return current

    def reserve(self, current, *, generation, kind, program, request_id, payload, transaction_cursor=None):
        if (kind not in {'say','wake','send_file'} or type(program) is not str or NAME.fullmatch(program) is None
                or type(request_id) is not str or REQUEST.fullmatch(request_id) is None
                or type(payload) is not dict or type(generation) is not int or generation<0):
            raise ValueError('invalid_program_action')
        try:
            encoded = json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
            if len(encoded.encode())>16384:
                raise ValueError()
            intent = hashlib.sha256(json.dumps([kind,program,encoded],ensure_ascii=False).encode()).hexdigest()
        except (ValueError,TypeError,UnicodeError):
            raise ValueError('invalid_program_action') from None
        now,scope = int(self.clock()),current.scope_key
        with (nullcontext(transaction_cursor) if transaction_cursor is not None else self.transaction()) as cursor:
            self.sessions.check(cursor,current)
            if not permitted(cursor,current.endpoint,generation=generation):
                raise ProgramDenied('conversation_stopped_or_replaced')
            limits = settings(cursor,scope,program)
            row = cursor.execute('SELECT * FROM qq_official_program_actions WHERE endpoint_key=? AND request_id=?',
                (current.endpoint.key,request_id)).fetchone()
            if row is not None:
                if row['intent_digest']!=intent:
                    raise ProgramDenied('program_request_conflict',409)
                self.check(cursor,row)
                return dict(row),False
            quota_kind = 'wake' if kind=='wake' else 'say'
            used = cursor.execute('SELECT used FROM program_quota WHERE scope_key=? AND kind=? AND hour=?',
                (scope,quota_kind,now//3600)).fetchone()
            if (used['used'] if used else 0)>=limits['program_'+quota_kind+'_limit']:
                raise ProgramDenied('program_rate_limited',429)
            cursor.execute('''INSERT INTO program_quota(scope_key,kind,hour,used) VALUES (?,?,?,1)
                ON CONFLICT(scope_key,kind,hour) DO UPDATE SET used=program_quota.used+1''',(scope,quota_kind,now//3600))
            fields = dict(action_id=secrets.token_hex(16),endpoint_key=current.endpoint.key,scope_key=scope,
                request_id=request_id,program_name=program,kind=kind,payload_json=encoded,intent_digest=intent,
                sandbox_id=current.sandbox_id,boot_id=current.boot_id,token_hash=current.token_hash,
                token_generation=current.token_generation,account_epoch=current.account_epoch,
                endpoint_generation=current.endpoint_generation,route_epoch=current.route_epoch,
                conversation_generation=generation,created_at=now,expires_at=now+300)
            row = cursor.execute('INSERT INTO qq_official_program_actions ('+','.join(fields)+') VALUES ('+
                ','.join('?' for _ in fields)+') RETURNING *',tuple(fields.values())).fetchone()
            return dict(row),True

    def get(self, current, action_id):
        """A scoped read can inspect past failures even while stopped or rotated."""
        with self.transaction() as cursor:
            self.sessions.check(cursor,current)
            row = cursor.execute('''SELECT action_id,request_id,program_name,kind,state,error_code,plan_id,created_at
                FROM qq_official_program_actions WHERE endpoint_key=? AND action_id=?''',
                (current.endpoint.key,action_id)).fetchone()
            if row is None:
                raise ProgramDenied('program_action_not_found',404)
            return dict(row)
