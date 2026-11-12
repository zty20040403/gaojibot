"""Host-issued VM file provenance; no human principal, turn or job is invented."""
from types import SimpleNamespace
import json

from .file_artifacts import SourceBinding, _issue
from .identity_store import snapshot
from .program_actions import ProgramActions, ProgramDenied
from .vm_sessions import VMSessions


def load(cursor, actions, action_id):
    row = cursor.execute('SELECT * FROM qq_official_program_actions WHERE action_id=?',(action_id,)).fetchone()
    if row is None or row['kind']!='send_file' or row['state'] in {'cancelled','failed','ambiguous'}:
        raise ProgramDenied('program_file_source_unavailable')
    current = actions.check(cursor,row)
    return row,current


def assert_artifact(cursor, *, token, artifact, admission, now):
    actions = ProgramActions(VMSessions(None,token,private_admission=admission,clock=lambda:now))
    row,current = load(cursor,actions,artifact['source_program_action_id'])
    if (artifact['source_extension_kind']!='program' or artifact['account_key']!=token.account_key
            or artifact['endpoint_key']!=current.endpoint.key or artifact['source_scope_key']!=current.scope_key
            or artifact['requester_actor_key']!=snapshot(cursor,current.endpoint.account).bot_actor.key
            or artifact['conversation_generation']!=row['conversation_generation']
            or artifact['endpoint_generation']!=row['endpoint_generation'] or artifact['route_epoch']!=row['route_epoch']
            or artifact['pending_deadline_at']!=row['expires_at']
            or artifact['filename']!=json.loads(row['payload_json']).get('filename')
            or any(artifact.get(key) is not None for key in ('source_execution_id','source_task_id',
                'source_canonical_message_id','source_native_message_id','source_job_id','source_migration_item_id'))):
        raise ProgramDenied('program_file_source_mismatch')
    return row


class FileSource:
    kind = 'program'

    def __init__(self, actions, action_id):
        self.actions,self.action_id = actions,action_id

    def current_source(self, cursor, binding, now):
        row,current = load(cursor,self.actions,self.action_id)
        if (binding.token is not self.actions.sessions.lease or binding.endpoint!=current.endpoint
                or binding.actor!=snapshot(cursor,current.endpoint.account).bot_actor
                or binding.source_key!='program-file:'+self.action_id or binding.deadline_at!=row['expires_at']):
            raise ProgramDenied('program_file_source_mismatch')
        return dict(source_scope_key=current.scope_key,source_canonical_message_id=None,source_native_message_id=None,
            source_program_action_id=self.action_id,endpoint_generation=row['endpoint_generation'],route_epoch=row['route_epoch'])

    def bind_artifact(self, cursor, binding, fields, *, now, existing):
        self.current_source(cursor,binding,now)
        assert_artifact(cursor,token=binding.token,artifact=fields,admission=binding.admission,now=now)
        cursor.execute("UPDATE qq_official_program_actions SET state='queued' WHERE action_id=?",(self.action_id,))


def issue(cursor, actions, action_id):
    row,current = load(cursor,actions,action_id)
    return _issue(SourceBinding(actions.sessions.lease,SimpleNamespace(job_id=None,lease_generation=1),
        actions.sessions.private_admission,None,None,row['expires_at'],current.endpoint,
        snapshot(cursor,current.endpoint.account).bot_actor,None,'program-file:'+action_id,row['expires_at'],None,
        extension=FileSource(actions,action_id)))
