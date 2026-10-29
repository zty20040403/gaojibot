"""Real Journal + Coordinator.resume + PG queue + FakePlatform M3 integration."""
import json
import unittest
from unittest.mock import AsyncMock,Mock,patch

import nonebot
nonebot.init()
from tests import test_qq_official_migration_journal_continuation as fixture
from src.plugins.ai_chat.adapters.qq_official.migration_subagents import SubAgentContinuations,ConversionDenied
from src.plugins.ai_chat.adapters.qq_official.migration_preview import SourceRef
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.agent.contracts import ContextPacket
from src.plugins.ai_chat.adapters.qq_official.subagent_runtime import OfficialSubAgentDispatcher,job_kind,assert_current
from src.plugins.ai_chat.model_catalog import ModelProfile,ModelCatalog
from src.plugins.ai_chat.subagents import SubAgentStore,SubAgentCoordinator,TaskStep
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker
from tests.qq_official_outbox_fixture import OutboxFixture

class SubAgentContinuationTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    owned=fixture.JournalContinuationTests.owned
    new_engine=fixture.JournalContinuationTests.new_engine
    ensure_current=fixture.JournalContinuationTests.ensure_current
    asyncTearDown=fixture.JournalContinuationTests.asyncTearDown
    rotate_mapping=fixture.JournalContinuationTests.rotate_mapping
    start=fixture.JournalContinuationTests.start
    freeze=fixture.JournalContinuationTests.freeze
    convert=fixture.JournalContinuationTests.convert
    complete=fixture.JournalContinuationTests.complete
    tick=fixture.JournalContinuationTests.tick

    async def asyncSetUp(self):
        await fixture.JournalContinuationTests.asyncSetUp(self)
        self.store=self.owned(SubAgentStore(self.database))
        self.profile=ModelProfile(name='test',provider='test',protocol='openai-chat',model='test',base_url='http://127.0.0.1',api_key_required=False)
        self.logger=Mock()
        self.coordinator=SubAgentCoordinator(self.store,ModelCatalog({'test':self.profile},default_profile='test'),logger=self.logger,max_parallelism=1)
        self.engine=SubAgentContinuations(self.database,token=self.token,jobs=self.jobs,coordinator=self.coordinator,
            journal=self.migration,preview=self.preview,profile='test',admission=lambda c,e,a:e==self.endpoint and a==self.actor,clock=lambda:self.now)
        self.dispatcher=OfficialSubAgentDispatcher(self.token,self.coordinator,self.jobs,self.outbox,self.tx,self.calls,
            self.engine.admission,self.profile,clock=lambda:self.now,migration_authority=self.engine)
        self.worker=DurableJobWorker(self.jobs,logger=self.logger,database_calls=self.calls,worker_id='TEST_MIGRATION_TASK')
        self.worker.register(job_kind(self.account),self.dispatcher.execute)
        self.jobs.lease_seconds=3600

    def source_task(self,*,completed=False,pending_attempt=0):
        packet=ContextPacket(self.source_scope.key,'TEST old conversation',201,None,'TEST approved two-step text')
        task=self.store.prepare_task(packet=packet,decision=None,
            dispatch={'bot_id':'301','deadline':self.execution_deadline,'profile':'test'},
            max_parallelism=1,max_steps=5,now=self.now)
        first=self.store.create_run(task.task_id,TaskStep('first','analyst','TEST already done','TEST first summary'),allowed_tools=(),model_profile='test',now=self.now)
        self.store.finish_run(first.run_id,'succeeded',result={'status':'success','summary':'TEST confirmed first','artifacts':[]},now=self.now)
        second=self.store.create_run(task.task_id,TaskStep('second','analyst','TEST pending only','TEST second summary',('first',)),allowed_tools=(),model_profile='test',now=self.now)
        if pending_attempt:
            self.query('UPDATE subagent_runs SET attempt=? WHERE run_id=?',(pending_attempt,second.run_id))
        if completed:
            self.store.finish_run(second.run_id,'succeeded',result={'status':'success','summary':'TEST confirmed second','artifacts':[]},now=self.now)
            self.store.set_task_state(task.task_id,'completed',result={'answer':'TEST saved completed answer'},now=self.now)
        self.old_task,self.old_first,self.old_second=task,first,second
        return SourceRef('subagent',str(task.task_id))

    def latest(self,item_id):
        return self.query('SELECT * FROM migration_subagent_continuations WHERE item_id=?',(item_id,))[0]

    async def test_real_coordinator_resume_only_unstarted_step_then_m3(self):
        plan=self.start(self.source_task())
        item,b=self.convert(plan)
        self.assertEqual(await self.tick(),0)
        self.assertEqual(self.jobs.get(b['job_id']).attempts,0)
        self.assertEqual(self.store.get(b['target_task_id']).trigger_message_id,None)
        self.assertEqual(self.store.get(b['target_task_id']).requester_user_id,None)
        self.complete(plan,item)
        model=AsyncMock(return_value='{"status":"success","summary":"TEST second result","artifacts":[]}')
        with patch('src.plugins.ai_chat.subagents.ask_deepseek_with_tools',model), \
             patch('src.plugins.ai_chat.subagents.ask_deepseek',new=AsyncMock(return_value='TEST final result')):
            self.assertEqual(await self.tick(),1)
        self.assertEqual(model.await_count,1)
        self.assertEqual(self.jobs.get(b['job_id']).status,'succeeded',self.logger.mock_calls)
        self.assertEqual(self.store.get(b['target_task_id']).status,'completed')
        self.assertEqual(self.store.runs(self.old_task.task_id)[0].status,'succeeded')
        self.assertEqual(self.store.runs(self.old_task.task_id)[1].status,'skipped')
        b=self.latest(item);self.assertIsNotNone(b['plan_id'])
        p=self.query('SELECT * FROM delivery_plans WHERE plan_id=?',(b['plan_id'],))[0]
        self.assertIsNone(p['source_canonical_message_id']);self.assertEqual(json.loads(p['manifest_json'])['source_native_message_id'],'')
        self.assertEqual(p['expires_at'],self.result_deadline)
        with self.tx() as cursor:self.assertTrue(self.engine.current_plan_source(cursor,b['plan_id'],self.now))
        delivery=self.query('SELECT delivery_id FROM deliveries WHERE plan_id=?',(b['plan_id'],))[0][0]
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertEqual(len(self.fake.received),1)
        await self.sender.send_delivery(delivery)
        self.assertEqual(len(self.fake.received),1)

    async def test_completed_source_past_execution_deadline_sends_frozen_without_model(self):
        plan=self.start(self.source_task(completed=True));item,b=self.convert(plan);self.complete(plan,item)
        self.now=self.execution_deadline+1
        with patch.object(self.coordinator,'resume',new_callable=AsyncMock) as resume:
            self.assertEqual(await self.tick(),1)
        resume.assert_not_awaited()
        self.assertEqual(self.jobs.get(b['job_id']).status,'succeeded',self.logger.mock_calls)
        b=self.latest(item);self.assertIsNotNone(b['plan_id'])
        self.assertEqual(self.store.get(self.old_task.task_id).result['answer'],'TEST saved completed answer')
        self.assertEqual(self.query('SELECT result_expires_at FROM qq_official_subagent_dispatches WHERE task_id=?',(b['target_task_id'],))[0][0],self.result_deadline)
        delivery=self.query('SELECT delivery_id FROM deliveries WHERE plan_id=?',(b['plan_id'],))[0][0]
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')

    async def test_pending_prior_model_attempt_no_target_and_no_job(self):
        source=self.source_task(pending_attempt=1)
        with self.tx() as cursor:preview=self.preview.preview(cursor,self.selection,source,now=self.now)
        self.assertEqual(preview.disposition,'unknown');self.assertIsNone(preview.export)
        plan=self.start(source)
        with self.assertRaises(MigrationDenied):self.convert(plan)
        self.assertEqual(self.query('SELECT count(*) FROM migration_subagent_continuations')[0][0],0)

    async def test_prepared_idempotent_and_profile_mutation_rejects(self):
        plan=self.start(self.source_task());item,b=self.convert(plan)
        with self.tx() as cursor:
            proof=self.migration.issue_item(cursor,item_id=item,now=self.now)
            self.assertEqual(self.engine.prepare(cursor,proof,now=self.now)['target_task_id'],b['target_task_id'])
        self.query("UPDATE subagent_controls SET dispatch_json=jsonb_set(dispatch_json::jsonb,'{tool_profile}','\"official-files-v1\"')::text WHERE task_id=?",(b['target_task_id'],))
        with self.tx() as cursor:
            with self.assertRaises((ConversionDenied,MigrationDenied)):
                proof=self.migration.issue_item(cursor,item_id=item,now=self.now)
                self.engine.prepare(cursor,proof,now=self.now)

    async def test_proof_cannot_be_reconstructed_or_source_changed(self):
        from dataclasses import replace
        plan=self.start(self.source_task())
        item=self.query('SELECT item_id FROM migration_items WHERE migration_id=?',(plan.migration_id,))[0][0]
        with self.tx() as cursor:
            proof=self.migration.issue_item(cursor,item_id=item,now=self.now)
            with self.assertRaises(MigrationDenied):self.engine.prepare(cursor,replace(proof),now=self.now)
        self.query('UPDATE subagent_tasks SET objective=? WHERE task_id=?',('TEST changed',self.old_task.task_id))
        with self.tx() as cursor:
            with self.assertRaises(MigrationDenied):self.migration.issue_item(cursor,item_id=item,now=self.now)

    async def test_background_metadata_has_no_message_or_credential_and_requires_job(self):
        plan=self.start(self.source_task());item,b=self.convert(plan);self.complete(plan,item)
        job=self.jobs.claim_due('TEST_SOURCE_PORT',kinds=(job_kind(self.account),),now=self.now)[0]
        with self.tx() as cursor:
            source=self.engine.tool_source(cursor,task_id=b['target_task_id'],job=job,now=self.now)
            self.assertEqual(source.endpoint,self.endpoint);self.assertEqual(source.actor,self.actor)
            for field in ('native_message_id','reply_credential','occurred_at'):
                self.assertFalse(hasattr(source,field))
        self.query('UPDATE durable_jobs SET lease_generation=lease_generation+1 WHERE job_id=?',(job.job_id,))
        from src.plugins.ai_chat.agent.control import LeaseLost
        with self.tx() as cursor:
            with self.assertRaises(LeaseLost):self.engine.tool_source(cursor,task_id=b['target_task_id'],job=job,now=self.now)

    async def test_missing_migration_provider_cannot_use_existing_dispatcher(self):
        plan=self.start(self.source_task(completed=True));item,b=self.convert(plan);self.complete(plan,item)
        job=self.jobs.claim_due('TEST_MISSING_PROVIDER',kinds=(job_kind(self.account),),now=self.now)[0]
        from src.plugins.ai_chat.agent.control import LeaseLost
        with self.tx() as cursor:
            with self.assertRaises(LeaseLost):assert_current(cursor,token=self.token,task_id=b['target_task_id'],job=job,now=self.now,admission=self.engine.admission)

    async def test_mapping_revoke_blocks_pending_plan(self):
        plan=self.start(self.source_task(completed=True));item,b=self.convert(plan);self.complete(plan,item)
        await self.tick();b=self.latest(item)
        with self.tx() as cursor:self.assertTrue(self.engine.current_plan_source(cursor,b['plan_id'],self.now))
        self.rotate_mapping()
        with self.tx() as cursor:
            with self.assertRaises(MigrationDenied):self.engine.current_plan_source(cursor,b['plan_id'],self.now)

    async def test_actual_service_background_neutral_registry_and_read_scope(self):
        from dataclasses import replace
        from types import SimpleNamespace
        from src.plugins.ai_chat.adapters.qq_official.subagent_composition import OfficialSubAgentService
        from src.plugins.ai_chat.config import settings
        from src.plugins.ai_chat.long_term_memory import LongTermMemoryStore
        from src.plugins.ai_chat.message_ir import MessageBody,TextNode
        self.memory=LongTermMemoryStore(self.database)
        self.memory.add(scope_type='user',scope_key=self.actor.key,content='TEST own memory',creator_actor_key=self.actor.key)
        self.memory.add(scope_type='user',scope_key='TEST_OTHER_ACTOR',content='TEST other memory')
        runtime=SimpleNamespace(store=SimpleNamespace(clock=lambda:self.now,admitted=self.engine.admission),
            deliveries=self.deliveries,jobs=self.jobs,outbox=self.outbox,_calls=self.calls,sender=None)
        app=SimpleNamespace(subagent_coordinator=self.coordinator,subagent_store=self.store,
            model_catalog=self.coordinator.model_catalog,logger=self.logger,
            settings=replace(settings,search_enabled=True),db_call=self.calls,long_term_memory=self.memory,
            message_ledger=self.ledger,context_store=None,pin_store=None,semantic_recall=None,
            skill_registry=SimpleNamespace(get=lambda name:None,list=lambda:[]),self_source=Mock())
        service=OfficialSubAgentService(runtime,self.token,app,migration_authority=self.engine)
        self.engine.coordinator=service.coordinator
        self.worker=DurableJobWorker(self.jobs,logger=self.logger,database_calls=self.calls,worker_id='TEST_COMPOSED_MIGRATION')
        self.worker.register(job_kind(self.account),service.execute)
        plan=self.start(self.source_task());item,b=self.convert(plan);self.complete(plan,item)
        # A real target message is history, never used as the migrated trigger.
        self.ledger.record_message(self.endpoint.scope,native_message_id='TEST_HISTORY',
            sender_native_user_id=self.actor.native_user_id,sender_display='TEST member',
            direction='inbound',body=MessageBody((TextNode(0,'TEST scope history'),)),occurred_at=self.now,
            actor=self.actor,source_endpoint=self.endpoint)
        async def model(*args,**kwargs):
            tools=args[2] if len(args)>2 else kwargs['tools']
            executor=args[3] if len(args)>3 else kwargs['execute_tool']
            names={t['function']['name'] for t in tools}
            self.assertIn('memory_list',names);self.assertIn('context_search',names)
            self.assertFalse(names & {'memory_add','sandbox_create','host_inspect','fleet_submit'})
            memory=json.loads(await executor('memory_list',{'scope':'user'}))
            self.assertIn('TEST own memory',json.dumps(memory))
            self.assertNotIn('TEST other memory',json.dumps(memory))
            history=json.loads(await executor('context_search',{'query':'TEST scope history'}))
            self.assertTrue(history['ok'],history)
            self.assertIn('TEST scope history',json.dumps(history))
            rejected=json.loads(await executor('context_search',{'query':'TEST','scope_key':self.source_scope.key}))
            self.assertFalse(rejected['ok'])
            return '{"status":"success","summary":"TEST composed result","artifacts":[]}'
        with patch('src.plugins.ai_chat.subagents.ask_deepseek_with_tools',new=model), \
             patch('src.plugins.ai_chat.subagents.ask_deepseek',new=AsyncMock(return_value='TEST composed final')):
            self.assertEqual(await self.tick(),1)
        self.assertEqual(self.jobs.get(b['job_id']).status,'succeeded',self.logger.mock_calls)
        self.assertEqual(self.store.get(b['target_task_id']).status,'completed')
        self.assertIsNotNone(self.latest(item)['plan_id'])

    async def test_lost_model_result_is_quarantined_on_new_job_lease(self):
        plan=self.start(self.source_task());item,b=self.convert(plan);self.complete(plan,item)
        first=self.jobs.claim_due('TEST_CRASHED_WORKER',kinds=(job_kind(self.account),),now=self.now)[0]
        with self.tx() as cursor:self.assertTrue(self.engine.begin_job(cursor,job=first,now=self.now))
        target_runs=self.store.runs(b['target_task_id'])
        self.query("UPDATE subagent_runs SET status='running',attempt=1 WHERE run_id=?",(target_runs[1].run_id,))
        self.query("UPDATE subagent_tasks SET status='running' WHERE task_id=?",(b['target_task_id'],))
        self.query('UPDATE durable_jobs SET lease_until=? WHERE job_id=?',(self.now-1,first.job_id))
        second=self.jobs.claim_due('TEST_RESTARTED_WORKER',kinds=(job_kind(self.account),),now=self.now)[0]
        self.assertGreater(second.lease_generation,first.lease_generation)
        with patch.object(self.coordinator,'resume',new_callable=AsyncMock) as resume:
            self.worker.worker_id=second.lease_owner
            await self.worker._execute(second)
        resume.assert_not_awaited()
        saved=self.query('SELECT suppressed_reason,plan_id FROM qq_official_subagent_dispatches WHERE task_id=?',(b['target_task_id'],))[0]
        self.assertEqual(saved['suppressed_reason'],'migration_execution_unknown');self.assertIsNone(saved['plan_id'])
        self.assertEqual(self.jobs.get(b['job_id']).status,'succeeded')
        self.assertEqual(self.store.runs(b['target_task_id'])[1].attempt,1)

    async def test_enqueue_failure_retries_completed_bytes_without_new_model(self):
        plan=self.start(self.source_task());item,b=self.convert(plan);self.complete(plan,item)
        model=AsyncMock(return_value='{"status":"success","summary":"TEST result","artifacts":[]}')
        with patch('src.plugins.ai_chat.subagents.ask_deepseek_with_tools',model), \
             patch('src.plugins.ai_chat.subagents.ask_deepseek',new=AsyncMock(return_value='TEST retained final')), \
             patch.object(self.outbox,'enqueue',side_effect=RuntimeError('TEST local enqueue failure')):
            await self.tick()
        self.assertEqual(model.await_count,1)
        self.assertEqual(self.store.get(b['target_task_id']).status,'completed')
        self.now+=4
        with patch.object(self.coordinator,'resume',new_callable=AsyncMock) as resume:
            await self.tick()
        resume.assert_not_awaited()
        self.assertIsNotNone(self.latest(item)['plan_id'])
        self.assertEqual(self.query('SELECT result_expires_at FROM qq_official_subagent_dispatches WHERE task_id=?',(b['target_task_id'],))[0][0],self.result_deadline)

    async def test_source_xor_cannot_attach_borrowed_message_to_migration(self):
        from src.bot_storage.database import DatabaseError
        plan=self.start(self.source_task());item,b=self.convert(plan)
        for sql,args in [
            ('UPDATE subagent_tasks SET trigger_message_id=1 WHERE task_id=?',(b['target_task_id'],)),
            ("UPDATE qq_official_subagent_dispatches SET source_native_message_id='TEST_BORROWED' WHERE task_id=?",(b['target_task_id'],)),
            ('UPDATE qq_official_subagent_dispatches SET migration_result_deadline_at=NULL WHERE task_id=?',(b['target_task_id'],))]:
            with self.assertRaises(DatabaseError):
                with self.tx() as cursor:cursor.execute(sql,args)

    async def test_original_contract_and_optional_step_not_silently_dropped(self):
        source=self.source_task()
        contract={'version':1,'objective':self.old_task.objective,'deliverables':['TEST exact output'],
            'constraints':['TEST do not change original limit'],'acceptance':['TEST original criterion'],
            'delivery_required':False,'outcome_checks':[{'criterion_index':0,'kind':'evidence'}]}
        original_plan={'mode':'workflow','contract':contract,'steps':[{'id':'second','optional':True}]}
        self.query('UPDATE subagent_tasks SET plan_json=? WHERE task_id=?',(json.dumps(original_plan),self.old_task.task_id))
        plan=self.start(source);item,b=self.convert(plan)
        target=self.store.get(b['target_task_id'])
        self.assertEqual(target.plan['contract'],contract)
        self.assertTrue(target.plan['steps'][1]['optional'])
        checkpoint=self.store.checkpoints(target.task_id)[0]['state']['context_packet']
        self.assertEqual(checkpoint['constraints'],contract['constraints'])
        self.assertEqual(checkpoint['trigger_message_id'],None)

    async def test_file_or_host_contract_cannot_silently_become_text(self):
        source=self.source_task()
        contract={'version':3,'objective':self.old_task.objective,'deliverables':['TEST PDF'],
            'constraints':[],'acceptance':['TEST file exists'],'delivery_required':True,
            'outcome_checks':[{'criterion_index':0,'kind':'evidence'}]}
        self.query('UPDATE subagent_tasks SET plan_json=? WHERE task_id=?',(json.dumps({'contract':contract}),self.old_task.task_id))
        plan=self.start(source)
        with self.assertRaises(ConversionDenied):self.convert(plan)
        self.assertFalse(self.store.get(self.old_task.task_id).cancel_requested)
        self.assertEqual(self.query('SELECT count(*) FROM migration_subagent_continuations')[0][0],0)

    async def test_old_immutable_artifact_reference_not_silently_dropped_or_replayed(self):
        source=self.source_task()
        result={'status':'success','summary':'TEST first with artifact','artifacts':[{
            'name':'TEST evidence.pdf','size':12,'snapshot':'e'*64,
            'handle':'TEST_HOST:/private/host/path','url':'https://invalid.example/credential'}]}
        self.query('UPDATE subagent_runs SET result_json=? WHERE run_id=?',(json.dumps(result),self.old_first.run_id))
        with self.tx() as cursor:export=self.preview.preview(cursor,self.selection,source,now=self.now)
        ref=export.export['steps'][0]['immutable_artifact_refs'][0]
        self.assertEqual(ref['sha256'],'e'*64)
        self.assertNotIn('TEST_HOST',json.dumps(export.export));self.assertNotIn('invalid.example',json.dumps(export.export))
        plan=self.start(source)
        with self.assertRaises(ConversionDenied):self.convert(plan)
        self.assertFalse(self.store.get(self.old_task.task_id).cancel_requested)
        self.assertEqual(self.query('SELECT count(*) FROM migration_subagent_continuations')[0][0],0)

    async def test_scheduled_child_cannot_escape_original_parent_budget(self):
        source=self.source_task()
        routine=self.routines.set(self.source_scope,name='TEST parent',prompt='TEST schedule',cron='* * * * *',now=self.now)
        run=self.routines.materialize_due(now=routine.next_due_at)[0]
        self.query('INSERT INTO routine_children(run_id,fingerprint,task_id,intent_json,created_at) VALUES (?,?,?,?,?)',
            (run.run_id,'TEST_CHILD',self.old_task.task_id,'{}',self.now))
        plan=self.start(source)
        with self.assertRaises(ConversionDenied):self.convert(plan)
        self.assertFalse(self.store.get(self.old_task.task_id).cancel_requested)
        self.assertEqual(self.query('SELECT count(*) FROM migration_subagent_continuations')[0][0],0)

    async def test_cancelled_target_cannot_authorize_new_physical_attempt(self):
        plan=self.start(self.source_task(completed=True));item,b=self.convert(plan);self.complete(plan,item)
        await self.tick();b=self.latest(item)
        self.query("UPDATE subagent_tasks SET status='cancelled' WHERE task_id=?",(b['target_task_id'],))
        with self.tx() as cursor:
            with self.assertRaises((MigrationDenied,ConversionDenied)):
                self.engine.current_plan_source(cursor,b['plan_id'],self.now)
        self.assertEqual(len(self.fake.received),0)
