"""Project grants and actual IO/task provenance for the existing M6 queue.

The bridge receives verified bytes from the existing project executor. It does
not fetch a model-provided URL/path, run a tool, grant a new capability or use
the legacy file outbox. Stored copies retain the original grant revision.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
import threading
import weakref

from src.project_content import project_path
from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.plugins.ai_chat.project_authorization import ForegroundProjectProvider, official_project
from src.plugins.ai_chat.project_background import BackgroundProjectProvider
from . import identity_store
from .file_artifacts import ArtifactError, SourceBinding, _issue
from .project_authority import Denied, Operation, ProjectAuthority, ProjectGrantRef, ProjectRequest


def _deny(*args, **kwargs):
    return False


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,
        separators=(',',':'),allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True, repr=False)
class _Registration:
    access: object
    project_id: int
    io_runtime: object | None
    io_grant: object | None


_MANIFEST = ('artifact_id','account_key','endpoint_key','requester_actor_key','source_execution_id',
    'source_task_id','source_task_revision','source_canonical_message_id','source_native_message_id',
    'source_scope_key','source_job_id','source_job_lease_generation','endpoint_generation','route_epoch',
    'sha256','byte_size','filename')


def _io_material(row):
    return {key:row[key] for key in ('operation_id','project_id','actor_principal_id','tool_name',
        'request_key','arguments_sha256','sandbox_id','generation','fence','journal_turn_id')}


def _fingerprint(artifact, io_row):
    return _digest([{key:artifact[key] for key in _MANIFEST}, _io_material(io_row) if io_row else None])


class ProjectArtifactAuthority:
    kind = 'project'

    def __init__(self, *, token, clock, route_guard=_deny, read_route_guard=_deny, project_scope_guard=_deny):
        self.token,self.clock = token,clock
        self.route_guard,self.read_route_guard,self.project_scope_guard = route_guard,read_route_guard,project_scope_guard
        self._sources,self._lock = weakref.WeakKeyDictionary(),threading.Lock()

    def _matches_provider(self, binding, provider):
        # Identity only; this does not validate or cache current authority.
        with self._lock:
            registration = self._sources.get(binding)
        proof = official_project(registration.access) if registration is not None else None
        return binding.extension is self and proof is not None and proof.provider is provider

    def _registration(self, cursor, registration):
        proof = official_project(registration.access)
        if proof is not None and type(proof.provider) is BackgroundProjectProvider:
            # Synchronous metadata verification/security locks; no business mutation or guest IO.
            # Keep every grant/source/namespace query below, while the exact
            # provider may reuse its first source check within this call only.
            with proof.provider._read_composition(cursor):
                return self._registration_current(cursor, registration)
        return self._registration_current(cursor, registration)

    def _registration_current(self, cursor, registration):
        access,project_id = registration.access,registration.project_id
        proof = official_project(access)
        if proof is None or proof.provider.token is not self.token:
            raise Denied()
        provider = proof.provider
        background_task = (registration.io_grant is None
            and isinstance(provider,BackgroundProjectProvider) and provider.kind=='task')
        # Local generation/capture remains an execution of the original task.
        # Fresh recipient evidence is checked again by __call__ at real upload
        # and send boundaries, rather than expiring a still-authorized guest.
        provider.require(cursor,proof,project_id=project_id,
            operation=Operation.EXECUTE if background_task else Operation.DELIVER)
        grant,scope = provider._pin(cursor,project_id)
        grant_row = cursor.execute('SELECT * FROM qq_official_project_grants WHERE grant_id=?', (grant.grant_id,)).fetchone()
        if (grant_row is None or grant_row['revision'] != grant.revision
                or (background_task and Operation.DELIVER.value not in grant_row['capabilities'])):
            raise Denied()
        io_row = None
        if registration.io_grant is not None:
            if type(provider) is not ForegroundProjectProvider or registration.io_runtime is None:
                raise Denied()
            source = dict(provider._current(cursor))
            project,io_row = registration.io_runtime.io._owned(cursor,registration.io_grant)
            if (project['project_id'] != project_id or io_row['actor_principal_id'] != access.principal_id
                    or io_row['tool_name'] != 'send_file_from_sandbox' or io_row['state'] != 'dispatched'
                    or io_row['journal_turn_id'] != source['journal_turn_id']):
                raise Denied()
            source.update(source_execution_id=source['execution_id'],source_task_id=None,
                source_canonical_message_id=source['canonical_message_id'],source_scope_key=source['scope_key'])
            source_kind = 'project_io'
        else:
            if not isinstance(provider,BackgroundProjectProvider) or provider.kind != 'task':
                raise Denied()
            provider._current(cursor)
            source = dict(cursor.execute('SELECT * FROM qq_official_subagent_dispatches WHERE task_id=?',
                                         (provider.binding_id,)).fetchone())
            task = cursor.execute('SELECT * FROM subagent_tasks WHERE task_id=?', (provider.binding_id,)).fetchone()
            if (task is None or task['project_id'] != project_id
                    or source['tool_profile'] != 'official-projects-v1' or source['job_id'] != provider.job.job_id):
                raise Denied()
            source.update(source_task_id=provider.binding_id,source_scope_key=task['scope_key'],
                          deadline_at=source['execution_deadline_at'])
            source_kind = 'project_task'
        if source['endpoint_key'] != provider.endpoint.key or source['requester_actor_key'] != provider.actor.key:
            raise Denied()
        return provider,grant_row,source,source_kind,io_row

    def issue(self, cursor, *, access, project_id, io_runtime=None, io_grant=None):
        if type(project_id) is not int or project_id<=0 or (io_runtime is None)!=(io_grant is None):
            raise Denied()
        registration = _Registration(access,project_id,io_runtime,io_grant)
        provider,grant,source,kind,io_row = self._registration(cursor,registration)
        pending = min(source['created_at']+86400,grant['expires_at'])
        migration_id=source.get('migration_item_id')
        if migration_id is not None:
            pending=min(pending,source['migration_result_deadline_at'])
        now = int(self.clock())
        if not now < min(pending,source['deadline_at']):
            raise Denied()
        source_key = ('project-io:'+str(io_row['operation_id']) if io_row else
                      'project-task:'+str(source['source_task_id'])+':1')
        binding = SourceBinding(self.token,provider.job,provider.admission,
            provider.handle if kind=='project_io' else None,source['source_task_id'],pending,
            provider.endpoint,provider.actor,source['source_execution_id'],source_key,source['deadline_at'],
            source['source_native_message_id'],self,migration_id)
        with self._lock:
            self._sources[binding]=registration
        return _issue(binding)

    def _checked_registration(self, cursor, binding, now):
        with self._lock:
            registration = self._sources.get(binding)
        if registration is None or binding.extension is not self or now>=min(binding.deadline_at,binding.pending_deadline_at):
            raise LeaseLost('project_artifact_source_unavailable')
        provider,grant,source,kind,io_row = self._registration(cursor,registration)
        if (binding.token is not self.token or binding.job != provider.job
                or binding.endpoint != provider.endpoint or binding.actor != provider.actor
                or binding.source_execution_id != source['source_execution_id']
                or binding.task_id != source['source_task_id']
                or binding.source_migration_item_id != source.get('migration_item_id')
                or binding.source_native_message_id != source['source_native_message_id']):
            raise LeaseLost('project_artifact_source_changed')
        return registration,provider,grant,source,kind,io_row

    def current_source(self, cursor, binding, now):
        return self._checked_registration(cursor,binding,now)[3]

    def bind_artifact(self, cursor, binding, artifact, *, now, existing):
        registration,provider,grant,source,kind,io_row = self._checked_registration(cursor,binding,now)
        fields = dict(artifact_id=artifact['artifact_id'],source_extension_kind='project',
            project_id=registration.project_id,grant_id=grant['grant_id'],grant_revision=grant['revision'],
            grant_expires_at=grant['expires_at'],account_key=binding.endpoint.account.key,
            endpoint_key=binding.endpoint.key,actor_key=binding.actor.key,principal_id=registration.access.principal_id,
            logical_route_epoch=provider.route_epoch,source_kind=kind,source_task_id=binding.task_id,
            io_operation_id=io_row['operation_id'] if io_row else None,
            io_token_digest=hashlib.sha256(io_row['token'].encode()).hexdigest() if io_row else None,
            source_digest=_fingerprint(artifact,io_row),created_at=artifact['created_at'])
        if existing:
            row = cursor.execute('SELECT * FROM qq_official_project_artifacts WHERE artifact_id=?', (artifact['artifact_id'],)).fetchone()
            if row is None or any(row[key]!=value for key,value in fields.items()):
                raise LeaseLost('project_artifact_provenance_changed')
        else:
            from src.plugins.ai_chat.project_usage import ProjectQuotaRejected, ProjectUsageStore
            from src.plugins.ai_chat.projects import ProjectQuotaExceeded, ProjectStore
            scope_key = grant['project_scope_key']
            cursor.execute('SELECT pg_advisory_xact_lock(hashtextextended(?,0))', ('project-scope:'+scope_key,))
            peers = cursor.execute('''SELECT p.*,u.complete,u.generation,u.fence,u.sample FROM projects p
                LEFT JOIN project_usage u USING(project_id) WHERE p.scope_key=? AND p.status<>'purged' ''',
                (scope_key,)).fetchall()
            if any(not ProjectUsageStore._fresh(p,p,now) for p in peers):
                raise ProjectQuotaRejected('project_scope_usage_unknown')
            limit = cursor.execute('SELECT quota_bytes FROM project_scope_limits WHERE scope_key=?',
                                   (scope_key,)).fetchone()
            counted = cursor.execute('''SELECT 1 FROM qq_official_project_artifacts p
                JOIN qq_official_file_artifacts a USING(artifact_id)
                WHERE p.project_id=? AND a.sha256=? AND a.bytes_state='available' LIMIT 1''',
                (registration.project_id,artifact['sha256'])).fetchone()
            extra = 0 if counted else artifact['byte_size']
            if (limit is None or ProjectStore._scope_bytes(cursor,scope_key)
                    + ProjectStore._scope_reserved_bytes(cursor,scope_key) + extra > limit[0]):
                raise ProjectQuotaExceeded('Project attachment exceeds conversation quota')
            cursor.execute('INSERT INTO qq_official_project_artifacts ('+','.join(fields)+') VALUES ('+
                ','.join('?' for _ in fields)+')', tuple(fields.values()))

    def __call__(self,cursor,*,token,artifact,now,read_only):
        self._project_current(cursor,token=token,artifact=artifact,now=now,read_only=read_only)

    def _project_current(self, cursor, *, token, artifact, now, read_only):
        if token is not self.token or artifact.get('source_extension_kind')!='project':
            raise LeaseLost('project_artifact_authority_unavailable')
        row = cursor.execute('SELECT * FROM qq_official_project_artifacts WHERE artifact_id=? FOR SHARE',
                             (artifact['artifact_id'],)).fetchone()
        if row is None:
            raise LeaseLost('project_artifact_provenance_missing')
        endpoint,actor = EndpointKey.parse(artifact['endpoint_key']),ActorKey.parse(artifact['requester_actor_key'])
        if (row['source_task_id']!=artifact['source_task_id'] or row['account_key']!=token.account_key
                or row['endpoint_key']!=endpoint.key or row['actor_key']!=actor.key
                or now>=row['grant_expires_at']):
            raise LeaseLost('project_artifact_source_changed')
        grant=cursor.execute('SELECT * FROM qq_official_project_grants WHERE grant_id=?', (row['grant_id'],)).fetchone()
        if grant is None or grant['principal_id']!=row['principal_id']:
            raise LeaseLost('project_artifact_grant_unavailable')
        request=ProjectRequest(endpoint,actor,ConversationScope.from_key(grant['project_scope_key']),
            row['logical_route_epoch'],artifact['route_epoch'],row['project_id'],Operation.DELIVER)
        source=object()

        def route_guard(c, **kwargs):
            if read_only:
                return self.read_route_guard(c,artifact=artifact,project_binding=dict(row),**kwargs) is True
            return self.route_guard(c,**kwargs) is True

        def source_guard(c, *, request, source: object):
            project=c.execute('SELECT * FROM projects WHERE project_id=?', (row['project_id'],)).fetchone()
            if source is not marker or project is None or project['scope_key']!=request.canonical_scope.key:
                return False
            # ProjectAuthority holds the scope lock before entering this guard.
            # Late IO receipts acquire scope -> project -> IO and must be able
            # to settle while this artifact is waiting for that same scope.
            io_row = None
            if row['source_kind']=='project_io':
                io_row=c.execute('SELECT * FROM project_io WHERE operation_id=? FOR SHARE',
                                 (row['io_operation_id'],)).fetchone()
                if (io_row is None or io_row['state'] not in {'dispatched','completed','needs_attention'}
                        or io_row['tool_name']!='send_file_from_sandbox'
                        or hashlib.sha256(io_row['token'].encode()).hexdigest()!=row['io_token_digest']):
                    raise LeaseLost('project_artifact_io_unavailable')
            if row['source_digest']!=_fingerprint(artifact,io_row):
                raise LeaseLost('project_artifact_source_changed')
            if request.canonical_scope.key==endpoint.scope.key:
                author=identity_store.snapshot(c,endpoint.account)
                return author is not None and project['bot_native_user_id']==author.bot_actor.native_user_id
            return self.project_scope_guard(c,request=request,project=dict(project),source=dict(row)) is True

        marker=source
        authority=ProjectAuthority(clock=self.clock,route_guard=route_guard,source_guard=source_guard,
            fresh_actor_evidence=lambda c,**kw: True)
        authority.require_project(cursor,request=request,grant=ProjectGrantRef(row['grant_id'],row['grant_revision']),source=source)


class ProjectFileBridge:
    def __init__(self, *, files, projects, authority):
        from .file_authority import ArtifactSourceAuthorities
        configured=files.config.source_authority
        if isinstance(configured,ArtifactSourceAuthorities):
            configured=configured.resolve('project')
        if configured is not authority or files.token is not authority.token:
            raise ValueError('project_file_services_not_bound')
        self.files,self.projects,self.authority=files,projects,authority

    async def __call__(self, access, io_grant, content, path, filename=None):
        normalized=project_path(path)
        if normalized!=path:
            raise ArtifactError('project_file_path_changed')
        args={'path':path}
        if filename is not None:
            args['filename']=filename
        expected=hashlib.sha256(json.dumps(['send_file_from_sandbox',args],sort_keys=True,ensure_ascii=False).encode()).hexdigest()

        def prepare():
            with self.files.transaction() as cursor:
                source=self.authority.issue(cursor,access=access,project_id=io_grant.project_id,
                    io_runtime=self.projects,io_grant=io_grant)
                _,row=self.projects.io._owned(cursor,io_grant)
                if row['arguments_sha256']!=expected:
                    raise Denied()
                return source,row['request_key']

        source,key=await self.files.calls(prepare)
        record=await self.files.calls(self.files.artifacts.register,source,content=content,
            filename=filename or PurePosixPath(path).name,logical_request_key=key)
        return {'ok':True,'status':'stored','artifact_handle':'artifact:'+record.artifact_id,
                'filename':record.filename,'sha256':record.sha256,'bytes':record.byte_size,
                'delivery_confirmed':record.message_accepted}
