"""Exact original foreground authority for end-of-turn snapshots only.

IO admission records this provenance in its own transaction. Recovery neither
selects a replacement grant nor impersonates a running foreground/job. These
host-only providers are never installed in a ToolContext or model session.
"""
from types import SimpleNamespace

from .project_authorization import ForegroundProjectProvider, official_project
from .project_background import _digest, _source_execution
from .adapters.qq_official.project_authority import Denied, Operation, ProjectGrantRef
from .adapters.qq_official.lease import assert_epoch
from .platform_types import EndpointKey, ActorKey
from .conversation_scope import ConversationScope


def _io_digest(row):
    return _digest({key: row[key] for key in ('operation_id','project_id',
        'actor_principal_id','request_key','tool_name','arguments_sha256',
        'sandbox_id','generation','fence','mutating','journal_turn_id','created_at')})


def capture_io(cursor, access, project, io, *, now):
    """Same transaction as an official foreground's mutating IO reservation."""
    proof=official_project(access)
    if proof is None or not io['mutating']:
        return
    provider=proof.provider
    if type(provider) is not ForegroundProjectProvider:
        raise Denied()
    provider.require(cursor,proof,project_id=project['project_id'],operation=Operation.WRITE)
    source,fingerprint=_source_execution(cursor,provider.handle.execution_id)
    grant,scope=provider._pin(cursor,project['project_id'])
    grant_row=cursor.execute('SELECT expires_at FROM qq_official_project_grants WHERE grant_id=?',
        (grant.grant_id,)).fetchone()
    if (source['journal_turn_id'] != io['journal_turn_id'] or source['state']!='executing'
            or source['execution_kind']!='chat' or scope.key!=project['scope_key']
            or grant_row is None or io['actor_principal_id']!=access.principal_id):
        raise Denied()
    deadline=min(now+3600,grant_row['expires_at'])
    if deadline<=now:raise Denied()
    cursor.execute('''INSERT INTO qq_official_project_io_authority
        (operation_id,project_id,turn_id,source_execution_id,source_job_id,source_job_generation,
         source_revision,source_digest,io_digest,grant_id,grant_revision,account_key,endpoint_key,
         actor_key,principal_id,resource_scope_key,endpoint_generation,endpoint_route_epoch,
         route_epoch,created_at,deadline_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (io['operation_id'],project['project_id'],io['journal_turn_id'],source['execution_id'],
         source['job_id'],source['job_lease_generation'],source['execution_revision'],fingerprint,
         _io_digest(io),grant.grant_id,grant.revision,provider.token.account_key,provider.endpoint.key,
         provider.actor.key,access.principal_id,scope.key,source['endpoint_generation'],
         source['route_epoch'],provider.route_epoch,now,deadline))


def _source(cursor, binding, *, now):
    source,fingerprint=_source_execution(cursor,binding['source_execution_id'])
    io=cursor.execute('SELECT * FROM project_io WHERE operation_id=?',
        (binding['operation_id'],)).fetchone()
    turn=cursor.execute('SELECT * FROM agent_turns WHERE turn_id=?',(binding['turn_id'],)).fetchone()
    if (now>=binding['deadline_at'] or fingerprint!=binding['source_digest']
            or source['execution_revision']!=binding['source_revision']
            or source['job_id']!=binding['source_job_id']
            or source['job_lease_generation']!=binding['source_job_generation']
            or source['journal_turn_id']!=binding['turn_id']
            or source['execution_kind']!='chat' or source['state'] not in {'result_ready','handed_off'}
            or source['result_status'] not in {'succeeded','silence'}
            or io is None or _io_digest(io)!=binding['io_digest']
            or turn is None or turn['status'] not in {'succeeded','silence'}
            or turn['finished_at'] is None or turn['scope_key']!=source['scope_key']
            or turn['trigger_canonical_message_id']!=source['canonical_message_id']):
        raise Denied()
    return source,io


def validate_lifecycle_io(cursor, binding, *, now):
    """Recheck the immutable IO parent of an existing snapshot job."""
    operation=binding.get('source_io_operation_id')
    if operation is None:return
    original=cursor.execute('SELECT * FROM qq_official_project_io_authority WHERE operation_id=?',
        (operation,)).fetchone()
    if original is None:raise Denied()
    for key in ('project_id','source_execution_id','grant_id','grant_revision','account_key',
                'endpoint_key','actor_key','principal_id','endpoint_generation','endpoint_route_epoch',
                'route_epoch','source_digest','source_revision'):
        if original[key]!=binding[key]:raise Denied()
    source,io=_source(cursor,original,now=now)
    run=cursor.execute('SELECT plan FROM project_lifecycle_runs WHERE job_id=?',
        (binding['job_id'],)).fetchone()
    if (binding['action']!='snapshot' or binding['deadline_at']>original['deadline_at']
            or io['state']!='completed' or run is None
            or run['plan'].get('source_turn_id')!=original['turn_id']):
        raise Denied()


class AutosaveProjectProvider(ForegroundProjectProvider):
    """A fixed successful terminal turn; only the existing autosave runner uses it."""
    @classmethod
    def load(cls,cursor,*,token,project_id,turn_id,admission,route_guard,project_scope_guard,clock):
        rows=cursor.execute('''SELECT a.* FROM qq_official_project_io_authority a
            JOIN project_io io USING(operation_id) WHERE a.project_id=? AND a.turn_id=?
            ORDER BY a.operation_id''',(project_id,turn_id)).fetchall()
        if not rows:raise Denied()
        binding=dict(rows[0])
        if binding['account_key']!=token.account_key:raise Denied()
        self=cls.__new__(cls)
        endpoint,actor=EndpointKey.parse(binding['endpoint_key']),ActorKey.parse(binding['actor_key'])
        handle=SimpleNamespace(endpoint=endpoint,actor=actor,route_epoch=binding['endpoint_route_epoch'],
            execution_id=binding['source_execution_id'])
        super(AutosaveProjectProvider,self).__init__(token=token,handle=handle,job=None,admission=admission,
            route_epoch=binding['route_epoch'],route_guard=route_guard,project_scope_guard=project_scope_guard,clock=clock)
        self.binding=binding
        self._current(cursor)
        return self

    def _current(self,cursor):
        assert_epoch(cursor,self.token)
        binding=self.binding;now=self._now()
        if self.route_guard(cursor,canonical_scope=self.endpoint.scope,endpoint=self.endpoint,
                            route_epoch=self.route_epoch,operation='execute') is not True:raise Denied()
        endpoint=cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=? FOR UPDATE',
            (self.endpoint.key,)).fetchone()
        if (endpoint is None or endpoint['generation']!=binding['endpoint_generation']
                or endpoint['route_epoch']!=binding['endpoint_route_epoch']
                or endpoint['membership_state']!='present' or endpoint['binding_state']=='revoked'
                or (self.endpoint.kind=='group' and endpoint['binding_state']!='active')
                or self.admission(cursor,self.endpoint,self.actor) is not True):raise Denied()
        source_guard=getattr(self.admission,'require_execution',None)
        if not callable(source_guard):raise Denied()
        source_guard(cursor,binding['source_execution_id'],endpoint=self.endpoint,actor=self.actor,operation='execute')
        current=cursor.execute('SELECT * FROM qq_official_project_io_authority WHERE operation_id=?',
            (binding['operation_id'],)).fetchone()
        if current is None or dict(current)!=binding:raise Denied()
        save=cursor.execute('SELECT * FROM project_turn_saves WHERE project_id=? AND turn_id=? FOR UPDATE',
            (binding['project_id'],binding['turn_id'])).fetchone()
        if save is None or save['state'] not in {'pending','queued'} or save['actor_principal_id']!=binding['principal_id']:
            raise Denied()
        rows=cursor.execute('''SELECT io.operation_id,a.* FROM project_io io LEFT JOIN
            qq_official_project_io_authority a USING(operation_id) WHERE io.project_id=?
            AND io.journal_turn_id=? AND io.mutating ORDER BY io.operation_id''',
            (binding['project_id'],binding['turn_id'])).fetchall()
        if not rows:raise Denied()
        stable=('project_id','turn_id','source_execution_id','source_job_id','source_job_generation',
            'source_revision','source_digest','grant_id','grant_revision','account_key','endpoint_key',
            'actor_key','principal_id','resource_scope_key','endpoint_generation','endpoint_route_epoch','route_epoch')
        for row in rows:
            if any(row[key]!=binding[key] for key in stable):raise Denied()
            _source(cursor,row,now=now)
        return binding

    def _pin(self,cursor,project_id):
        if project_id!=self.binding['project_id']:raise Denied()
        return ProjectGrantRef(self.binding['grant_id'],self.binding['grant_revision']),ConversationScope.from_key(self.binding['resource_scope_key'])

    def snapshot_source(self,cursor):
        """Choose confirmed mutation evidence, retaining all original checks.

        An earlier reserved operation may have been safely aborted. It remains
        in the validated operation set but cannot represent a saved mutation.
        """
        self._current(cursor)
        row=cursor.execute('''SELECT a.operation_id FROM qq_official_project_io_authority a
            JOIN project_io io USING(operation_id) WHERE a.project_id=? AND a.turn_id=?
            AND io.state='completed' ORDER BY a.operation_id LIMIT 1''',
            (self.binding['project_id'],self.binding['turn_id'])).fetchone()
        if row is None:raise Denied()
        return row['operation_id']

    def require(self,cursor,proof,*,project_id=None,operation=Operation.READ):
        if operation not in {Operation.READ,Operation.WRITE,Operation.EXECUTE}:raise Denied()
        from .adapters.qq_official.migration_types import MigrationDenied
        try:return super().require(cursor,proof,project_id=project_id,operation=operation)
        except MigrationDenied:raise Denied() from None

    def before_create(self,*args,**kwargs):raise Denied()
    def created(self,*args,**kwargs):raise Denied()
    def _recipient(self,*args,**kwargs):return False
    def _fresh(self,*args,**kwargs):return False
