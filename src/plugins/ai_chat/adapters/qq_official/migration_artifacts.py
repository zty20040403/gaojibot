"""Explicit migration task source for the existing M6 artifact pipeline.

The item was approved with a finite file profile. Its old task remains frozen;
new generation uses the actual target task job, and handoff uses the distinct
file job. Neither source IDs nor an active parent lease are invented.
"""
from dataclasses import dataclass
import threading
import weakref

from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from .file_artifacts import SourceBinding, _issue
from .lease import assert_epoch
from .migration_types import digest


_MANIFEST=('artifact_id','account_key','endpoint_key','requester_actor_key','source_execution_id',
    'source_migration_item_id','source_task_id','source_task_revision','source_canonical_message_id',
    'source_native_message_id','source_scope_key','source_job_id','source_job_lease_generation',
    'source_account_epoch','endpoint_generation','route_epoch','sha256','byte_size','mime','filename',
    'draft','draft_reason','created_at','pending_deadline_at')


def _fingerprint(artifact, continuation):
    return digest([{key:artifact[key] for key in _MANIFEST}, {key:continuation[key] for key in (
        'item_id','target_task_id','source_task_id','source_revision','source_frozen_digest','export_digest',
        'account_key','endpoint_key','actor_key','job_id','logical_route_epoch','mapping_id','mapping_revision',
        'execution_deadline','result_deadline','tool_profile')}])


@dataclass(frozen=True,repr=False)
class _Registration:
    task_id: int
    job: object


class MigrationArtifactAuthority:
    kind='migration'

    def __init__(self, *, continuations, imports=None):
        self.engine=continuations
        source=continuations if continuations is not None else imports
        if source is None:
            raise ValueError('migration_artifact_domain_required')
        self.token,self.journal=source.token,source.journal
        self.imports=imports
        self._sources=weakref.WeakKeyDictionary()
        self._lock=threading.Lock()

    def _current(self,cursor,task_id,job,now):
        if self.engine is None:
            raise LeaseLost('migration_artifact_producer_unavailable')
        from .subagent_runtime import assert_current
        source,task=assert_current(cursor,token=self.token,task_id=task_id,job=job,
            admission=self.engine.admission,now=now,migration_authority=self.engine)
        continuation=cursor.execute('SELECT * FROM migration_subagent_continuations WHERE target_task_id=?', (task_id,)).fetchone()
        if (continuation is None or source['migration_item_id']!=continuation['item_id']
                or source['tool_profile']!='official-files-v1' or continuation['tool_profile']!='official-files-v1'
                or source['source_execution_id'] is not None or source['source_native_message_id'] is not None
                or source['source_canonical_message_id'] is not None or task['trigger_message_id'] is not None
                or task['project_id'] is not None or task['status'] in {'completed','partial','failed','cancelled'}
                or now>=min(continuation['execution_deadline'],continuation['result_deadline'])):
            raise LeaseLost('migration_artifact_producer_unavailable')
        return dict(source),dict(task),dict(continuation)

    def issue(self,cursor,*,task,job,now):
        source,stored,continuation=self._current(cursor,task.task_id,job,now)
        endpoint,actor=EndpointKey.parse(source['endpoint_key']),ActorKey.parse(source['requester_actor_key'])
        if task.endpoint!=endpoint or task.requester_actor!=actor or task.migration_item_id!=continuation['item_id']:
            raise LeaseLost('migration_artifact_producer_changed')
        binding=SourceBinding(self.token,job,self.engine.admission,None,task.task_id,continuation['result_deadline'],
            endpoint,actor,None,f'task:{task.task_id}:1',continuation['execution_deadline'],None,self,continuation['item_id'])
        with self._lock:
            self._sources[binding]=_Registration(task.task_id,job)
        return _issue(binding)

    def current_source(self,cursor,binding,now):
        with self._lock:
            registration=self._sources.get(binding)
        if registration is None or binding.extension is not self or binding.token is not self.token:
            raise LeaseLost('migration_artifact_source_unissued')
        source,task,continuation=self._current(cursor,registration.task_id,registration.job,now)
        if (binding.job!=registration.job or binding.task_id!=registration.task_id
                or binding.source_migration_item_id!=continuation['item_id']
                or binding.endpoint.key!=source['endpoint_key'] or binding.actor.key!=source['requester_actor_key']
                or binding.source_execution_id is not None or binding.source_native_message_id is not None
                or binding.deadline_at!=continuation['execution_deadline']
                or binding.pending_deadline_at!=continuation['result_deadline']):
            raise LeaseLost('migration_artifact_source_changed')
        source['source_scope_key']=task['scope_key']
        return source

    def bind_artifact(self,cursor,binding,artifact,*,now,existing):
        self.current_source(cursor,binding,now)
        continuation=dict(cursor.execute('SELECT * FROM migration_subagent_continuations WHERE item_id=?',
            (binding.source_migration_item_id,)).fetchone())
        fields=dict(artifact_id=artifact['artifact_id'],source_extension_kind='migration',item_id=continuation['item_id'],
            task_id=binding.task_id,task_revision=1,source_job_id=binding.job.job_id,account_key=binding.endpoint.account.key,
            endpoint_key=binding.endpoint.key,actor_key=binding.actor.key,logical_route_epoch=continuation['logical_route_epoch'],
            mapping_id=continuation['mapping_id'],mapping_revision=continuation['mapping_revision'],
            source_digest=_fingerprint(artifact,continuation),result_deadline=continuation['result_deadline'],created_at=artifact['created_at'])
        if existing:
            row=cursor.execute('SELECT * FROM qq_official_migration_artifacts WHERE artifact_id=?', (artifact['artifact_id'],)).fetchone()
            if row is None or any(row[key]!=value for key,value in fields.items()):
                raise LeaseLost('migration_artifact_manifest_changed')
        else:
            cursor.execute('INSERT INTO qq_official_migration_artifacts ('+','.join(fields)+') VALUES ('+
                           ','.join('?' for _ in fields)+')',tuple(fields.values()))

    def validate_bound_artifact(self,cursor,item,artifact_id,*,now):
        if item.source_kind=='file':
            if self.imports is None or self.imports.token is not self.token or self.imports.journal is not self.journal:
                raise LeaseLost('migration_file_import_authority_unavailable')
            return self.imports.validate_bound_artifact(cursor,item,artifact_id,now=now)
        assert_epoch(cursor,self.token)
        artifact=cursor.execute('SELECT * FROM qq_official_file_artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()
        stored=cursor.execute('SELECT * FROM qq_official_migration_artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()
        continuation=cursor.execute('SELECT * FROM migration_subagent_continuations WHERE item_id=?', (item.item_id,)).fetchone()
        if artifact is None or stored is None or continuation is None:
            raise LeaseLost('migration_artifact_source_missing')
        artifact,continuation=dict(artifact),dict(continuation)
        self.engine._same(continuation,item)
        self.engine._source_current(cursor,continuation)
        task=cursor.execute('SELECT * FROM subagent_tasks WHERE task_id=? FOR SHARE', (continuation['target_task_id'],)).fetchone()
        dispatch=cursor.execute('SELECT * FROM qq_official_subagent_dispatches WHERE task_id=? FOR SHARE', (continuation['target_task_id'],)).fetchone()
        if (continuation['tool_profile']!='official-files-v1' or stored['source_digest']!=_fingerprint(artifact,continuation)
                or artifact['source_extension_kind']!='migration' or artifact['source_migration_item_id']!=item.item_id
                or artifact['source_execution_id'] is not None or artifact['source_native_message_id'] is not None
                or artifact['source_canonical_message_id'] is not None or artifact['source_task_revision']!=1
                or artifact['source_task_id']!=continuation['target_task_id'] or artifact['source_job_id']!=continuation['job_id']
                or artifact['source_scope_key']!=item.target_endpoint.scope.key
                or artifact['pending_deadline_at']>item.result_deadline or stored['result_deadline']!=item.result_deadline
                or dispatch is None or dispatch['tool_profile']!='official-files-v1' or dispatch['migration_item_id']!=item.item_id
                or task is None or task['cancel_requested'] or task['status']=='cancelled'
                or (task['status']=='failed' and not artifact['draft'])):
            raise LeaseLost('migration_artifact_source_changed')
        if self.engine.admission(cursor,item.target_endpoint,item.target_actor) is not True:
            raise LeaseLost('migration_artifact_actor_revoked')
        if task['status'] not in {'completed','partial','failed'}:
            producer=cursor.execute('SELECT * FROM durable_jobs WHERE job_id=? FOR SHARE', (artifact['source_job_id'],)).fetchone()
            if (producer is None or producer['status']!='running' or (producer['lease_until'] or 0)<=now
                    or producer['lease_generation']!=artifact['source_job_lease_generation']
                    or producer['lease_generation']!=continuation['execution_job_generation']
                    or producer['lease_owner']!=continuation['execution_job_owner'] or now>=continuation['execution_deadline']):
                raise LeaseLost('migration_artifact_producer_interrupted')

    def __call__(self,cursor,*,token,artifact,now,read_only):
        if token is not self.token or artifact.get('source_extension_kind')!='migration':
            raise LeaseLost('migration_artifact_authority_unavailable')
        self.journal.current_artifact_source(cursor,artifact['artifact_id'],now=now)

    def enqueue_proof(self,cursor,*,token,artifact,job,now):
        self(cursor,token=token,artifact=artifact,now=now,read_only=False)
        return self.journal.issue_artifact(cursor,artifact_id=artifact['artifact_id'],job=job,now=now)

    def bind_plan(self,cursor,proof,*,plan_id,now):
        return self.journal.bind_artifact_plan(cursor,proof,plan_id=plan_id,now=now)
