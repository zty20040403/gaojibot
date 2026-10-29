"""M5 ordinary task continuation: real stores, real Coordinator, original queue.

The injected Journal is mandatory authority; BackgroundToolSource is metadata.
All cursor methods join the caller's transaction; no commits or network calls.
"""
from contextlib import contextmanager
import hashlib
import json

from .migration_preview import Selection, SourceRef
from src.plugins.ai_chat.agent.contracts import ContextPacket
from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.subagent_runtime import job_kind, TEXT_TOOLS, FILE_TOOLS, FILE_ROLES, TERMINAL
from src.plugins.ai_chat.platform_types import BackgroundToolSource
from src.plugins.ai_chat.subagents import _validate_plan

PARKED=2**63-1

def encoded(value):
    return json.dumps(value,sort_keys=True,separators=(',', ':'),ensure_ascii=False)

def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()

def row(cursor,sql,args=()):
    value=cursor.execute(sql,args).fetchone()
    return dict(value) if value is not None else None

class ConversionDenied(ValueError):
    def __init__(self):
        super().__init__('migration_subagent_unavailable')

class SubAgentContinuations:
    domain_kind = "subagent"

    def __init__(self,database,*,token,jobs,coordinator,journal,preview,profile,admission,clock,file_admission=None):
        self.database,self.token,self.jobs,self.coordinator=database,token,jobs,coordinator
        self.journal,self.preview,self.profile,self.admission,self.clock=journal,preview,profile,admission,clock
        self.file_admission=file_admission

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

    def _require(self,cursor,proof,operation,now,job=None,plan_id=None):
        if type(now) is not int or now<0:
            raise ConversionDenied()
        self.journal.require_item(cursor,proof,operation=operation,now=now,job=job,
            **({'plan_id':plan_id} if plan_id is not None else {}))
        self._admission_current(cursor,proof)

    def _admission_current(self,cursor,proof):
        # This checks only this host adapter. It does not authorize a proof;
        # callers must first issue or revalidate it through Journal in this TX.
        assert_epoch(cursor,self.token)
        if proof.source_kind!=self.domain_kind or proof.target_endpoint.account.key!=self.token.account_key:
            raise ConversionDenied()
        if self.admission(cursor,proof.target_endpoint,proof.target_actor) is not True:
            raise ConversionDenied()

    def _source_digest(self,cursor,task_id):
        task=row(cursor,'SELECT * FROM subagent_tasks WHERE task_id=?',(task_id,))
        control=row(cursor,'SELECT version,revision,dispatch_json FROM subagent_controls WHERE task_id=?',(task_id,))
        runs=[dict(r) for r in cursor.execute('''SELECT run_id,step_key,role,objective,deliverable,
            dependencies_json,allowed_tools_json,status,attempt,result_json FROM subagent_runs
            WHERE task_id=? ORDER BY run_id''',(task_id,)).fetchall()]
        if task is None or control is None:
            raise ConversionDenied()
        return digest([{k:task[k] for k in ('task_id','scope_key','requester_user_id','trigger_message_id',
            'status','objective','plan_json','result_json','cancel_requested','project_id')},control,runs])

    def _same(self,b,proof):
        expected={'item_id':proof.item_id,'export_digest':proof.export_digest,
            'account_key':proof.target_endpoint.account.key,'endpoint_key':proof.target_endpoint.key,
            'actor_key':proof.target_actor.key,'endpoint_generation':proof.endpoint_generation,
            'endpoint_route_epoch':proof.endpoint_route_epoch,'logical_route_epoch':proof.logical_route_epoch,
            'mapping_id':proof.mapping_id,'mapping_revision':proof.mapping_revision,
            'source_task_id':int(proof.source_key),'source_revision':proof.source_revision,
            'execution_deadline':proof.execution_deadline,'result_deadline':proof.result_deadline}
        if any(b[k]!=v for k,v in expected.items()):
            raise ConversionDenied()

    def _source_current(self,cursor,b):
        if self._source_digest(cursor,b['source_task_id'])!=b['source_frozen_digest']:
            raise ConversionDenied()
        target=row(cursor,'SELECT * FROM subagent_tasks WHERE task_id=?',(b['target_task_id'],))
        control=row(cursor,'SELECT revision,dispatch_json FROM subagent_controls WHERE task_id=?',(b['target_task_id'],))
        if (target is None or control is None or target['migration_item_id']!=b['item_id']
                or target['endpoint_key']!=b['endpoint_key'] or target['requester_actor_key']!=b['actor_key']
                or target['account_key']!=b['account_key'] or target['trigger_message_id'] is not None
                or target['requester_user_id'] is not None or not self._target_kind(target)
                or target['cancel_requested'] or target['status']=='cancelled' or control['revision']!=1):
            raise ConversionDenied()
        dispatch=json.loads(control['dispatch_json'])
        tool_profile=b['tool_profile']
        if (tool_profile not in self._profiles() or dispatch.get('tool_profile')!=tool_profile
                or dispatch.get('profile')!=b['profile'] or dispatch.get('migration_item_id')!=b['item_id']):
            raise ConversionDenied()
        if tool_profile in {'official-files-v1','official-projects-v1'}:
            self._files_current(cursor,item_id=b['item_id'],endpoint=target['endpoint_key'],actor=target['requester_actor_key'],now=int(self.clock()))
        self._extra_current(cursor,b,target)

    def _lock_source(self, cursor, proof):
        return None

    def _profiles(self):
        return {'official-text-v1','official-files-v1'}

    def _profile(self, files):
        return 'official-files-v1' if files else 'official-text-v1'

    def _original_kind(self, task):
        return task['project_id'] is None

    def _target_kind(self, task):
        return task['project_id'] is None

    def _extra_current(self, cursor, binding, task):
        return None

    def _prepare_target(self, cursor, proof, original, data, now):
        return None

    def _bind_target(self, cursor, proof, original, data, task, project_context, now):
        return None

    def _files_current(self,cursor,*,item_id,endpoint,actor,now):
        from src.plugins.ai_chat.platform_types import EndpointKey,ActorKey
        endpoint=EndpointKey.parse(endpoint) if isinstance(endpoint,str) else endpoint
        actor=ActorKey.parse(actor) if isinstance(actor,str) else actor
        if self.file_admission is None or self.file_admission(cursor,item_id=item_id,endpoint=endpoint,actor=actor,now=now) is not True:
            raise ConversionDenied()

    def prepare(self,cursor,proof,*,now):
        self._require(cursor,proof,'prepare',now)
        saved=row(cursor,'SELECT * FROM migration_subagent_continuations WHERE item_id=? FOR UPDATE',(proof.item_id,))
        if saved:
            self._same(saved,proof);self._source_current(cursor,saved);return saved
        self._lock_source(cursor,proof)
        original=row(cursor,'SELECT * FROM subagent_tasks WHERE task_id=? FOR UPDATE',(int(proof.source_key),))
        if (not original or not self._original_kind(original) or original['account_key'] is not None
                or cursor.execute('SELECT 1 FROM routine_children WHERE task_id=?',(int(proof.source_key),)).fetchone()):
            raise ConversionDenied()
        cursor.execute('SELECT * FROM subagent_controls WHERE task_id=? FOR UPDATE',(original['task_id'],)).fetchone()
        cursor.execute('SELECT run_id FROM subagent_runs WHERE task_id=? ORDER BY run_id FOR UPDATE',(original['task_id'],)).fetchall()
        oldjobs=cursor.execute("""SELECT * FROM durable_jobs WHERE kind='subagent.workflow'
            AND payload_json::jsonb->>'task_id'=? ORDER BY job_id FOR UPDATE""",(str(original['task_id']),)).fetchall()
        if any(j['status']=='running' or (j['attempts']>0 and j['status']!='succeeded') for j in oldjobs):
            raise ConversionDenied()
        export=self.preview.preview(cursor,Selection(proof.source_endpoint,proof.source_scope,proof.target_endpoint),
            SourceRef(self.domain_kind,proof.source_key),now=now)
        if export.disposition!='unsent' or export.safe_digest!=proof.export_digest:
            raise ConversionDenied()
        data=export.export
        if (data['mapping']['mapping_id']!=proof.mapping_id or data['mapping']['revision']!=proof.mapping_revision
                or data['source_revision']!=proof.source_revision or data['execution_deadline']!=proof.execution_deadline
                or not now<proof.result_deadline<=now+86400 or proof.execution_deadline>proof.result_deadline):
            raise ConversionDenied()
        project_context=self._prepare_target(cursor,proof,original,data,now)
        terminal=data['source_task_status'] in {'completed','partial'}
        if not terminal and now>=proof.execution_deadline:
            raise ConversionDenied()
        steps=data['steps']
        contract=data['task_contract']
        files=bool((contract is not None and contract['delivery_required']) or any(
            s['role'] in {'coder','document'} or set(s['required_tools'])&FILE_TOOLS for s in steps))
        tool_profile=self._profile(files)
        files=files or tool_profile=='official-projects-v1'
        if files:
            self._files_current(cursor,item_id=proof.item_id,endpoint=proof.target_endpoint,actor=proof.target_actor,now=now)
        allowed=TEXT_TOOLS|FILE_TOOLS if files else TEXT_TOOLS
        roles=FILE_ROLES if files else {'researcher','analyst'}
        # Old immutable artifacts require a separate reviewed import grant;
        # this path only starts clean, bounded work using the current file port.
        if (any(s['immutable_artifact_refs'] or s['role'] not in roles or not set(s['required_tools'])<=allowed
                or s['state'] not in {'pending','succeeded','partial'} for s in steps)
                or (not terminal and not steps)):
            raise ConversionDenied()
        if contract is not None and any(c['kind']!='evidence' for c in contract['outcome_checks']):
            raise ConversionDenied()
        plan={'mode':data['plan_mode'],'steps':[{'id':s['step_key'],'agent':s['role'],
            'objective':s['objective'],'deliverable':s['deliverable'],'depends_on':s['dependencies'],
            'required_tools':s['required_tools'],'optional':s['optional']} for s in steps]}
        if contract is not None:plan['contract']=contract
        validated=_validate_plan(plan,data['objective'],self.coordinator.max_steps,strict=bool(steps)) if steps else []
        if any(x.key!=s['step_key'] or x.objective!=s['objective'] or x.deliverable!=s['deliverable'] for x,s in zip(validated,steps)):
            raise ConversionDenied()
        # Retain old rows and successful evidence. Only never-started work is frozen.
        cursor.execute("UPDATE subagent_tasks SET cancel_requested=TRUE,status=CASE WHEN status IN ('completed','partial') THEN status ELSE 'cancelled' END,updated_at=?,finished_at=COALESCE(finished_at,?) WHERE task_id=?",(now,now,original['task_id']))
        cursor.execute("UPDATE subagent_runs SET status='skipped',last_error='migration_frozen',finished_at=? WHERE task_id=? AND status='pending' AND attempt=0",(now,original['task_id']))
        for old in oldjobs:
            cursor.execute("UPDATE durable_jobs SET status='cancelled',finished_at=?,updated_at=? WHERE job_id=? AND status='pending'",(now,now,old['job_id']))
        source_digest=self._source_digest(cursor,original['task_id'])
        packet=ContextPacket(proof.target_endpoint.scope.key,'migration-task:'+proof.item_id,None,None,
            data['objective'],endpoint=proof.target_endpoint,requester_actor=proof.target_actor,
            constraints=tuple(contract['constraints']) if contract else ())
        task=self.coordinator.store.prepare_task(packet=packet,decision=None,
            dispatch={'version':1,'platform':'qq-official','deadline':proof.execution_deadline,
                'profile':self.profile,'tool_profile':tool_profile,'migration_item_id':proof.item_id},
            max_parallelism=self.coordinator.max_parallelism,max_steps=self.coordinator.max_steps,
            migration_item_id=proof.item_id,now=now,transaction_cursor=cursor)
        self._bind_target(cursor,proof,original,data,task,project_context,now)
        cursor.execute('UPDATE subagent_tasks SET plan_json=? WHERE task_id=?',(encoded(plan),task.task_id))
        pending,_=self.jobs.enqueue(kind=job_kind(proof.target_endpoint.account),
            idempotency_key='migration-subagent:'+digest([proof.item_id,proof.export_digest]),
            scope_key=proof.target_endpoint.scope.key,payload={'task_id':task.task_id,'revision':1},
            now=now,max_attempts=5,resume_on_lease_loss=True,run_at=PARKED,transaction_cursor=cursor)
        cursor.execute('''INSERT INTO qq_official_subagent_dispatches
            (task_id,account_key,endpoint_key,requester_actor_key,submission_key,submission_digest,job_id,
             endpoint_generation,route_epoch,tool_profile,execution_deadline_at,result_wait_seconds,created_at,updated_at,
             migration_item_id,migration_result_deadline_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,86400,?,?,?,?)''',
            (task.task_id,self.token.account_key,proof.target_endpoint.key,proof.target_actor.key,
             digest(['migration',proof.item_id]),proof.export_digest,pending.job_id,
             proof.endpoint_generation,proof.endpoint_route_epoch,tool_profile,proof.execution_deadline,now,now,proof.item_id,proof.result_deadline))
        cursor.execute('''INSERT INTO migration_subagent_continuations
            (item_id,target_task_id,source_task_id,source_revision,source_control_version,source_frozen_digest,
             export_digest,account_key,endpoint_key,actor_key,endpoint_generation,endpoint_route_epoch,
             logical_route_epoch,mapping_id,mapping_revision,job_id,execution_deadline,result_deadline,
             imported_terminal,profile,created_at,tool_profile) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (proof.item_id,task.task_id,original['task_id'],proof.source_revision,data['control_version'],source_digest,
             proof.export_digest,self.token.account_key,proof.target_endpoint.key,proof.target_actor.key,
             proof.endpoint_generation,proof.endpoint_route_epoch,proof.logical_route_epoch,proof.mapping_id,
             proof.mapping_revision,pending.job_id,proof.execution_deadline,proof.result_deadline,terminal,self.profile,now,tool_profile))
        for step,s in zip(validated,steps):
            target=self.coordinator.store.create_run(task.task_id,step,allowed_tools=s['required_tools'],
                model_profile=self.profile,now=now,transaction_cursor=cursor)
            if s['state']!='pending':
                cursor.execute('UPDATE subagent_runs SET status=?,result_json=?,finished_at=? WHERE run_id=?',
                    (s['state'],encoded({'status':'success' if s['state']=='succeeded' else 'partial',
                      'summary':s['completed_summary'],'artifacts':[]}),now,target.run_id))
            cursor.execute('INSERT INTO migration_subagent_run_provenance VALUES (?,?,?,?)',
                (proof.item_id,s['source_run_id'],target.run_id,digest(s)))
        if terminal:
            cursor.execute('UPDATE subagent_tasks SET status=?,result_json=?,finished_at=? WHERE task_id=?',
                (data['source_task_status'],encoded({'answer':data['frozen_final_text']}),now,task.task_id))
        self.journal.bind_item_job(cursor,proof,job_id=pending.job_id,expected_kind=pending.kind,now=now)
        return row(cursor,'SELECT * FROM migration_subagent_continuations WHERE item_id=?',(proof.item_id,))

    def activate(self,cursor,proof,*,now):
        self._require(cursor,proof,'activate',now)
        b=row(cursor,'SELECT * FROM migration_subagent_continuations WHERE item_id=?',(proof.item_id,))
        if not b:raise ConversionDenied()
        self._same(b,proof);self._source_current(cursor,b)
        self.journal.activate_item(cursor,proof,due_at=b['created_at'],now=now)

    def validate_bound_job(self,cursor,item,job,*,operation,now,due_at=None):
        b=row(cursor,'SELECT * FROM migration_subagent_continuations WHERE item_id=? AND job_id=?',
            (item.item_id if hasattr(item,'item_id') else item['item_id'],job.job_id if hasattr(job,'job_id') else job['job_id']))
        if not b or (due_at is not None and due_at!=b['created_at']):raise ConversionDenied()
        if operation!='expire':self._source_current(cursor,b)

    def validate_bound_plan(self,cursor,item,plan_id,*,now):
        b=row(cursor,'SELECT * FROM migration_subagent_continuations WHERE item_id=? AND plan_id=?',(item.item_id if hasattr(item,'item_id') else item['item_id'],plan_id))
        p=row(cursor,'SELECT * FROM delivery_plans WHERE plan_id=?',(plan_id,))
        if (not b or not p or p['account_key']!=b['account_key'] or p['endpoint_key']!=b['endpoint_key']
                or p['requester_actor_key']!=b['actor_key'] or json.loads(p['manifest_json'])['source_native_message_id']!=''
                or p['source_canonical_message_id'] is not None or p['expires_at']>b['result_deadline']):
            raise ConversionDenied()
        self._source_current(cursor,b)

    def assert_current(self,cursor,*,binding,task,job,now):
        try:
            if type(now) is not int or now < 0:
                raise ConversionDenied()
            b=row(cursor,'SELECT * FROM migration_subagent_continuations WHERE target_task_id=?',(task['task_id'],))
            if not b or b['item_id']!=binding['migration_item_id'] or b['job_id']!=binding['job_id']:
                raise ConversionDenied()
            op='send' if task['status'] in TERMINAL else 'execute'
            plan_id=b['plan_id']
            if job is None:
                if plan_id is None:raise ConversionDenied()
                op='deliver'
            proof=self.journal.issue_item(cursor,item_id=b['item_id'],operation=op,job=job,now=now,
                **({'plan_id':plan_id} if op=='deliver' else {}))
            # issue_item has just checked the exact persisted job/plan and
            # invoked our bound-job/plan source validator on this cursor. No
            # mutation or await occurs here. Do not recursively reissue the
            # same proof and repeat that validator twice more.
            self._admission_current(cursor,proof)
            self._same(b,proof)
        except ValueError:
            raise LeaseLost('migration_subagent_authority_unavailable') from None

    def enqueue_proof(self,cursor,*,binding,job,now):
        proof=self.journal.issue_item(cursor,item_id=binding['migration_item_id'],operation='send',job=job,now=now)
        self._require(cursor,proof,'send',now,job=job)
        return proof

    def bind_plan(self,cursor,*,binding,plan_id,job,now):
        proof=self.journal.issue_item(cursor,item_id=binding['migration_item_id'],operation='send',job=job,now=now)
        self._require(cursor,proof,'send',now,job=job)
        cursor.execute('UPDATE migration_subagent_continuations SET plan_id=? WHERE item_id=? AND plan_id IS NULL',
            (plan_id,proof.item_id))
        if cursor.rowcount!=1:raise ConversionDenied()
        self.journal.bind_item_plan(cursor,proof,plan_id=plan_id,job=job,now=now)

    def current_plan_source(self,cursor,plan_id,now):
        b=row(cursor,'SELECT * FROM migration_subagent_continuations WHERE plan_id=?',(plan_id,))
        if not b:return False
        proof=self.journal.issue_item(cursor,item_id=b['item_id'],operation='deliver',plan_id=plan_id,now=now)
        self._require(cursor,proof,'deliver',now,plan_id=plan_id)
        self._same(b,proof);self.validate_bound_plan(cursor,{'item_id':b['item_id']},plan_id,now=now)
        return True

    def begin_job(self,cursor,*,job,now):
        b=row(cursor,'SELECT * FROM migration_subagent_continuations WHERE job_id=? FOR UPDATE',(job.job_id,))
        if b is None:return None  # Unmigrated M4 path stays with its existing authority.
        task=row(cursor,'SELECT * FROM subagent_tasks WHERE task_id=? FOR UPDATE',(b['target_task_id'],))
        binding=row(cursor,'SELECT * FROM qq_official_subagent_dispatches WHERE task_id=?',(b['target_task_id'],))
        self.assert_current(cursor,binding=binding,task=task,job=job,now=now)
        if task['status'] in TERMINAL:return True
        old_generation=b['execution_job_generation']
        if old_generation is not None and (old_generation!=job.lease_generation or b['execution_job_owner']!=job.lease_owner):
            uncertain=cursor.execute("SELECT 1 FROM subagent_runs WHERE task_id=? AND (attempt>0 OR status IN ('running','interrupted')) LIMIT 1",(task['task_id'],)).fetchone()
            # Nonterminal supervisor/model may also have run after all steps completed.
            if uncertain or task['status'] not in {'queued'}:
                cursor.execute("UPDATE qq_official_subagent_dispatches SET suppressed_reason='migration_execution_unknown',updated_at=? WHERE task_id=? AND plan_id IS NULL",(now,task['task_id']))
                cursor.execute("UPDATE subagent_tasks SET status='interrupted',last_error='migration_execution_unknown' WHERE task_id=?",(task['task_id'],))
                return False
        cursor.execute('UPDATE migration_subagent_continuations SET execution_job_generation=?,execution_job_owner=? WHERE item_id=?',(job.lease_generation,job.lease_owner,b['item_id']))
        return True

    def tool_source(self,cursor,*,task_id,job,now):
        binding=row(cursor,'SELECT * FROM qq_official_subagent_dispatches WHERE task_id=?',(task_id,))
        task=row(cursor,'SELECT * FROM subagent_tasks WHERE task_id=?',(task_id,))
        self.assert_current(cursor,binding=binding,task=task,job=job,now=now)
        actual=self.coordinator.store._task_row(task)
        return BackgroundToolSource(actual.endpoint,actual.requester_actor,'project' if self.domain_kind=='project_task' else 'subagent',f'task:{task_id}',1)
