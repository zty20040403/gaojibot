"""Persistent project lifecycle/task authority, never inferred from a creator.

The actual shared project executor remains the sole physical execution path.
This module has no queue, Bot/Event, native user conversion or network effects.
"""
from __future__ import annotations

from contextvars import ContextVar
from contextlib import contextmanager
import hashlib
import json
import threading
from types import SimpleNamespace

from src.plugins.ai_chat.project_authorization import (
    ForegroundProjectProvider, official_project, _deny,
)
from src.plugins.ai_chat.adapters.qq_official.project_authority import (
    Denied, Operation, ProjectGrantRef,
)
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch, LeaseLost
from src.plugins.ai_chat.platform_types import EndpointKey, ActorKey
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind


# Only a synchronous authorization composition installs this value.
# It is never retained by a provider, transaction, async task or returned proof.
_read_current = ContextVar("project_background_read_current", default=None)

def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _source_execution(cursor, execution_id):
    row = cursor.execute('SELECT * FROM qq_official_turn_executions WHERE execution_id=?',
                         (execution_id,)).fetchone()
    if row is None or row['state'] not in {'executing', 'result_ready', 'handed_off'}:
        raise Denied()
    if row['result_status'] not in {None, 'succeeded', 'silence'}:
        raise Denied()
    material = {key: row[key] for key in ('execution_id', 'execution_revision',
        'account_key', 'endpoint_key', 'requester_actor_key', 'inbox_id',
        'canonical_message_id', 'source_native_message_id', 'scope_key',
        'endpoint_generation', 'route_epoch', 'input_digest')}
    return dict(row), _digest(material)


def _source_task(cursor, task_id):
    row = cursor.execute('''SELECT t.*,c.revision AS control_revision,c.dispatch_json,
        d.submission_digest,d.endpoint_generation,d.route_epoch,d.execution_deadline_at
        FROM subagent_tasks t JOIN subagent_controls c USING(task_id)
        JOIN qq_official_subagent_dispatches d USING(task_id) WHERE t.task_id=?''',
        (task_id,)).fetchone()
    if (row is None or row['cancel_requested'] or row['status'] in {'failed', 'cancelled'}
            or row['requester_user_id'] is not None):
        raise Denied()
    material = {key: row[key] for key in ('task_id', 'account_key', 'endpoint_key',
        'requester_actor_key', 'scope_key', 'project_id', 'project_base_version',
        'trigger_message_id', 'control_revision', 'submission_digest',
        'endpoint_generation', 'route_epoch', 'execution_deadline_at')}
    return dict(row), _digest(material)


def bind_lifecycle(cursor, access, project, job, run, *, now):
    """Called in the existing lifecycle enqueue transaction, before publication."""
    proof = official_project(access)
    if proof is None:
        return
    provider = proof.provider
    provider.require(cursor, proof, project_id=project['project_id'], operation=Operation.EXECUTE)
    grant, scope = provider._pin(cursor, project['project_id'])
    grant_row = cursor.execute('SELECT * FROM qq_official_project_grants WHERE grant_id=?',
                               (grant.grant_id,)).fetchone()
    if scope.key != project['scope_key'] or grant_row is None:
        raise Denied()
    source_task_id = None
    source_io_operation_id = None
    from .project_autosave_authority import AutosaveProjectProvider
    if isinstance(provider, BackgroundProjectProvider):
        source_execution_id = provider.binding.get('source_execution_id')
        source_task_id = provider.binding.get('source_task_id')
        if provider.kind == 'task':
            source_execution_id, source_task_id = None, provider.binding_id
    elif type(provider) is ForegroundProjectProvider:
        source_execution_id = provider.handle.execution_id
    elif type(provider) is AutosaveProjectProvider:
        if (run['action']!='snapshot' or run['plan'].get('source_turn_id')!=provider.binding['turn_id']
                or run['request_key']!=f"turn-save:{provider.binding['turn_id']}"):
            raise Denied()
        source_execution_id=provider.binding['source_execution_id']
        source_io_operation_id=provider.snapshot_source(cursor)
    else:
        raise Denied()
    if source_task_id is not None:
        source, fingerprint = _source_task(cursor, source_task_id)
        revision = source['control_revision']
    else:
        source, fingerprint = _source_execution(cursor, source_execution_id)
        revision = source['execution_revision']
    deadline = min(now + 3600, grant_row['expires_at'])
    if source_io_operation_id is not None:
        deadline=min(deadline,provider.binding['deadline_at'])
    if source_task_id is not None:
        deadline = min(deadline, source['execution_deadline_at'])
    if deadline <= now:
        raise Denied()
    if (source['endpoint_key'] != provider.endpoint.key
            or source['requester_actor_key'] != provider.actor.key
            or source['account_key'] != provider.token.account_key):
        raise Denied()
    cursor.execute('''INSERT INTO qq_official_project_lifecycle_authority
        (job_id,project_id,grant_id,grant_revision,account_key,endpoint_key,actor_key,
         principal_id,endpoint_generation,endpoint_route_epoch,route_epoch,
         source_execution_id,source_task_id,source_revision,source_digest,action,deadline_at,created_at,
         source_io_operation_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (job.job_id,project['project_id'],grant.grant_id,grant.revision,provider.token.account_key,
         provider.endpoint.key,provider.actor.key,access.principal_id,
         source['endpoint_generation'],source['route_epoch'],provider.route_epoch,
         source_execution_id,source_task_id,revision,fingerprint,run['action'],deadline,now,source_io_operation_id))


class BackgroundProjectProvider(ForegroundProjectProvider):
    """Reconstruct only an exact persisted source/grant under the current job.

    Construction is not authority: issue/require always re-read every binding.
    No grant renewal is selected when the original revision is revoked.
    """
    @classmethod
    def load(cls, cursor, **kwargs):
        self = cls._construct(cursor, **kwargs)
        self._current(cursor)
        return self

    @classmethod
    def load_for_project(cls, cursor, *, project_id, operation=Operation.EXECUTE, **kwargs):
        # Private construction carries only the original source identity. The
        # public combined call performs one complete current-source validation
        # inside issue_for_project, including the real project grant checks.
        self = cls._construct(cursor, **kwargs)
        access = self.issue_for_project(cursor, project_id, operation=operation)
        return self, access

    @classmethod
    def _construct(cls, cursor, *, token, job, kind, binding_id, admission,
             route_guard=_deny, project_scope_guard=_deny, clock, purpose='execute', migration_authority=None):
        if (kind not in {'lifecycle', 'task'} or type(binding_id) is not int or binding_id <= 0
                or purpose not in {'execute','finalize'} or (kind=='lifecycle' and purpose!='execute')):
            raise Denied()
        table, key = ('qq_official_project_lifecycle_authority', 'job_id') if kind == 'lifecycle' else (
            'qq_official_project_task_authority', 'task_id')
        row = cursor.execute(f'SELECT * FROM {table} WHERE {key}=?', (binding_id,)).fetchone()
        if row is None or row['account_key'] != token.account_key:
            raise Denied()
        self = cls.__new__(cls)
        binding = dict(row)
        endpoint, actor = EndpointKey.parse(row['endpoint_key']), ActorKey.parse(row['actor_key'])
        if not actor.matches_endpoint(endpoint) or actor.identity_kind == 'bot':
            raise Denied()
        handle = SimpleNamespace(endpoint=endpoint, actor=actor, route_epoch=row['endpoint_route_epoch'])
        super(BackgroundProjectProvider, self).__init__(token=token, handle=handle, job=job,
            admission=admission, route_epoch=row['route_epoch'], route_guard=route_guard,
            project_scope_guard=project_scope_guard, clock=clock)
        self.kind, self.binding_id, self.binding, self.purpose = kind, binding_id, binding, purpose
        self.migration_authority = migration_authority
        return self

    @contextmanager
    def _read_composition(self, cursor):
        # All uses below compose synchronous authorization queries/security locks;
        # none changes business state or invokes guest/network IO.
        # Neither a result nor a source grant survives this call/RPC boundary.
        checked_at = self._now()
        current = self._current(cursor)
        transaction_id = cursor.execute('SELECT txid_current()').fetchone()[0]
        cached = SimpleNamespace(provider=self, cursor=cursor, transaction_id=transaction_id,
            checked_at=checked_at, row=current, thread_id=threading.get_ident(), active=True)
        mark = _read_current.set(cached)
        try:
            yield
        finally:
            # A copied context must lose this optimization too: reset alone
            # only clears the current context, whereas the marker is shared.
            cached.active = False
            _read_current.reset(mark)

    def require(self, cursor, proof, *, project_id=None, operation=Operation.READ):
        # Every public call performs a fresh source check. The base implementation
        # then calls _pin/_source_guard/_fresh within this same authorization call;
        # those may reuse it, while grant/owner/fresh evidence checks still run.
        with self._read_composition(cursor):
            return super().require(cursor, proof, project_id=project_id, operation=operation)

    def issue_for_project(self, cursor, project_id, *, operation=Operation.EXECUTE):
        """Issue and authorize one access in a single authorization-only host call."""
        with self._read_composition(cursor):
            access = super().issue(cursor)
            super().require(cursor, access.official_proof, project_id=project_id, operation=operation)
            return access

    def _current(self, cursor):
        cached = _read_current.get()
        if (cached is not None and cached.active and cached.provider is self
                and cached.cursor is cursor and cached.thread_id == threading.get_ident()):
            if cached.transaction_id != cursor.execute('SELECT txid_current()').fetchone()[0]:
                raise Denied()
            if cached.checked_at != self._now():
                # A new second requires a real source revalidation. Stamp its
                # START, never completion, so slow checks cannot renew a lease.
                checked_at = self._now()
                mark = _read_current.set(None)
                try:
                    current = self._current(cursor)
                finally:
                    _read_current.reset(mark)
                if (cached.active and cached.provider is self and cached.cursor is cursor
                        and cached.thread_id == threading.get_ident()):
                    if cached.transaction_id != cursor.execute('SELECT txid_current()').fetchone()[0]:
                        raise Denied()
                    cached.row, cached.checked_at = current, checked_at
                return dict(current)
            return dict(cached.row)
        now = self._now()
        try:
            assert_epoch(cursor, self.token)
        except LeaseLost:
            raise Denied() from None
        if self.route_guard(cursor, canonical_scope=self.endpoint.scope, endpoint=self.endpoint,
                            route_epoch=self.route_epoch, operation='execute') is not True:
            raise Denied()
        endpoint = cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=? FOR UPDATE',
                                  (self.endpoint.key,)).fetchone()
        table, key = ('qq_official_project_lifecycle_authority', 'job_id') if self.kind == 'lifecycle' else (
            'qq_official_project_task_authority', 'task_id')
        binding = cursor.execute(f'SELECT * FROM {table} WHERE {key}=?', (self.binding_id,)).fetchone()
        if binding is None or dict(binding) != self.binding:
            raise Denied()
        if (endpoint is None or endpoint['binding_state'] == 'revoked'
                or (self.endpoint.kind == 'group' and endpoint['binding_state'] != 'active')
                or endpoint['membership_state'] != 'present'
                or endpoint['route_epoch'] != binding['endpoint_route_epoch']
                or self.admission(cursor,self.endpoint,self.actor) is not True):
            raise Denied()
        current = cursor.execute('SELECT * FROM durable_jobs WHERE job_id=? FOR SHARE',
                                 (self.job.job_id,)).fetchone()
        if (current is None or current['status'] != 'running'
                or current['lease_owner'] != self.job.lease_owner
                or current['lease_generation'] != self.job.lease_generation
                or current['attempts'] != self.job.attempts
                or current['kind'] != self.job.kind
                or json.loads(current['payload_json']) != self.job.payload
                or (current['lease_until'] or 0) <= now):
            raise Denied()
        if self.kind == 'lifecycle':
            run = cursor.execute('SELECT * FROM project_lifecycle_runs WHERE job_id=?',
                                 (self.binding_id,)).fetchone()
            if (self.job.job_id != self.binding_id or self.job.kind != 'project.lifecycle'
                    or self.job.payload != {'project_id':binding['project_id']}
                    or run is None or run['project_id'] != binding['project_id']
                    or run['actor_principal_id'] != binding['principal_id']
                    or run['action'] != binding['action']
                    or run['status'] not in {'pending', 'running', 'needs_attention'}
                    or now >= binding['deadline_at']):
                raise Denied()
            if binding['source_execution_id']:
                source, fingerprint = _source_execution(cursor,binding['source_execution_id'])
                revision = source['execution_revision']
            else:
                source, fingerprint = _source_task(cursor,binding['source_task_id'])
                revision = source['control_revision']
            if fingerprint != binding['source_digest'] or revision != binding['source_revision']:
                raise Denied()
            from .project_autosave_authority import validate_lifecycle_io
            validate_lifecycle_io(cursor,binding,now=now)
        else:
            source, _ = _source_task(cursor,self.binding_id)
            dispatch = cursor.execute('SELECT * FROM qq_official_subagent_dispatches WHERE task_id=?',
                                      (self.binding_id,)).fetchone()
            if (dispatch is None or dispatch['job_id'] != self.job.job_id
                    or self.job.kind != account_job_kind('subagent',self.token.account_key)
                    or self.job.payload != {'task_id':self.binding_id,'revision':source['control_revision']}
                    or source['project_id'] != binding['project_id']
                    or source['control_revision'] != 1
                    or source['status'] not in ({'queued','interrupted','planning','running','verifying'} if self.purpose=='execute' else {'completed','partial'})
                    or now >= source['execution_deadline_at']):
                raise Denied()
        if source.get('migration_item_id') is not None:
            if self.migration_authority is None or self.migration_authority.require_task_source(
                    cursor, task_id=source['task_id'], job=self.job,
                    purpose='lifecycle' if self.kind=='lifecycle' else self.purpose, now=now) is not True:
                raise Denied()
        if (source['account_key'] != self.token.account_key or source['endpoint_key'] != self.endpoint.key
                or source['requester_actor_key'] != self.actor.key
                or source['endpoint_generation'] != endpoint['generation']
                or source['route_epoch'] != endpoint['route_epoch']):
            raise Denied()
        return dict(binding)

    def _pin(self, cursor, project_id):
        self._current(cursor)
        if project_id != self.binding['project_id']:
            raise Denied()
        row = cursor.execute('SELECT project_scope_key FROM qq_official_project_grants WHERE grant_id=?',
                             (self.binding['grant_id'],)).fetchone()
        if row is None:
            raise Denied()
        return ProjectGrantRef(self.binding['grant_id'],self.binding['grant_revision']), ConversationScope.from_key(row['project_scope_key'])

    def before_create(self, *args, **kwargs):
        raise Denied()

    def created(self, *args, **kwargs):
        raise Denied()

    def _fresh(self, cursor, *, request, source, now, max_age_seconds):
        from .adapters.qq_official.actor_evidence import recent_actor_evidence
        self._current(cursor)
        if (source is not self or request.endpoint != self.endpoint or request.actor != self.actor
                or request.route_epoch != self.route_epoch
                or request.endpoint_route_epoch != self.handle.route_epoch):
            return False
        return recent_actor_evidence(cursor, endpoint=self.endpoint, actor=self.actor,
                                     now=now, max_age_seconds=max_age_seconds)

    def _recipient(self, *args, **kwargs):
        return False

    def matches_tool_source(self,source):
        # Metadata selects the matching host proof; it cannot create one.
        return (self.kind=='task' and self.purpose=='execute'
            and source.endpoint==self.endpoint and source.actor==self.actor
            and source.source_kind=='project' and source.source_ref==f'task:{self.binding_id}'
            and source.source_revision==1)


def revalidate_access(cursor, access, *, project_id, operation=Operation.EXECUTE):
    proof = official_project(access)
    if proof is None:
        return None
    proof.provider.require(cursor, proof, project_id=project_id, operation=operation)
    return access


def require_packet(access, packet, dispatch):
    """Structured packet equality, independent of the resource project's scope."""
    proof = official_project(access)
    if proof is None:
        return False
    if (packet.requester_user_id is not None or packet.endpoint != access.endpoint
            or packet.requester_actor != access.actor or packet.scope_key != access.scope.key
            or dispatch.get('platform') != 'qq-official' or 'event' in dispatch
            or dispatch.get('bot_id') not in {None, access.scope.bot_native_user_id}):
        raise Denied()
    return True


def bind_task_authority(cursor, access, task, project, *, now):
    proof = official_project(access)
    if proof is None:
        return False
    provider = proof.provider
    provider.require(cursor,proof,project_id=project['project_id'],operation=Operation.EXECUTE)
    grant, _ = provider._pin(cursor,project['project_id'])
    if (task['account_key'] != access.endpoint.account.key or task['endpoint_key'] != access.endpoint.key
            or task['requester_actor_key'] != access.actor.key or task['requester_user_id'] is not None):
        raise Denied()
    cursor.execute('''INSERT INTO qq_official_project_task_authority
        (task_id,project_id,grant_id,grant_revision,account_key,endpoint_key,actor_key,
         route_epoch,endpoint_route_epoch,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
        (task['task_id'],project['project_id'],grant.grant_id,grant.revision,
         access.endpoint.account.key,access.endpoint.key,access.actor.key,provider.route_epoch,
         provider.handle.route_epoch,now))
    return True


class LifecycleAccessResolver:
    """Application injection for the existing ProjectRuntime, one account host.

    The actual token provider is consulted on each use. No global Bot registry
    or event construction is needed, and a missing account defaults to denial.
    """
    def __init__(self, *, transaction, token_for_account, admission, route_guard,
                 project_scope_guard, clock, migration_authority=None):
        self.transaction,self.token_for_account,self.admission=transaction,token_for_account,admission
        self.migration_authority=migration_authority
        self.route_guard,self.project_scope_guard,self.clock=route_guard,project_scope_guard,clock

    def authority(self,cursor,job):
        binding=cursor.execute('SELECT * FROM qq_official_project_lifecycle_authority WHERE job_id=?',
                               (job.job_id,)).fetchone()
        if binding is None:
            raise Denied()
        token=self.token_for_account(binding['account_key'])
        if token is None or token.account_key != binding['account_key']:
            raise Denied()
        provider=BackgroundProjectProvider.load(cursor,token=token,job=job,kind='lifecycle',
            binding_id=job.job_id,admission=self.admission,route_guard=self.route_guard,
            project_scope_guard=self.project_scope_guard,clock=self.clock,migration_authority=self.migration_authority)
        access=provider.issue(cursor)
        provider.require(cursor,access.official_proof,project_id=binding['project_id'],operation=Operation.EXECUTE)
        return access

    async def __call__(self,project,principal_id,*,job):
        from src.plugins.ai_chat.project_executor import _store
        def resolve():
            with self.transaction() as cursor:
                access=self.authority(cursor,job)
                if (access.principal_id != principal_id
                        or job.payload != {'project_id':project['project_id']}):
                    raise Denied()
                return access
        return await _store(resolve)


class ProjectTaskDispatcher:
    """Register an already-bound task in the existing official Sub-Agent lane.

    Only an actual foreground or its frozen lifecycle intent may enter here;
    migration/task continuation uses its separately reviewed source variant.
    """
    def __init__(self,jobs,*,result_wait_seconds=900):
        if type(result_wait_seconds) is not int or not 1<=result_wait_seconds<=86400:
            raise Denied()
        self.jobs,self.result_wait_seconds=jobs,result_wait_seconds

    def __call__(self,cursor,*,access,task,packet,dispatch,now):
        if not require_packet(access,packet,dispatch):
            raise Denied()
        provider=official_project(access).provider
        provider.require(cursor,access.official_proof,project_id=task['project_id'],operation=Operation.EXECUTE)
        if type(provider) is ForegroundProjectProvider:
            execution_id=provider.handle.execution_id
        elif isinstance(provider,BackgroundProjectProvider) and provider.kind=='lifecycle':
            execution_id=provider.binding.get('source_execution_id')
        else:
            raise Denied()
        if not execution_id:
            raise Denied()
        source,_=_source_execution(cursor,execution_id)
        deadline=dispatch.get('deadline')
        if (type(deadline) is not int or not now<deadline<=now+7200
                or dispatch.get('tool_profile')!='official-projects-v1'
                or not isinstance(dispatch.get('profile'),str) or not 1<=len(dispatch['profile'])<=128
                or task['trigger_message_id']!=source['canonical_message_id']
                or packet.trigger_message_id!=source['canonical_message_id']
                or source['endpoint_key']!=access.endpoint.key
                or source['requester_actor_key']!=access.actor.key):
            raise Denied()
        key=_digest(['qq-project-task:v1',task['task_id'],task['project_id'],execution_id])
        control=cursor.execute('SELECT * FROM subagent_controls WHERE task_id=?', (task['task_id'],)).fetchone()
        if control is None or control['revision']!=1:
            raise Denied()
        if json.loads(control['dispatch_json'])!=dispatch:
            raise Denied()
        pending,created=self.jobs.enqueue(kind=account_job_kind('subagent',access.endpoint.account.key),
            idempotency_key='qq-project-task:'+key,scope_key=access.endpoint.scope.key,
            payload={'task_id':task['task_id'],'revision':1},now=now,max_attempts=5,
            resume_on_lease_loss=True,transaction_cursor=cursor)
        if not created:
            raise Denied()
        cursor.execute('''INSERT INTO qq_official_subagent_dispatches
            (task_id,account_key,endpoint_key,requester_actor_key,source_execution_id,
             source_canonical_message_id,source_native_message_id,submission_key,submission_digest,
             job_id,endpoint_generation,route_epoch,tool_profile,execution_deadline_at,
             result_wait_seconds,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (task['task_id'],access.endpoint.account.key,access.endpoint.key,access.actor.key,
             execution_id,source['canonical_message_id'],source['source_native_message_id'],key,
             _digest([packet.as_payload(),dispatch]),pending.job_id,source['endpoint_generation'],
             source['route_epoch'],'official-projects-v1',deadline,self.result_wait_seconds,now,now))
        return pending
