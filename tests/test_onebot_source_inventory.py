"""Real domain rows and source journal; no transport, inferred bot or fake jobs."""
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from src.plugins.ai_chat.adapters.onebot_migration_runtime import SourceJournal, job_digest
from src.plugins.ai_chat.adapters.onebot_source_inventory import (
    OneBotSourceInventory, resolve_task, resolve_routine, resolve_job, resolve_delivery,
)
from src.plugins.ai_chat.agent import ContextPacket
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.projects import ProjectAccess, ProjectStore
from src.plugins.ai_chat.quota import UsageStore
from src.plugins.ai_chat.routines import RoutineStore
from src.plugins.ai_chat.subagents import SubAgentStore, TaskStep
from tests.qq_official_migration_journal_fixture import JournalFixture


class OneBotInventoryTests(JournalFixture):
    def setUp(self):
        super().setUp()
        self.tasks = SubAgentStore(self.database)
        self.usage = UsageStore(self.database)
        self.routines = RoutineStore(self.database, jobs=self.jobs, outbox=self.deliveries, usage=self.usage)
        for store in (self.tasks, self.usage, self.routines):
            self.addCleanup(store.close)
        self.sources = SourceJournal(self.routes, self.tx, clock=lambda: self.now)
        self.holder = SimpleNamespace(account=self.source.account, runtime_id=self.baseline.runtime_id)
        self.principal=self.ledger.ensure_principal_identity('onebot-v11','42','TEST owner')
        self.scope = replace(self.source_scope, actor_native_user_id='42', bot_native_user_id=self.source.account.app_id)

    def task(self, *, bot=None, deadline=True, status='queued'):
        bot = bot or self.source.account.app_id
        dispatch = {'bot_id': bot, 'event': {'self_id': int(bot), 'user_id': 42, 'message_type': 'group', 'group_id': 700}}
        if deadline:
            dispatch['deadline'] = self.now + 500
        task = self.tasks.prepare_task(packet=ContextPacket(self.scope.key,'TEST_CONVERSATION',42,None,'TEST objective'),
            decision=None, dispatch=dispatch, max_parallelism=1, max_steps=2, now=self.now)
        if status != 'queued':
            self.tasks.set_task_state(task.task_id, status, now=self.now)
        return task

    def job(self, task):
        return self.jobs.enqueue(kind='subagent.workflow',idempotency_key=f'subagent:{task.task_id}:revision:1',
            scope_key=self.scope.key,payload={'task_id':task.task_id,'revision':1},now=self.now)[0]

    def resolve(self, resolver, *args):
        with self.tx() as cursor:
            return resolver(cursor,*args)

    def inventory(self):
        with self.tx() as cursor:
            return OneBotSourceInventory()(cursor,self.holder,self.sources)

    def test_actual_task_job_adopt_once_and_fingerprint_does_not_follow_status(self):
        task=self.task(); job=self.job(task)
        before=self.resolve(resolve_task,task.task_id,1)
        self.assertEqual(before.bot_native_id,self.source.account.app_id)
        self.assertEqual(before.actor_native_id,'42')
        self.assertEqual((before.endpoint_kind,before.endpoint_native_id),('group','700'))
        with self.tx() as cursor:
            raw=cursor.execute('SELECT * FROM durable_jobs WHERE job_id=?',(job.job_id,)).fetchone()
            self.assertEqual(resolve_job(cursor,job.job_id).fingerprint,job_digest(raw))
        first=self.inventory(); second=self.inventory()
        self.assertEqual((first.adopted,second.existing),(2,2))
        self.assertEqual(1,self.query("SELECT COUNT(*) FROM migration_work_sources WHERE parent_source_id IS NOT NULL")[0][0])
        self.tasks.set_task_state(task.task_id,'running',plan={'steps':['updated']},now=self.now+1)
        after=self.resolve(resolve_task,task.task_id,1)
        self.assertEqual(before.fingerprint,after.fingerprint);self.assertEqual(after.state,'unknown')
        self.assertNotIn(self.source.account.app_id,repr(after))

    def test_missing_bot_or_event_never_infers_account_from_scope_or_history(self):
        task=self.task()
        for dispatch in ({'event':{'self_id':int(self.source.account.app_id)}},
                         {'bot_id':self.source.account.app_id,'event':{'user_id':42}},
                         {'bot_id':self.source.account.app_id,'event':{'self_id':True,'user_id':42,'message_type':'group','group_id':700}}):
            self.query('UPDATE subagent_controls SET dispatch_json=? WHERE task_id=?',(json.dumps(dispatch),task.task_id))
            self.assertIsNone(self.resolve(resolve_task,task.task_id,1))
        self.assertEqual(self.inventory().unattributed,1)
        self.assertEqual([],self.query('SELECT * FROM migration_work_sources'))

    def test_missing_deadline_is_not_renewed_and_other_account_not_adopted(self):
        self.task(deadline=False); self.task(bot='999999')
        summary=self.inventory()
        self.assertEqual(summary.missing_deadline,1);self.assertEqual(summary.other_account,1)
        self.assertEqual(summary.adopted,0)

    def test_attempted_or_unresolved_task_job_stays_unknown(self):
        task=self.task();job=self.job(task)
        self.tasks.create_run(task.task_id,TaskStep('work','coder','TEST','TEST'),allowed_tools=[],model_profile='TEST',now=self.now)
        self.query('UPDATE subagent_runs SET attempt=1 WHERE task_id=?',(task.task_id,))
        self.assertEqual(self.resolve(resolve_job,job.job_id).state,'unknown')
        summary=self.inventory();self.assertEqual(summary.unknown,2);self.assertEqual(summary.adopted,0)
        self.query('UPDATE subagent_runs SET attempt=0 WHERE task_id=?',(task.task_id,))
        self.jobs.claim_due('TEST_OWNER',now=self.now)
        self.assertEqual(self.resolve(resolve_job,job.job_id).state,'unknown')

    def test_actual_collaborator_revision_uses_authorized_actor_and_revocation_invalidates(self):
        task=self.task()
        principal=self.ledger.ensure_principal_identity('onebot-v11','43','TEST collaborator')
        grant=self.tasks.set_collaborator(task.task_id,scope_key=self.scope.key,requester_user_id=42,
            principal_id=principal,enabled=True,expected_version=1)
        control=self.tasks.control(task.task_id);dispatch=control['dispatch']
        dispatch['execution']={'revision':2,'actor_user_id':43,'actor_principal_id':principal,
            'grant_version':grant['version'],'event':{'self_id':int(self.source.account.app_id),
            'user_id':43,'message_type':'group','group_id':700}}
        # Use the actual grant row's version, distinct from control CAS version.
        dispatch['execution']['grant_version']=self.query('SELECT grant_version FROM subagent_collaborators WHERE task_id=?',(task.task_id,))[0][0]
        self.tasks.update_control(task.task_id,expected_version=control['version'],revision=2,dispatch=dispatch)
        source=self.resolve(resolve_task,task.task_id,2)
        self.assertIsNotNone(source);self.assertEqual(source.actor_native_id,'43')
        self.assertIsNone(self.resolve(resolve_task,task.task_id,1))
        self.tasks.set_collaborator(task.task_id,scope_key=self.scope.key,requester_user_id=42,
            principal_id=principal,enabled=False,expected_version=self.tasks.control(task.task_id)['version'])
        self.assertIsNone(self.resolve(resolve_task,task.task_id,2))

    def test_private_actor_and_physical_target_do_not_follow_canonical_alias(self):
        task=self.task();control=self.tasks.control(task.task_id);dispatch=control['dispatch']
        dispatch['event']={'self_id':int(self.source.account.app_id),'user_id':42,'message_type':'private'}
        self.tasks.update_control(task.task_id,expected_version=1,dispatch=dispatch)
        value=self.resolve(resolve_task,task.task_id,1)
        self.assertEqual(value.scope.key,self.scope.key)
        self.assertEqual((value.endpoint_kind,value.endpoint_native_id),('private','42'))

    def test_periodic_routine_has_no_invented_deadline_and_frozen_run_has_exact_job(self):
        routine=self.routines.set(self.scope,name='TEST',prompt='TEST reminder',cron='* * * * *',now=self.now)
        value=self.resolve(resolve_routine,routine.routine_id,1)
        self.assertIsNone(value.deadline)
        run=self.routines.materialize_due(now=routine.next_due_at)[0]
        job=self.resolve(resolve_job,run.job_id)
        self.assertIsNotNone(job);self.assertIsNone(job.deadline)
        self.query('UPDATE agent_routines SET next_due_at=next_due_at+100,updated_at=updated_at+1 WHERE routine_id=?',(routine.routine_id,))
        self.assertEqual(value.fingerprint,self.resolve(resolve_routine,routine.routine_id,1).fingerprint)
        self.assertEqual(self.inventory().missing_deadline,2)
        spec=dict(run.spec.__dict__);spec['bot_id']='999999'
        self.query('UPDATE routine_runs SET spec_json=? WHERE run_id=?',(json.dumps(spec),run.run_id))
        self.assertIsNone(self.resolve(resolve_job,run.job_id))

    def test_original_run_deadline_preserved_and_terminal_jobs_not_restarted(self):
        routine=self.routines.set(self.scope,name='TEST',prompt='TEST',cron='* * * * *',now=self.now)
        run=self.routines.materialize_due(now=routine.next_due_at)[0]
        self.query('UPDATE routine_runs SET deadline_at=? WHERE run_id=?',(self.now+75,run.run_id))
        self.assertEqual(self.resolve(resolve_job,run.job_id).deadline,self.now+75)
        self.query("UPDATE durable_jobs SET status='failed' WHERE job_id=?",(run.job_id,))
        self.assertEqual(self.resolve(resolve_job,run.job_id).state,'terminal')
        self.assertEqual(self.inventory().adopted,0)

    def delivery(self,task,*,key=None):
        return self.deliveries.enqueue(idempotency_key=key or f'subagent-final:{task.task_id}:1',
            source_scope_key=self.scope.key,target_scope=self.scope,body=MessageBody((TextNode(0,'TEST result'),)),
            source_canonical_message_id=task.trigger_message_id,now=self.now)[0]

    def test_delivery_requires_actual_domain_provenance_and_success_unknown_not_replayed(self):
        task=self.task(status='completed');delivery=self.delivery(task)
        self.assertIsNone(self.resolve(resolve_delivery,delivery.delivery_id))
        self.tasks.mark_final_queued(task.task_id,1)
        self.assertEqual(self.resolve(resolve_delivery,delivery.delivery_id).state,'safe')
        arbitrary=self.delivery(task,key='TEST_same_target_no_bot_proof')
        self.assertIsNone(self.resolve(resolve_delivery,arbitrary.delivery_id))
        self.query("UPDATE deliveries SET status='ambiguous' WHERE delivery_id=?",(delivery.delivery_id,))
        self.assertEqual(self.resolve(resolve_delivery,delivery.delivery_id).state,'unknown')
        self.assertEqual(self.inventory().adopted,0)
        self.query("UPDATE deliveries SET status='committed' WHERE delivery_id=?",(delivery.delivery_id,))
        self.assertEqual(self.resolve(resolve_delivery,delivery.delivery_id).state,'terminal')
        self.assertEqual(self.inventory().adopted,0)

    def test_project_task_requires_same_actual_project_bot(self):
        task=self.task()
        with TemporaryDirectory() as root:
            projects=ProjectStore(self.database,Path(root))
            self.addCleanup(projects.close)
            project=projects.create(ProjectAccess(self.scope,self.principal,member=True),'TEST project',now=self.now)
            self.query('UPDATE subagent_tasks SET project_id=? WHERE task_id=?',(project['project_id'],task.task_id))
            self.assertIsNotNone(self.resolve(resolve_task,task.task_id,1))
            self.query("UPDATE projects SET bot_native_user_id='999999' WHERE project_id=?",(project['project_id'],))
            self.assertIsNone(self.resolve(resolve_task,task.task_id,1))

    def test_new_inventory_after_route_exists_does_not_upgrade_old_epoch(self):
        self.task(); self.plan()
        summary=self.inventory()
        self.assertEqual(summary.adopted,0);self.assertEqual(summary.unavailable,1)
        self.assertEqual([],self.query('SELECT * FROM migration_work_sources'))

    def test_inventory_bounds_explicitly_report_truncation(self):
        self.task();self.task()
        with self.tx() as cursor:
            summary=OneBotSourceInventory(limit=1)(cursor,self.holder,self.sources)
        self.assertTrue(summary.truncated);self.assertEqual(summary.adopted,1)
        for limit in (True,0,10001):
            with self.assertRaises(ValueError):OneBotSourceInventory(limit=limit)
