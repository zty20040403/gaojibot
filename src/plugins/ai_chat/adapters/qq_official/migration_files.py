"""Finite imports of reviewed legacy immutable files into the existing M6 queue.

No remote handle, old wire payload, fabricated task or native message ID crosses
the boundary. A copied artifact has its own M6 job; this import job only reads
already captured local bytes and is safe to replay by the same immutable key.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import weakref

from .file_artifacts import SourceBinding, _issue, detect_content
from .keys import account_job_kind
from .lease import assert_epoch
from .migration_preview import Selection, SourceRef
from .migration_types import MigrationDenied, digest, encoded
from .project_authority import ProjectGrantRef, Operation

PARKED = 2**63-1


def job_kind(account):
    return account_job_kind('file_import', account.key)


def _row(cursor, sql, args=()):
    value=cursor.execute(sql,args).fetchone()
    return dict(value) if value is not None else None


def _deny():
    raise MigrationDenied('migration_file_unavailable')


class MigrationFiles:
    kind='migration'
    source_kind='file'

    def __init__(self,database,*,token,jobs,journal,preview,admission,clock,database_calls,
                 artifact_store=None,legacy_snapshot_root=None,projects=None,project_grants=None):
        self.database,self.token,self.jobs,self.journal=database,token,jobs,journal
        self.preview,self.admission,self.clock,self.calls=preview,admission,clock,database_calls
        self.artifacts,self.projects,self.project_grants=artifact_store,projects,project_grants
        self.legacy_root=Path(legacy_snapshot_root) if legacy_snapshot_root is not None else None
        self._bindings=weakref.WeakKeyDictionary()
        self._lock=threading.Lock()

    @contextmanager
    def tx(self):
        connection=self.database.store_connection();cursor=connection.cursor()
        try:
            yield cursor
            connection.commit()
        except BaseException:
            connection.rollback();raise
        finally:
            cursor.close();connection.close()

    def _now(self):
        import math
        value=self.clock()
        if type(value) not in {int,float} or not math.isfinite(value) or not 0<=value<2**63:_deny()
        return int(value)

    def _require(self,cursor,proof,operation,now,job=None):
        self.journal.require_item(cursor,proof,operation=operation,now=now,job=job)
        assert_epoch(cursor,self.token)
        if (proof.source_kind!='file' or proof.target_endpoint.account.key!=self.token.account_key
                or self.admission(cursor,proof.target_endpoint,proof.target_actor) is not True):_deny()

    def _source(self,cursor,source_ref,*,lock=False):
        origin,key=source_ref.split(':',1)
        suffix=' FOR UPDATE' if lock else ''
        if origin=='subagent':
            task_id,revision,sha=key.split(':');task_id,revision=int(task_id),int(revision)
            task=_row(cursor,'SELECT * FROM subagent_tasks WHERE task_id=?'+suffix,(task_id,))
            control=_row(cursor,'SELECT * FROM subagent_controls WHERE task_id=?'+suffix,(task_id,))
            row=_row(cursor,'SELECT * FROM subagent_deliveries WHERE task_id=? AND revision=? AND delivery_key=?'+suffix,
                     (task_id,revision,sha))
            if (not task or not control or not row or task['account_key'] is not None or task['project_id'] is not None
                    or task['cancel_requested'] or control['revision']!=revision):_deny()
            payload=json.loads(row['payload_json']);artifact=payload['artifact']
            if artifact.get('snapshot')!=sha:_deny()
            return {'origin':origin,'task_id':task_id,'revision':revision,'sha256':sha,'byte_size':artifact['size'],
                'filename':payload['filename'],'draft':payload.get('draft',False),'draft_reason':payload.get('draft_reason',''),
                'state':row['state'],'payload_digest':digest(payload),'scope_key':task['scope_key'],
                'actor_id':task['requester_user_id'],'bot_id':json.loads(control['dispatch_json']).get('bot_id')}
        if origin=='project':
            if lock:
                # Match the legacy writer/collector lock order; never hold an
                # operation/delivery row while waiting for its project scope.
                selected=_row(cursor,'''SELECT p.scope_key FROM project_file_deliveries f
                    JOIN projects p USING(project_id) WHERE f.delivery_id=?''',(int(key),))
                if selected is None:_deny()
                cursor.execute('SELECT pg_advisory_xact_lock(hashtextextended(?,0))',
                    ('project-scope:'+selected['scope_key'],))
            delivery=_row(cursor,'SELECT * FROM deliveries WHERE delivery_id=?'+suffix,(int(key),))
            row=_row(cursor,'SELECT * FROM project_file_deliveries WHERE delivery_id=?'+suffix,(int(key),))
            if not row or not delivery:_deny()
            project=_row(cursor,'SELECT * FROM projects WHERE project_id=?'+suffix,(row['project_id'],))
            operation=_row(cursor,'SELECT * FROM project_io WHERE operation_id=?'+suffix,(row['operation_id'],))
            if (not project or not operation or operation['state']!='completed' or operation['tool_name']!='send_file_from_sandbox'
                    or row['upload_started_at'] is not None or row['blob_sha256']!=row['sha256']
                    or delivery['account_key'] is not None or delivery['attempts']!=0
                    or cursor.execute('SELECT 1 FROM delivery_attempts WHERE delivery_id=?',(int(key),)).fetchone()):_deny()
            return {'origin':origin,'delivery_id':int(key),'project_id':row['project_id'],'operation_id':row['operation_id'],
                'sha256':row['sha256'],'byte_size':row['byte_size'],'filename':row['filename'],
                'draft':False,'draft_reason':'','state':delivery['status'],'scope_key':project['scope_key'],
                'actor_principal_id':operation['actor_principal_id'],'bot_id':project['bot_native_user_id'],
                'operation_digest':digest({k:operation[k] for k in ('operation_id','project_id','tool_name','state','actor_principal_id')})}
        _deny()

    def _source_current(self,cursor,b):
        source=self._source(cursor,b['source_ref'])
        if digest(source)!=b['source_frozen_digest']:_deny()
        return source

    def _project(self,cursor,proof,source,ref,now):
        if source['origin']!='project':return
        if self.projects is None or self.project_grants is None or type(ref) is not ProjectGrantRef:_deny()
        _,route=self.journal._load(cursor,proof.migration_id)
        phase='prepare' if route['phase'] in {'switching','verifying'} else 'deliver'
        self.project_grants.require_staged(cursor,proof=proof,project_id=source['project_id'],grant=ref,
            phase=phase,now=now,operation=Operation.DELIVER)

    def _same(self,b,proof):
        fields=dict(item_id=proof.item_id,source_ref=proof.source_key,source_revision=proof.source_revision,
            export_digest=proof.export_digest,account_key=proof.target_endpoint.account.key,endpoint_key=proof.target_endpoint.key,
            actor_key=proof.target_actor.key,endpoint_generation=proof.endpoint_generation,
            endpoint_route_epoch=proof.endpoint_route_epoch,logical_route_epoch=proof.route_epoch,
            mapping_id=proof.mapping_id,mapping_revision=proof.mapping_revision,
            execution_deadline=proof.execution_deadline,result_deadline=proof.result_deadline)
        if any(b[k]!=v for k,v in fields.items()):_deny()

    def _current(self,cursor,b,proof,now):
        self._same(b,proof)
        source=self._source_current(cursor,b)
        self._project(cursor,proof,source,ProjectGrantRef(b['grant_id'],b['grant_revision']) if b['grant_id'] else None,now)
        return source

    def prepare(self,cursor,proof,*,now):
        self._require(cursor,proof,'prepare',now)
        b=_row(cursor,'SELECT * FROM migration_file_continuations WHERE item_id=?',(proof.item_id,))
        if b:self._current(cursor,b,proof,now);return b
        if (self.artifacts is None or proof.execution_deadline is None or proof.result_deadline is None
                or not now<proof.execution_deadline<=proof.result_deadline<=now+86400):_deny()
        source=self._source(cursor,proof.source_key,lock=True)
        if source['origin']=='subagent' and self.legacy_root is None:_deny()
        value=self.preview.preview(cursor,Selection(proof.source_endpoint,proof.source_scope,proof.target_endpoint),
            SourceRef('file',proof.source_key),now=now)
        if value.disposition!='unsent' or value.safe_digest!=proof.export_digest:_deny()
        data=value.export
        if (source['state'] not in {'pending','queued','failed'} or source['sha256']!=data['immutable_ref']['sha256']
                or source['byte_size']!=data['byte_size'] or source['filename']!=data['filename']):_deny()
        grant_data=data['mapping'].get('project_grant')
        grant=ProjectGrantRef(**grant_data) if grant_data is not None else None
        self._project(cursor,proof,source,grant,now)
        if source['origin']=='subagent':
            cursor.execute("UPDATE subagent_deliveries SET state='migrated',updated_at=? WHERE task_id=? AND revision=? AND delivery_key=?",
                (now,source['task_id'],source['revision'],source['sha256']))
        else:
            cursor.execute("UPDATE deliveries SET status='cancelled',last_error='migrated immutable file',updated_at=? WHERE delivery_id=?",
                (now,source['delivery_id']))
        source=self._source(cursor,proof.source_key)
        job,_=self.jobs.enqueue(kind=job_kind(proof.target_endpoint.account),idempotency_key='qq-file-import:'+proof.item_id,
            scope_key=proof.target_endpoint.scope.key,payload={'item_id':proof.item_id},now=now,run_at=PARKED,
            max_attempts=3,resume_on_lease_loss=True,transaction_cursor=cursor)
        fields=dict(item_id=proof.item_id,origin=source['origin'],source_ref=proof.source_key,source_revision=proof.source_revision,
            source_frozen_digest=digest(source),export_digest=proof.export_digest,source_json=encoded(data),
            account_key=proof.target_endpoint.account.key,endpoint_key=proof.target_endpoint.key,actor_key=proof.target_actor.key,
            endpoint_generation=proof.endpoint_generation,endpoint_route_epoch=proof.endpoint_route_epoch,logical_route_epoch=proof.route_epoch,
            mapping_id=proof.mapping_id,mapping_revision=proof.mapping_revision,execution_deadline=proof.execution_deadline,
            result_deadline=proof.result_deadline,job_id=job.job_id,project_id=source.get('project_id'),
            grant_id=grant.grant_id if grant else None,grant_revision=grant.revision if grant else None,created_at=now)
        cursor.execute('INSERT INTO migration_file_continuations ('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+')',tuple(fields.values()))
        self.journal.bind_item_job(cursor,proof,job_id=job.job_id,expected_kind=job.kind,now=now)
        return _row(cursor,'SELECT * FROM migration_file_continuations WHERE item_id=?',(proof.item_id,))

    def activate(self,cursor,proof,*,now):
        self._require(cursor,proof,'activate',now)
        b=_row(cursor,'SELECT * FROM migration_file_continuations WHERE item_id=?',(proof.item_id,))
        if b is None:_deny()
        self._current(cursor,b,proof,now)
        self.journal.activate_item(cursor,proof,due_at=b['created_at'],now=now)

    def validate_bound_job(self,cursor,item,job,*,operation,now,due_at=None):
        key=item.item_id if hasattr(item,'item_id') else item['item_id']
        job_id=job.job_id if hasattr(job,'job_id') else job['job_id']
        b=_row(cursor,'SELECT * FROM migration_file_continuations WHERE item_id=? AND job_id=?',(key,job_id))
        if b is None or (due_at is not None and due_at!=b['created_at']):_deny()
        if operation!='expire':self._source_current(cursor,b)

    def validate_bound_plan(self,*args,**kwargs):_deny()

    def current_plan_source(self,cursor,plan_id,now):
        return False  # All file plans use the exact artifact journal path.

    def _issue_source(self,cursor,job,now):
        proof=self.journal.issue_item(cursor,item_id=job.payload['item_id'],operation='execute',job=job,now=now)
        self._require(cursor,proof,'execute',now,job)
        b=_row(cursor,'SELECT * FROM migration_file_continuations WHERE item_id=?',(proof.item_id,))
        if b is None:_deny()
        self._current(cursor,b,proof,now)
        binding=SourceBinding(self.token,job,self.admission,None,None,proof.result_deadline,proof.target_endpoint,
            proof.target_actor,None,'file-import:'+proof.item_id,proof.execution_deadline,None,self,proof.item_id)
        with self._lock:self._bindings[binding]=proof.item_id
        return _issue(binding),b

    def current_source(self,cursor,binding,now):
        with self._lock:item_id=self._bindings.get(binding)
        if item_id is None or binding.extension is not self or binding.token is not self.token:_deny()
        proof=self.journal.issue_item(cursor,item_id=item_id,operation='execute',job=binding.job,now=now)
        self._require(cursor,proof,'execute',now,binding.job)
        b=_row(cursor,'SELECT * FROM migration_file_continuations WHERE item_id=?',(item_id,))
        if b is None:_deny()
        self._current(cursor,b,proof,now)
        if (binding.endpoint!=proof.target_endpoint or binding.actor!=proof.target_actor or binding.task_id is not None
                or binding.source_execution_id is not None or binding.source_native_message_id is not None
                or binding.deadline_at!=proof.execution_deadline or binding.pending_deadline_at!=proof.result_deadline):_deny()
        return dict(account_key=proof.target_endpoint.account.key,endpoint_key=proof.target_endpoint.key,
            requester_actor_key=proof.target_actor.key,source_canonical_message_id=None,source_native_message_id=None,
            source_scope_key=proof.target_endpoint.scope.key,endpoint_generation=proof.endpoint_generation,
            route_epoch=proof.endpoint_route_epoch)

    @staticmethod
    def _artifact_digest(artifact,b):
        from .migration_artifacts import _MANIFEST
        return digest([{k:artifact[k] for k in _MANIFEST},{k:b[k] for k in (
            'item_id','source_frozen_digest','export_digest','job_id','mapping_id','mapping_revision','logical_route_epoch','result_deadline')}])

    def bind_artifact(self,cursor,binding,artifact,*,now,existing):
        self.current_source(cursor,binding,now)
        b=_row(cursor,'SELECT * FROM migration_file_continuations WHERE item_id=?',(binding.source_migration_item_id,))
        fields=dict(artifact_id=artifact['artifact_id'],source_extension_kind='migration',source_kind='file',item_id=b['item_id'],
            task_id=None,task_revision=None,source_job_id=b['job_id'],account_key=b['account_key'],endpoint_key=b['endpoint_key'],
            actor_key=b['actor_key'],logical_route_epoch=b['logical_route_epoch'],mapping_id=b['mapping_id'],mapping_revision=b['mapping_revision'],
            source_digest=self._artifact_digest(artifact,b),result_deadline=b['result_deadline'],created_at=artifact['created_at'])
        if existing:
            row=_row(cursor,'SELECT * FROM qq_official_migration_artifacts WHERE artifact_id=?',(artifact['artifact_id'],))
            if row is None or b['artifact_id']!=artifact['artifact_id'] or any(row[k]!=v for k,v in fields.items()):_deny()
        else:
            if b['artifact_id'] is not None:_deny()
            cursor.execute('INSERT INTO qq_official_migration_artifacts ('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+')',tuple(fields.values()))
            cursor.execute('UPDATE migration_file_continuations SET artifact_id=? WHERE item_id=?',(artifact['artifact_id'],b['item_id']))

    def validate_bound_artifact(self,cursor,item,artifact_id,*,now):
        assert_epoch(cursor,self.token)
        b=_row(cursor,'SELECT * FROM migration_file_continuations WHERE item_id=? AND artifact_id=?',(item.item_id,artifact_id))
        a=_row(cursor,'SELECT * FROM qq_official_file_artifacts WHERE artifact_id=?',(artifact_id,))
        side=_row(cursor,'SELECT * FROM qq_official_migration_artifacts WHERE artifact_id=?',(artifact_id,))
        if b is None or a is None or side is None:_deny()
        self._current(cursor,b,item,now)
        data=json.loads(b['source_json'])
        if (side['source_kind']!='file' or side['source_digest']!=self._artifact_digest(a,b)
                or a['source_task_id'] is not None or a['source_task_revision'] is not None
                or a['source_execution_id'] is not None or a['source_native_message_id'] is not None
                or a['source_canonical_message_id'] is not None or a['source_job_id']!=b['job_id']
                or a['source_migration_item_id']!=b['item_id'] or a['sha256']!=data['immutable_ref']['sha256']
                or a['byte_size']!=data['byte_size'] or a['pending_deadline_at']!=b['result_deadline']
                or self.admission(cursor,item.target_endpoint,item.target_actor) is not True):_deny()

    def _read(self,source):
        sha,size=source['sha256'],source['byte_size']
        if source['origin']=='project':
            if self.projects is None:_deny()
            content=self.projects.blobs.read(sha)
        else:
            if self.legacy_root is None:_deny()
            # Each fixed host directory is opened without following a link;
            # neither an old artifact handle nor a model path enters this read.
            root=os.open(self.legacy_root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:
                directory=os.open(str(source['task_id']),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root)
                try:
                    fd=os.open(sha,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
                    with os.fdopen(fd,'rb') as stream:
                        info=os.fstat(stream.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_size!=size or info.st_uid!=os.geteuid():_deny()
                        content=stream.read(size+1)
                finally:os.close(directory)
            finally:os.close(root)
        if len(content)!=size or hashlib.sha256(content).hexdigest()!=sha:_deny()
        detect_content(content,source['filename'],self.artifacts.max_bytes)
        return content

    def _import(self,job):
        with self.tx() as cursor:
            binding,b=self._issue_source(cursor,job,self._now())
            if b['artifact_id'] is not None:
                self.journal.current_artifact_source(cursor,b['artifact_id'],now=self._now())
                return
            source=self._source_current(cursor,b)
        content=self._read(source)
        self.artifacts.register(binding,content=content,filename=source['filename'],
            logical_request_key=digest(['migration-file-import-v1',b['item_id'],b['export_digest']]),
            draft=source['draft'],draft_reason=source['draft_reason'])

    async def execute(self,job):
        try:await self.calls(self._import,job)
        except Exception:
            # Fixed diagnostics; retry is local idempotent CAS only, never a
            # replay of the old platform upload/message request.
            raise MigrationDenied('migration_file_import_unavailable') from None
