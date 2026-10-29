"""Exact project and migration provenance for the existing M6 file worker."""
from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.migration_artifacts import _fingerprint


class ProjectMigrationArtifacts:
    def __init__(self, authority, engine):
        if engine.token is not authority.token or engine.domain_kind!='project_task':
            raise ValueError('project_migration_authority_mismatch')
        self.authority,self.engine,self.token,self.journal=authority,engine,engine.token,engine.journal

    def bind(self,cursor,binding,artifact,*,now,existing):
        continuation=cursor.execute('SELECT * FROM migration_subagent_continuations WHERE item_id=?',(binding.source_migration_item_id,)).fetchone()
        if continuation is None or continuation['target_task_id']!=binding.task_id or continuation['job_id']!=binding.job.job_id:
            raise LeaseLost('project_migration_source_changed')
        continuation=dict(continuation)
        self.engine._source_current(cursor,continuation)
        fields=dict(artifact_id=artifact['artifact_id'],source_extension_kind='project',source_kind='project_task',
            item_id=continuation['item_id'],task_id=binding.task_id,task_revision=1,source_job_id=binding.job.job_id,
            account_key=binding.endpoint.account.key,endpoint_key=binding.endpoint.key,actor_key=binding.actor.key,
            logical_route_epoch=continuation['logical_route_epoch'],mapping_id=continuation['mapping_id'],
            mapping_revision=continuation['mapping_revision'],source_digest=_fingerprint(artifact,continuation),
            result_deadline=continuation['result_deadline'],created_at=artifact['created_at'])
        if existing:
            stored=cursor.execute('SELECT * FROM qq_official_migration_artifacts WHERE artifact_id=?',(artifact['artifact_id'],)).fetchone()
            if stored is None or any(stored[key]!=value for key,value in fields.items()):
                raise LeaseLost('project_migration_manifest_changed')
        else:
            cursor.execute('INSERT INTO qq_official_migration_artifacts ('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+')',tuple(fields.values()))

    def validate_bound_artifact(self,cursor,item,artifact_id,*,now,read_only=False):
        assert_epoch(cursor,self.token)
        artifact=cursor.execute('SELECT * FROM qq_official_file_artifacts WHERE artifact_id=?',(artifact_id,)).fetchone()
        stored=cursor.execute('SELECT * FROM qq_official_migration_artifacts WHERE artifact_id=?',(artifact_id,)).fetchone()
        continuation=cursor.execute('SELECT * FROM migration_subagent_continuations WHERE item_id=?',(item.item_id,)).fetchone()
        if artifact is None or stored is None or continuation is None or item.source_kind!='project_task':
            raise LeaseLost('project_migration_artifact_missing')
        artifact,continuation=dict(artifact),dict(continuation)
        self.engine._same(continuation,item);self.engine._source_current(cursor,continuation)
        task=cursor.execute('SELECT * FROM subagent_tasks WHERE task_id=? FOR SHARE',(continuation['target_task_id'],)).fetchone()
        dispatch=cursor.execute('SELECT * FROM qq_official_subagent_dispatches WHERE task_id=? FOR SHARE',(continuation['target_task_id'],)).fetchone()
        if (stored['source_digest']!=_fingerprint(artifact,continuation) or stored['source_kind']!='project_task'
                or stored['result_deadline']!=item.result_deadline or artifact['source_extension_kind']!='project'
                or artifact['source_migration_item_id']!=item.item_id or artifact['source_task_revision']!=1
                or artifact['source_task_id']!=continuation['target_task_id'] or artifact['source_job_id']!=continuation['job_id']
                or any(artifact[key] is not None for key in ('source_execution_id','source_canonical_message_id','source_native_message_id'))
                or artifact['source_scope_key']!=item.target_endpoint.scope.key or artifact['pending_deadline_at']>item.result_deadline
                or dispatch is None or dispatch['tool_profile']!='official-projects-v1' or dispatch['migration_item_id']!=item.item_id
                or task is None or task['project_id'] is None or task['cancel_requested'] or task['status']=='cancelled'
                or (task['status']=='failed' and not artifact['draft'])):
            raise LeaseLost('project_migration_artifact_changed')
        if self.engine.admission(cursor,item.target_endpoint,item.target_actor) is not True:
            raise LeaseLost('project_migration_actor_revoked')
        if task['status'] not in {'completed','partial','failed'}:
            job=cursor.execute('SELECT * FROM durable_jobs WHERE job_id=? FOR SHARE',(continuation['job_id'],)).fetchone()
            if (job is None or job['status']!='running' or (job['lease_until'] or 0)<=now
                    or job['lease_generation']!=artifact['source_job_lease_generation']
                    or job['lease_generation']!=continuation['execution_job_generation']
                    or job['lease_owner']!=continuation['execution_job_owner'] or now>=continuation['execution_deadline']):
                raise LeaseLost('project_migration_producer_interrupted')
        self.authority._project_current(cursor,token=self.token,artifact=artifact,now=now,read_only=read_only)
