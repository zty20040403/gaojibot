"""Persistent one-shot VM commands; neither a human identity nor a model job."""
import hashlib
import json
import secrets

from .conversation_control import permitted, state
from .program_actions import NAME, REQUEST, ProgramDenied, settings
from .vm_sessions import source


def command_generation(cursor, endpoint):
    row = cursor.execute('SELECT command_generation FROM conversation_settings WHERE scope_key=?',(endpoint.scope.key,)).fetchone()
    return row['command_generation'] if row else 0


def cancel_commands(cursor, endpoint, now, *, command_id=None):
    """Pending work never reached the VM; other work retains its slot until stopped."""
    where,args = (' AND command_id=?',(command_id,)) if command_id is not None else (' AND finished_at IS NULL',())
    rows = cursor.execute('''UPDATE qq_official_background_commands SET cancel_requested=TRUE,
        state=CASE WHEN state='pending' THEN 'cancelled' WHEN finished_at IS NULL THEN 'stopping' ELSE state END,
        finished_at=CASE WHEN state='pending' THEN ? ELSE finished_at END,
        error_code=CASE WHEN finished_at IS NULL THEN 'command_cancel_requested' ELSE error_code END
        WHERE endpoint_key=? AND NOT cancel_requested'''+where+' RETURNING command_id',
        (now,endpoint.key,*args)).fetchall()
    return [row['command_id'] for row in rows]


def check_notification(cursor, sessions, command_id, *, current, program):
    row = cursor.execute('SELECT * FROM qq_official_background_commands WHERE command_id=?',(command_id,)).fetchone()
    if row is None:
        raise ProgramDenied('background_command_not_found',404)
    origin = BackgroundCommands(sessions).check(cursor,row)
    if (origin!=current or program!=row['program_name'] or row['finished_at'] is None
            or row['state'] not in {'completed','failed'}):
        raise ProgramDenied('background_command_notification_unavailable')
    return row


class BackgroundCommands:
    def __init__(self, sessions):
        self.sessions = sessions
        self.transaction,self.clock = sessions.transaction,sessions.clock

    def _capture(self, cursor, current):
        generation = state(cursor,current.endpoint)['stop_generation']
        if not permitted(cursor,current.endpoint,generation=generation):
            raise ProgramDenied('conversation_stopped_or_replaced')
        return generation,command_generation(cursor,current.endpoint)

    def authenticate(self, token):
        with self.transaction() as cursor:
            current = self.sessions.authenticate_cursor(cursor,token)
            return current,self._capture(cursor,current)

    def capture(self, current):
        with self.transaction() as cursor:
            self.sessions.check(cursor,current)
            return self._capture(cursor,current)

    def require_generation(self, current, generation):
        with self.transaction() as cursor:
            self.sessions.check(cursor,current)
            if self._capture(cursor,current)!=generation:
                raise ProgramDenied('background_command_generation_changed')

    def check(self, cursor, row):
        current = source(row)
        self.sessions.check(cursor,current)
        live = cursor.execute('SELECT * FROM qq_official_background_commands WHERE command_id=? FOR UPDATE',(row['command_id'],)).fetchone()
        if (live is None or source(live)!=current or live['intent_digest']!=row['intent_digest']
                or live['scope_key']!=current.scope_key or live['cancel_requested'] or
                self._capture(cursor,current)!=(live['conversation_generation'],live['command_generation'])):
            raise ProgramDenied('background_command_cancelled_or_replaced')
        settings(cursor,current.scope_key,row['program_name'])
        return current

    def reserve(self, current, *, generation, program, request_id, argv):
        if (type(generation) is not tuple or len(generation)!=2 or any(type(value) is not int or value<0 for value in generation)
                or type(program) is not str or NAME.fullmatch(program) is None
                or type(request_id) is not str or REQUEST.fullmatch(request_id) is None
                or type(argv) is not list or not 1<=len(argv)<=128
                or any(type(value) is not str or '\0' in value for value in argv) or not argv[0]):
            raise ProgramDenied('invalid_background_command',400)
        try:
            encoded = json.dumps(argv,ensure_ascii=False,separators=(',',':'))
            if len(encoded.encode())>16384:
                raise ValueError()
            digest = hashlib.sha256(json.dumps([program,encoded]).encode()).hexdigest()
        except (ValueError,UnicodeError):
            raise ProgramDenied('invalid_background_command',400) from None
        with self.transaction() as cursor:
            self.sessions.check(cursor,current)
            if self._capture(cursor,current)!=generation:
                raise ProgramDenied('background_command_generation_changed')
            settings(cursor,current.scope_key,program)
            row = cursor.execute('SELECT * FROM qq_official_background_commands WHERE endpoint_key=? AND request_id=?',
                (current.endpoint.key,request_id)).fetchone()
            if row is not None:
                if row['intent_digest']!=digest:
                    raise ProgramDenied('background_command_request_conflict',409)
                self.check(cursor,row)
                return dict(row),False
            count = cursor.execute('SELECT COUNT(*) AS n FROM qq_official_background_commands WHERE endpoint_key=? AND finished_at IS NULL',
                (current.endpoint.key,)).fetchone()['n']
            if count>=8:
                raise ProgramDenied('background_command_limit',429)
            now = int(self.clock())
            fields = dict(command_id=secrets.token_hex(16),endpoint_key=current.endpoint.key,scope_key=current.scope_key,
                request_id=request_id,program_name=program,argv_json=encoded,intent_digest=digest,
                sandbox_id=current.sandbox_id,boot_id=current.boot_id,token_hash=current.token_hash,
                token_generation=current.token_generation,account_epoch=current.account_epoch,
                endpoint_generation=current.endpoint_generation,route_epoch=current.route_epoch,
                conversation_generation=generation[0],command_generation=generation[1],created_at=now,deadline_at=now+21600)
            row = cursor.execute('INSERT INTO qq_official_background_commands ('+','.join(fields)+') VALUES ('+
                ','.join('?' for _ in fields)+') RETURNING *',tuple(fields.values())).fetchone()
            return dict(row),True

    def cancel(self, current, command_id):
        with self.transaction() as cursor:
            self.sessions.check(cursor,current)
            return bool(cancel_commands(cursor,current.endpoint,int(self.clock()),command_id=command_id))

    def get(self, current, identifier, *, by_request=False):
        with self.transaction() as cursor:
            self.sessions.check(cursor,current)
            row = cursor.execute('''SELECT command_id,request_id,program_name,state,cancel_requested,
                created_at,deadline_at,started_at,finished_at,exit_code,elapsed_seconds,output_bytes,error_code,notification_error,notification_action_id
                FROM qq_official_background_commands WHERE endpoint_key=? AND '''+
                ('request_id' if by_request else 'command_id')+'=?',(current.endpoint.key,identifier)).fetchone()
            if row is None:
                raise ProgramDenied('background_command_not_found',404)
            return dict(row,unit='gaoji-bg-'+row['command_id']+'.service',
                output_path='/work/.gaoji/out/'+row['command_id']+'.txt')
