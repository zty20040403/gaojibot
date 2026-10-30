"""Actual migrated Coordinator, owned guest, immutable CAS and M3 handoff."""
from dataclasses import replace
import json
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tests import test_qq_official_migration_subagents as base
from tests import test_qq_official_file_subagent_composition as files_fixture
from tests.qq_official_outbox_fixture import OutboxFixture
from tests.qq_official_file_http_fixture import FileMediaFake
from src.plugins.ai_chat.adapters.qq_official.file_artifacts import file_job_kind
from src.plugins.ai_chat.adapters.qq_official.file_authority import ArtifactSourceAuthorities, assert_file_current
from src.plugins.ai_chat.adapters.qq_official.file_payloads import TrustedFilePayloads
from src.plugins.ai_chat.adapters.qq_official.file_services import FileServiceConfig, FileServices
from src.plugins.ai_chat.adapters.qq_official.migration_artifacts import MigrationArtifactAuthority
from src.plugins.ai_chat.adapters.qq_official.migration_artifact_journal import MigrationArtifactProof
from src.plugins.ai_chat.adapters.qq_official.migration_host_ports import MigrationPlanGuard
from src.plugins.ai_chat.adapters.qq_official.migration_preview import SourceRef
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.adapters.qq_official.native_media import fingerprint, parse_upload_receipt
from src.plugins.ai_chat.adapters.qq_official.native_vault import NativeUploadBinding
from src.plugins.ai_chat.adapters.qq_official.subagent_composition import OfficialSubAgentService
from src.plugins.ai_chat.adapters.qq_official.subagent_runtime import FILE_TOOLS, job_kind
from src.plugins.ai_chat.agent.contracts import ContextPacket
from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.agent.execution import TaskContract
from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.subagents import TaskStep
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker
from src.bot_storage import DatabaseError


class MigrationArtifactTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    owned=base.SubAgentContinuationTests.owned
    new_engine=base.SubAgentContinuationTests.new_engine
    ensure_current=base.SubAgentContinuationTests.ensure_current
    rotate_mapping=base.SubAgentContinuationTests.rotate_mapping
    start=base.SubAgentContinuationTests.start
    freeze=base.SubAgentContinuationTests.freeze
    convert=base.SubAgentContinuationTests.convert
    complete=base.SubAgentContinuationTests.complete
    tick=base.SubAgentContinuationTests.tick
    latest=base.SubAgentContinuationTests.latest
    invoke=files_fixture.FileSubAgentCompositionTests.invoke
    file_model=files_fixture.FileSubAgentCompositionTests.file_model

    async def asyncSetUp(self):
        def platform(*,clock):
            fake=FileMediaFake()
            fake.clock=clock
            return fake
        with patch('tests.test_qq_official_migration_continuation.FakePlatform', platform):
            await base.SubAgentContinuationTests.asyncSetUp(self)
        temp=TemporaryDirectory(prefix='qq-migrated-files-');self.addCleanup(temp.cleanup)
        self.private=Path(temp.name)
        self.manager=files_fixture.ReviewGuest()
        self.sequence=[];self.model_calls=0;self.kind='pdf';self.file_allowed=True
        self.engine.file_admission=lambda c,**kw:self.file_allowed and kw['endpoint']==self.endpoint and kw['actor']==self.actor
        self.authority=MigrationArtifactAuthority(continuations=self.engine)
        self.authorities=ArtifactSourceAuthorities(self.authority)
        self.ports.bound_artifact=self.authority.validate_bound_artifact
        self.runtime=SimpleNamespace(_calls=self.calls,deliveries=self.deliveries,jobs=self.jobs,outbox=self.outbox,
            sender=None,_stopping=False,_token=self.token,store=SimpleNamespace(clock=lambda:self.now,admitted=self.engine.admission),file_service=None)
        config=FileServiceConfig(self.private,self.manager,generation_enabled=True,route_authority=lambda *_:True,
            min_free_bytes=0,link_origin='https://files.example',link_reachable=lambda:True,source_authority=self.authorities)
        self.files=FileServices(self.runtime,self.token,None,config)
        self.files.generation.source_factory=self.authority.issue
        self.runtime.file_service=self.files
        self.app=SimpleNamespace(subagent_coordinator=self.coordinator,subagent_store=self.store,
            model_catalog=self.coordinator.model_catalog,logger=self.logger,
            settings=replace(settings,search_enabled=False,subagent_max_parallelism=1),db_call=self.calls,
            long_term_memory=None,message_ledger=self.ledger,context_store=None,pin_store=None,semantic_recall=None,
            skill_registry=SimpleNamespace(get=lambda _:None,list=lambda:[]),self_source=Mock())
        self.service=OfficialSubAgentService(self.runtime,self.token,self.app,migration_authority=self.engine)
        self.engine.coordinator=self.service.coordinator
        self.worker=DurableJobWorker(self.jobs,logger=self.logger,database_calls=self.calls,worker_id='TEST_MIGRATION_FILES')
        self.worker.register(job_kind(self.account),self.service.execute)

    async def asyncTearDown(self):
        await self.files.close()
        await base.SubAgentContinuationTests.asyncTearDown(self)

    def source_task(self):
        objective='生成 TEST '+self.kind
        contract=TaskContract(objective,('result.'+self.kind,),(),('核对实际内容',),True,
            ({'criterion_index':0,'kind':'evidence'},),version=3).as_payload()
        task=self.store.prepare_task(packet=ContextPacket(self.source_scope.key,'TEST_FILE_MIGRATION',201,None,objective),
            decision=None,dispatch={'bot_id':'301','deadline':self.execution_deadline,'profile':'test'},
            max_parallelism=1,max_steps=5,now=self.now)
        step={'id':'produce','agent':'document' if self.kind=='pdf' else 'coder','objective':objective,
            'deliverable':'result.'+self.kind,'depends_on':[],'required_tools':sorted(FILE_TOOLS),'optional':False}
        self.store.create_run(task.task_id,TaskStep('produce',step['agent'],objective,step['deliverable']),
            allowed_tools=sorted(FILE_TOOLS),model_profile='test',now=self.now)
        self.query('UPDATE subagent_tasks SET plan_json=? WHERE task_id=?',
            (json.dumps({'mode':'delegate','steps':[step],'contract':contract}),task.task_id))
        self.old_task=task
        return SourceRef('subagent',str(task.task_id))

    async def produce(self):
        plan=self.start(self.source_task());self.item,self.binding=self.convert(plan);self.complete(plan,self.item)
        with patch('src.plugins.ai_chat.subagents.ask_deepseek_with_tools',new=self.file_model), \
             patch('src.plugins.ai_chat.subagents.ask_deepseek',new=AsyncMock(return_value='TEST generated and verified')):
            self.assertEqual(await self.tick(),1)
        job=self.jobs.get(self.binding['job_id'])
        self.assertEqual(job.status,'succeeded',self.logger.mock_calls)
        task=self.store.get(self.binding['target_task_id'])
        self.assertEqual(task.status,'completed',task.result)
        records=self.query('SELECT * FROM qq_official_file_artifacts')
        self.assertEqual(len(records),1,task.result)
        self.artifact=dict(records[0])
        return self.artifact

    def native_handoff(self):
        def no_native_fallback(*args,**kwargs):
            raise AssertionError('Migration artifact cannot use a native trigger')
        self.outbox.source_guard=MigrationPlanGuard(native=SimpleNamespace(plan=no_native_fallback),
            journal=self.migration,continuations=(self.engine,))
        self.file_job=self.jobs.claim_due('TEST_MIGRATED_FILE_JOB',kinds=(file_job_kind(self.account),),now=self.now)[0]
        self.port=TrustedFilePayloads(admission=self.engine.admission,route_policy=lambda *_:True,
            native_vault=self.files.native_vault,source_authority=self.authorities)
        self.outbox.protected_payloads=self.port
        data=self.files.artifacts.blobs.read(self.artifact['sha256'],max_bytes=self.artifact['byte_size'])
        fp=fingerprint([data],filename=self.artifact['filename'],file_type=4)
        binding=NativeUploadBinding(self.artifact['artifact_id'],secrets.token_hex(16),self.endpoint,fp,self.artifact['mime'])
        media=parse_upload_receipt(200,{'file_info':'TEST_FILE_INFO','file_uuid':'TEST_FILE_UUID','ttl':120},
            endpoint=self.endpoint,artifact=fp,now=self.now)
        ref=self.files.native_vault.save(binding,media)
        self.query('''INSERT INTO qq_official_file_stage_attempts(stage_attempt_id,artifact_id,operation,
            request_commitment,job_id,job_lease_generation,account_epoch,state,created_at,expires_at,settled_at,receipt_commitment)
            VALUES (?,?,'upload_small',?,?,?,?,'confirmed',?,?,?,?)''',
            (binding.stage_attempt_id,self.artifact['artifact_id'],'f'*64,self.file_job.job_id,self.file_job.lease_generation,
             self.token.epoch,self.now,self.now+60,self.now,ref.binding_digest))
        with self.tx() as cursor:
            grant=self.port.issue(cursor,token=self.token,job=self.file_job,artifact_id=self.artifact['artifact_id'],now=self.now)
            content=self.port.register_native(cursor,grant,binding,ref,self.now)
            self.plan=self.port.handoff(cursor,grant,content,self.outbox,self.now)
        return self.plan

    async def test_actual_coordinator_document_and_file_job_keep_null_native_union(self):
        artifact=await self.produce()
        self.assertEqual(self.model_calls,2)
        self.assertIsNone(artifact['source_execution_id']);self.assertIsNone(artifact['source_canonical_message_id'])
        self.assertIsNone(artifact['source_native_message_id']);self.assertEqual(artifact['source_migration_item_id'],self.item)
        self.assertEqual(artifact['pending_deadline_at'],self.result_deadline)
        self.assertTrue(self.files.artifacts.blobs.read(artifact['sha256'],max_bytes=artifact['byte_size']).startswith(b'%PDF-'))
        capacity=self.query("SELECT * FROM qq_official_generation_capacity")
        self.assertGreaterEqual(len(capacity),2)  # Producer plus independent verifier use the same physical budget.
        self.assertTrue(all(row['source_execution_id'] is None and row['source_migration_item_id']==self.item
            and row['source_task_id']==self.binding['target_task_id'] and row['source_job_id']==self.binding['job_id']
            for row in capacity), [(row['source_execution_id'],row['source_migration_item_id'],
                row['source_task_id'],row['source_job_id'],row['state']) for row in capacity])
        # Completed production no longer authorizes guest actions; remembered
        # resources remain charged until the existing bounded host cleanup.
        self.assertTrue(all(row['expires_at']==self.execution_deadline for row in capacity))
        plan=self.native_handoff()
        row=self.query('SELECT * FROM migration_artifact_plans')[0]
        self.assertNotEqual(row['source_job_id'],row['file_job_id'])
        with self.tx() as cursor:self.migration.current_artifact_plan_source(cursor,plan.plan_id,now=self.now)
        result=await self.sender.send_delivery(plan.delivery_ids[0])
        self.assertEqual(result.status,'committed')
        self.assertEqual(len(self.fake.received),1)
        self.assertEqual(self.fake.received[0]['payload']['msg_type'],7)
        self.assertIsNone(self.store.get(self.binding['target_task_id']).trigger_message_id)
        self.assertEqual(len(self.query('SELECT * FROM qq_official_file_message_plans')),1)

    async def test_coder_zip_real_generation_uses_current_profile(self):
        self.kind='zip';artifact=await self.produce()
        self.assertEqual(artifact['mime'],'application/zip');self.assertEqual(artifact['source_extension_kind'],'migration')

    async def test_missing_current_file_authorization_cannot_convert_or_replay(self):
        plan=self.start(self.source_task());self.file_allowed=False
        with self.assertRaises(ValueError):self.convert(plan)
        self.assertEqual([],self.query('SELECT * FROM migration_subagent_continuations'))
        self.file_allowed=True;self.item,self.binding=self.convert(plan);self.complete(plan,self.item)
        self.file_allowed=False
        job=self.jobs.claim_due('TEST_DENIED_FILE',kinds=(job_kind(self.account),),now=self.now)[0]
        with self.tx() as cursor, self.assertRaises((ValueError,LeaseLost)):
            self.authority.issue(cursor,task=self.store.get(self.binding['target_task_id']),job=job,now=self.now)
        self.assertEqual(self.manager.calls,[])

    async def test_mapping_revoke_and_task_cancel_block_stored_artifact(self):
        await self.produce();plan=self.native_handoff()
        self.rotate_mapping()
        with self.tx() as cursor,self.assertRaises((ValueError,LeaseLost)):
            assert_file_current(cursor,token=self.token,artifact_id=self.artifact['artifact_id'],plan_id=plan.plan_id,
                admission=self.engine.admission,source_authority=self.authorities,now=self.now)
        self.assertEqual(self.fake.received,[])

    async def test_late_positive_receipt_survives_mapping_revocation_without_resend(self):
        await self.produce();plan=self.native_handoff()
        original=self.outbox.finalize
        def after_revoke(*args,**kwargs):
            self.rotate_mapping()
            return original(*args,**kwargs)
        with patch.object(self.outbox,'finalize',side_effect=after_revoke):
            result=await self.sender.send_delivery(plan.delivery_ids[0])
        self.assertEqual(result.status,'committed')
        self.assertEqual(len(self.fake.received),1)
        self.assertEqual(self.query('SELECT state FROM qq_official_file_deliveries')[0][0],'message_accepted')
        with self.tx() as cursor,self.assertRaises((ValueError,LeaseLost)):
            self.migration.current_artifact_plan_source(cursor,plan.plan_id,now=self.now)
        await self.sender.send_delivery(plan.delivery_ids[0])
        self.assertEqual(len(self.fake.received),1)

    async def test_completed_producer_cancel_and_missing_provider_deny_file_job(self):
        await self.produce()
        job=self.jobs.claim_due('TEST_DENIED_FILE',kinds=(file_job_kind(self.account),),now=self.now)[0]
        with self.tx() as cursor,self.assertRaises(LeaseLost):
            assert_file_current(cursor,token=self.token,artifact_id=self.artifact['artifact_id'],job=job,
                admission=self.engine.admission,now=self.now)
        self.query('UPDATE subagent_tasks SET cancel_requested=TRUE WHERE task_id=?',(self.binding['target_task_id'],))
        with self.tx() as cursor,self.assertRaises((ValueError,LeaseLost)):
            assert_file_current(cursor,token=self.token,artifact_id=self.artifact['artifact_id'],job=job,
                admission=self.engine.admission,source_authority=self.authorities,now=self.now)
        self.assertEqual(self.fake.received,[])

    async def test_source_union_and_provenance_are_immutable(self):
        await self.produce();self.native_handoff()
        for sql,args in (
            ('UPDATE qq_official_file_artifacts SET source_migration_item_id=NULL WHERE artifact_id=?',(self.artifact['artifact_id'],)),
            ('DELETE FROM qq_official_migration_artifacts WHERE artifact_id=?',(self.artifact['artifact_id'],)),
            ('UPDATE migration_artifact_plans SET logical_route_epoch=logical_route_epoch+1 WHERE plan_id=?',(self.plan.plan_id,)),
            ('DELETE FROM migration_artifact_plans WHERE plan_id=?',(self.plan.plan_id,)),
            ('UPDATE qq_official_generation_capacity SET source_migration_item_id=NULL WHERE source_migration_item_id=?',(self.item,))):
            with self.subTest(sql=sql),self.assertRaises(DatabaseError):self.query(sql,args)
        with self.tx() as cursor,self.assertRaises(MigrationDenied):
            self.migration.require_artifact(cursor,MigrationArtifactProof('f'*32,None,self.file_job,1),now=self.now)
