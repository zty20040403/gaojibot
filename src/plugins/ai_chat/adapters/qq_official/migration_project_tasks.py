"""Project conversion using the existing task domain and immutable baseline.

The injected staged-grant port must be the authenticated console grant
service. It must recheck the real journal/route/mapping without recursively asking
Journal to validate this same domain binding. No physical IO occurs here.
"""
import hashlib
import json
from typing import Protocol

from src.plugins.ai_chat.platform_types import EndpointKey, ActorKey
from src.plugins.ai_chat.project_tasks import ProjectTaskStore
from src.plugins.ai_chat.projects import ProjectStore, ProjectConflict
from src.plugins.ai_chat.project_task_reconciliation import ProjectTaskReleases
from src.project_content import MAX_MANIFEST_BYTES, manifest_bytes
from src.plugins.ai_chat.adapters.qq_official.project_authority import ProjectGrantRef, ProjectSnapshot, Operation
from src.plugins.ai_chat.adapters.qq_official.migration_subagents import SubAgentContinuations, ConversionDenied, digest, row


class StagedProjectGrants(Protocol):
    def require_binding(self, cursor, *, migration_id, item_id, project_id, grant,
                        phase, now, operation) -> ProjectSnapshot: ...


class ProjectTaskContinuations(SubAgentContinuations):
    domain_kind = 'project_task'

    def __init__(self, *args, grants: StagedProjectGrants, projects: ProjectStore, **kwargs):
        if grants is None or not callable(getattr(grants, 'require_binding', None)):
            raise ConversionDenied()
        super().__init__(*args, **kwargs)
        if not isinstance(projects,ProjectStore) or projects.database is not self.database:
            raise ConversionDenied()
        self.grants, self.projects = grants, projects

    def _profiles(self):
        return {'official-projects-v1'}

    def _profile(self, files):
        return 'official-projects-v1'

    def _original_kind(self, task):
        return task['project_id'] is not None

    def _target_kind(self, task):
        return task['project_id'] is not None

    def _lock_source(self, cursor, proof):
        # Journal already holds account and the sorted route mutexes. Acquire
        # project-scope and project before original task/run/domain rows.
        original = row(cursor, 'SELECT project_id FROM subagent_tasks WHERE task_id=?', (int(proof.source_key),))
        project = row(cursor, 'SELECT * FROM projects WHERE project_id=?', (original['project_id'],)) if original else None
        if project is None or project['scope_key'] != proof.source_scope.key:
            raise ConversionDenied()
        cursor.execute('SELECT pg_advisory_xact_lock(hashtextextended(?,0))', ('project-scope:' + project['scope_key'],))
        cursor.execute('SELECT project_id FROM projects WHERE project_id=? FOR UPDATE', (project['project_id'],)).fetchone()

    def _source_digest(self, cursor, task_id):
        binding = row(cursor, 'SELECT * FROM project_task_bindings WHERE task_id=?', (task_id,))
        if binding is None:
            raise ConversionDenied()
        return digest([super()._source_digest(cursor, task_id), binding])

    @staticmethod
    def _grant(data):
        value = data.get('mapping', {}).get('project_grant')
        if not isinstance(value, dict) or set(value) != {'grant_id', 'revision'}:
            raise ConversionDenied()
        return ProjectGrantRef(value['grant_id'], value['revision'])

    def _grant_current(self, cursor, *, data, item, endpoint, actor, logical_epoch, endpoint_epoch, phase, now):
        ref = self._grant(data)
        route = row(cursor, '''SELECT r.* FROM migration_routes r JOIN migration_journals j USING(route_id)
            WHERE j.migration_id=?''', (item['migration_id'],))
        if (route is None or item['target_actor_key'] != actor.key or route['target_endpoint_key'] != endpoint.key
                or route['epoch'] != logical_epoch or route['target_endpoint_epoch'] != endpoint_epoch):
            raise ConversionDenied()
        snapshot = self.grants.require_binding(cursor, migration_id=item['migration_id'], item_id=item['item_id'],
            project_id=data['project_id'], grant=ref, phase=phase, now=now, operation=Operation.EXECUTE)
        if (not isinstance(snapshot, ProjectSnapshot) or snapshot.grant != ref
                or snapshot.project_id != data['project_id'] or snapshot.operation is not Operation.EXECUTE
                or snapshot.expires_at < item['execution_deadline']):
            raise ConversionDenied()
        return snapshot

    def _prepare_target(self, cursor, proof, original, data, now):
        # A terminal project's accepted file/publication has a separate reviewed
        # file source. Copying its final text is not a new accepted project update.
        if data['source_task_status'] in {'completed', 'partial'}:
            raise ConversionDenied()
        item = row(cursor, 'SELECT * FROM migration_items WHERE item_id=?', (proof.item_id,))
        project = row(cursor, 'SELECT * FROM projects WHERE project_id=? FOR UPDATE', (data['project_id'],))
        source = row(cursor, 'SELECT * FROM project_task_bindings WHERE task_id=? FOR UPDATE', (original['task_id'],))
        baseline = row(cursor, 'SELECT * FROM project_versions WHERE project_id=? AND version_no=?',
                       (data['project_id'], data['base_version']))
        if (item is None or project is None or source is None or baseline is None
                or project['scope_key'] != proof.source_scope.key or project['bot_native_user_id'] != proof.source_endpoint.account.app_id
                or source['project_id'] != project['project_id'] or original['project_id'] != project['project_id']
                or project['locked_task_id'] != original['task_id'] or project['lifecycle_job_id'] is not None
                or project['workspace_generation'] != data['generation'] or project['lifecycle_fence'] != data['fence']
                or baseline['manifest_sha256'] != data['manifest_sha256'] or source['manifest_sha256'] != data['manifest_sha256']
                or source['base_version'] != data['base_version'] or source['generation'] != data['generation'] or source['fence'] != data['fence']
                or cursor.execute("SELECT 1 FROM project_io WHERE project_id=? AND state IN ('reserved','dispatched','needs_attention') LIMIT 1", (project['project_id'],)).fetchone()):
            raise ConversionDenied()
        ProjectTaskStore._clean_baseline(cursor, project, baseline)
        snapshot = self._grant_current(cursor, data=data, item=item, endpoint=proof.target_endpoint, actor=proof.target_actor,
            logical_epoch=proof.logical_route_epoch, endpoint_epoch=proof.endpoint_route_epoch, phase='prepare', now=now)
        grant_row=row(cursor,'SELECT capabilities FROM qq_official_project_grants WHERE grant_id=?',(snapshot.grant.grant_id,))
        if grant_row is None or not {'project.read','project.write','project.execute','project.deliver'} <= set(grant_row['capabilities']):
            raise ConversionDenied()
        return source, snapshot

    def _bind_target(self, cursor, proof, original, data, task, project_context, now):
        source, snapshot = project_context
        project=row(cursor,'SELECT * FROM projects WHERE project_id=? FOR UPDATE',(source['project_id'],))
        frozen=row(cursor,'SELECT * FROM subagent_tasks WHERE task_id=? FOR UPDATE',(original['task_id'],))
        control=row(cursor,'SELECT revision FROM subagent_controls WHERE task_id=? FOR UPDATE',(original['task_id'],))
        if (project is None or frozen is None or control is None
                or control['revision']!=proof.source_revision
                or cursor.execute('SELECT 1 FROM project_task_releases WHERE task_id=?',(original['task_id'],)).fetchone()):
            raise ConversionDenied()
        def verify_manifest(current_cursor, project_id, baseline):
            if current_cursor is not cursor or project_id!=project['project_id']:
                raise ConversionDenied()
            version=row(cursor,'SELECT * FROM project_versions WHERE project_id=? AND version_no=?',
                (project['project_id'],baseline['version_no']))
            if version is None or version['manifest_sha256']!=baseline['manifest_sha256']:
                raise ProjectConflict('Project baseline manifest changed')
            content=self.projects.blobs.read(version['manifest_sha256'],max_bytes=MAX_MANIFEST_BYTES)
            document=json.loads(content)
            if hashlib.sha256(manifest_bytes(document,quota_bytes=project['quota_bytes'])).hexdigest()!=baseline['manifest_sha256']:
                raise ProjectConflict('Project baseline manifest changed')
        ProjectTaskReleases._check_failed(cursor,project=project,binding=source,task=frozen,
            revision=control['revision'],now=now,verify_manifest=verify_manifest,lease_clock=lambda:int(self.clock()))
        # The reviewed source is now settled and frozen. Persist its explicit
        # release in this same transaction before handing the existing lock to
        # the target task; rollback must remove both the receipt and new owner.
        cursor.execute("INSERT INTO project_task_releases(task_id,revision,task_status,created_at) VALUES (?,?,'cancelled',?)",
            (original['task_id'],control['revision'],now))
        self.coordinator.store.append_checkpoint(original['task_id'],'project_migration_released',
            {'migration_item_id':proof.item_id,'revision':control['revision'],'target_task_id':task.task_id},
            now=now,transaction_cursor=cursor)
        cursor.execute('UPDATE subagent_tasks SET project_id=?,project_base_version=? WHERE task_id=?',
                       (source['project_id'], source['base_version'], task.task_id))
        request = digest(['migration-project-task', proof.item_id, proof.export_digest])
        cursor.execute('''INSERT INTO project_task_bindings
            (task_id,project_id,actor_principal_id,request_key,intent_sha256,base_version,manifest_sha256,generation,fence,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (task.task_id, source['project_id'], snapshot.principal_id, request, proof.export_digest,
             source['base_version'], source['manifest_sha256'], source['generation'], source['fence'], now))
        cursor.execute('''INSERT INTO qq_official_project_task_authority
            (task_id,project_id,grant_id,grant_revision,account_key,endpoint_key,actor_key,route_epoch,endpoint_route_epoch,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (task.task_id, source['project_id'], snapshot.grant.grant_id, snapshot.grant.revision,
             proof.target_endpoint.account.key, proof.target_endpoint.key, proof.target_actor.key,
             proof.logical_route_epoch, proof.endpoint_route_epoch, now))
        cursor.execute('''UPDATE projects SET locked_task_id=?,version=version+1,updated_at=?,last_active_at=?
            WHERE project_id=? AND locked_task_id=?''',
            (task.task_id, now, now, source['project_id'], original['task_id']))
        if cursor.rowcount != 1:
            raise ConversionDenied()

    def _extra_current(self, cursor, binding, task):
        item = row(cursor, 'SELECT * FROM migration_items WHERE item_id=?', (binding['item_id'],))
        if item is None or item['kind'] != 'project_task':
            raise ConversionDenied()
        data = json.loads(item['export_json'])
        original = row(cursor, 'SELECT * FROM project_task_bindings WHERE task_id=?', (binding['source_task_id'],))
        target = row(cursor, 'SELECT * FROM project_task_bindings WHERE task_id=?', (binding['target_task_id'],))
        authority = row(cursor, 'SELECT * FROM qq_official_project_task_authority WHERE task_id=?', (binding['target_task_id'],))
        release=row(cursor,'SELECT * FROM project_task_releases WHERE task_id=?',(binding['source_task_id'],))
        if (original is None or target is None or authority is None or release is None
                or release['revision']!=binding['source_revision'] or release['task_status']!='cancelled'):
            raise ConversionDenied()
        ref = self._grant(data)
        if (any(original[key] != target[key] for key in ('project_id', 'base_version', 'manifest_sha256', 'generation', 'fence'))
                or target['project_id'] != data['project_id'] or task['project_id'] != target['project_id']
                or authority['grant_id'] != ref.grant_id or authority['grant_revision'] != ref.revision
                or authority['account_key'] != binding['account_key'] or authority['endpoint_key'] != binding['endpoint_key']
                or authority['actor_key'] != binding['actor_key'] or authority['route_epoch'] != binding['logical_route_epoch']
                or authority['endpoint_route_epoch'] != binding['endpoint_route_epoch']):
            raise ConversionDenied()
        snapshot = self._grant_current(cursor, data=data, item=item,
            endpoint=EndpointKey.parse(binding['endpoint_key']), actor=ActorKey.parse(binding['actor_key']),
            logical_epoch=binding['logical_route_epoch'], endpoint_epoch=binding['endpoint_route_epoch'],
            phase='execute' if row(cursor, 'SELECT phase FROM migration_routes r JOIN migration_journals j USING(route_id) WHERE j.migration_id=?', (item['migration_id'],))['phase']=='completed' else 'prepare', now=int(self.clock()))
        if target['actor_principal_id'] != snapshot.principal_id:
            raise ConversionDenied()

    def require_task_source(self, cursor, *, task_id, job, purpose, now):
        binding = row(cursor, 'SELECT * FROM qq_official_subagent_dispatches WHERE task_id=?', (task_id,))
        task = row(cursor, 'SELECT * FROM subagent_tasks WHERE task_id=?', (task_id,))
        if binding is None or task is None or purpose not in {'execute', 'finalize', 'lifecycle'}:
            raise ConversionDenied()
        if purpose != 'lifecycle':
            self.assert_current(cursor, binding=binding, task=task, job=job, now=now)
        else:
            # A separately fenced lifecycle may finish an already accepted task.
            # It cannot borrow its terminal producer job to execute more models.
            if task['status'] not in {'completed', 'partial'}:
                raise ConversionDenied()
            proof = self.journal._issue_item(cursor, item_id=binding['migration_item_id'], operation='artifact', now=now)
            record = row(cursor, 'SELECT * FROM migration_subagent_continuations WHERE target_task_id=?', (task_id,))
            if record is None:
                raise ConversionDenied()
            self._same(record, proof)
            self._source_current(cursor, record)
        return True
