"""Legacy immutable bytes -> real migration item/job -> M6 upload -> M3."""
import hashlib
import io
import json
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_qq_official_migration_artifacts as fixture
from tests.qq_official_outbox_fixture import OutboxFixture
from src.plugins.ai_chat.adapters.qq_official.file_artifacts import file_job_kind
from src.plugins.ai_chat.adapters.qq_official.file_dispatcher import OfficialFileDispatcher
from src.plugins.ai_chat.adapters.qq_official.file_payloads import TrustedFilePayloads
from src.plugins.ai_chat.adapters.qq_official.migration_files import MigrationFiles,job_kind
from src.plugins.ai_chat.adapters.qq_official.migration_host_ports import MigrationPlanGuard
from src.plugins.ai_chat.adapters.qq_official.migration_preview import SourceRef
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.agent.contracts import ContextPacket
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker


class MigrationFileTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    owned=fixture.MigrationArtifactTests.owned
    new_engine=fixture.MigrationArtifactTests.new_engine
    ensure_current=fixture.MigrationArtifactTests.ensure_current
    rotate_mapping=fixture.MigrationArtifactTests.rotate_mapping
    start=fixture.MigrationArtifactTests.start
    freeze=fixture.MigrationArtifactTests.freeze
    convert=fixture.MigrationArtifactTests.convert
    complete=fixture.MigrationArtifactTests.complete
    tick=fixture.MigrationArtifactTests.tick
    asyncTearDown=fixture.MigrationArtifactTests.asyncTearDown

    async def asyncSetUp(self):
        await fixture.MigrationArtifactTests.asyncSetUp(self)
        self.legacy_root=self.private/'legacy'
        self.legacy_root.mkdir(mode=0o700)
        self.engine=MigrationFiles(self.database,token=self.token,jobs=self.jobs,journal=self.migration,
            preview=self.preview,admission=lambda c,e,a:e==self.endpoint and a==self.actor,clock=lambda:self.now,
            database_calls=self.calls,artifact_store=self.files.artifacts,legacy_snapshot_root=self.legacy_root)
        self.authority.imports=self.engine
        self.worker=DurableJobWorker(self.jobs,logger=self.logger,database_calls=self.calls,worker_id='TEST_OLD_FILE_IMPORT')
        self.worker.register(job_kind(self.account),self.engine.execute)

    def legacy_file(self,kind='pdf',state='queued'):
        if kind=='pdf':content=b'%PDF-1.7\nTEST original captured bytes\n%%EOF\n'
        else:
            output=io.BytesIO()
            with zipfile.ZipFile(output,'w') as archive:archive.writestr('result.txt','TEST original ZIP')
            content=output.getvalue()
        sha=hashlib.sha256(content).hexdigest()
        task=self.store.prepare_task(packet=ContextPacket(self.source_scope.key,'TEST legacy captured',201,None,'TEST report'),
            decision=None,dispatch={'bot_id':'301','deadline':self.execution_deadline,'profile':'test'},
            max_parallelism=1,max_steps=1,now=self.now)
        self.store.set_task_state(task.task_id,'completed',result={'answer':'TEST old completed'},now=self.now)
        path=self.legacy_root/str(task.task_id);path.mkdir(mode=0o700)
        self.snapshot=path/sha;self.snapshot.write_bytes(content);self.snapshot.chmod(0o400)
        payload={'artifact':{'snapshot':sha,'size':len(content),'handle':'TEST_OLD_HANDLE_MUST_NOT_COPY'},
            'filename':'report.'+kind,'draft':False,'draft_reason':''}
        self.query('INSERT INTO subagent_deliveries VALUES (?,?,?,?,?,?)',
            (task.task_id,1,sha,state,json.dumps(payload),self.now))
        self.old_task=task;self.original=content
        return SourceRef('file',f'subagent:{task.task_id}:1:{sha}')

    async def imported(self,kind='pdf'):
        self.plan=self.start(self.legacy_file(kind))
        self.item,self.binding=self.convert(self.plan)
        self.assertEqual(await self.tick(),0)
        self.assertEqual(self.jobs.get(self.binding['job_id']).attempts,0)
        self.complete(self.plan,self.item)
        self.assertEqual(await self.tick(),1)
        self.assertEqual(self.jobs.get(self.binding['job_id']).status,'succeeded',self.logger.mock_calls)
        self.artifact=dict(self.query('SELECT * FROM qq_official_file_artifacts')[0])
        return self.artifact

    def transport(self):
        self.outbox.source_guard=MigrationPlanGuard(native=SimpleNamespace(plan=lambda **_:False),journal=self.migration)
        self.payloads=TrustedFilePayloads(admission=self.engine.admission,route_policy=lambda c,a,r:r=='native',
            native_vault=self.files.native_vault,source_authority=self.authorities)
        self.outbox.protected_payloads=self.payloads
        return OfficialFileDispatcher(self.token,self.files.artifacts,self.payloads,self.outbox,self.rest,
            calls=self.calls,lease_check=lambda:True,clock=lambda:self.now)

    async def test_legacy_pdf_actual_import_and_upload_message_keeps_no_fake_source(self):
        artifact=await self.imported()
        for key in ('source_task_id','source_task_revision','source_execution_id','source_native_message_id','source_canonical_message_id'):
            self.assertIsNone(artifact[key])
        self.assertEqual(artifact['source_job_id'],self.binding['job_id'])
        self.assertEqual(artifact['pending_deadline_at'],self.result_deadline)
        self.assertEqual(self.files.artifacts.blobs.read(artifact['sha256']),self.original)
        self.assertEqual(self.query('SELECT state FROM subagent_deliveries')[0][0],'migrated')
        self.assertNotIn('TEST_OLD_HANDLE',json.dumps(artifact))
        self.worker.register(file_job_kind(self.account),self.transport())
        self.assertEqual(await self.tick(),1)
        self.assertEqual(len(self.fake.media_calls),1)
        plan=self.query('SELECT * FROM migration_artifact_plans')[0]
        self.assertNotEqual(plan['file_job_id'],plan['source_job_id'])
        delivery=self.query('SELECT delivery_id FROM deliveries WHERE plan_id=?',(plan['plan_id'],))[0][0]
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertEqual(len(self.fake.received),1)
        self.assertEqual(self.fake.received[0]['payload']['msg_type'],7)
        await self.sender.send_delivery(delivery)
        self.assertEqual(len(self.fake.received),1)

    async def test_zip_import_replay_after_register_crash_reuses_one_artifact(self):
        source=self.legacy_file('zip');plan=self.start(source);item,b=self.convert(plan);self.complete(plan,item)
        job=self.jobs.claim_due('TEST_IMPORT_CRASH',kinds=(job_kind(self.account),),now=self.now)[0]
        self.engine._import(job)  # Simulate crash before durable job settlement.
        self.snapshot.unlink()  # Registered target CAS is now the immutable object.
        self.engine._import(job)
        self.assertEqual(len(self.query('SELECT * FROM qq_official_file_artifacts')),1)
        self.assertEqual(len(self.query('SELECT * FROM qq_official_file_deliveries')),1)
        self.assertEqual(self.query('SELECT mime FROM qq_official_file_artifacts')[0][0],'application/zip')
        self.assertEqual(self.fake.media_calls,[])

    async def test_confirmed_and_unknown_legacy_files_never_get_target_jobs(self):
        for state,expected in (('acknowledged','confirmed'),('unknown','unknown'),('sending','unknown')):
            source=self.legacy_file(state=state)
            with self.tx() as cursor:value=self.preview.preview(cursor,self.selection,source,now=self.now)
            self.assertEqual(value.disposition,expected);self.assertIsNone(value.export)
        self.assertEqual(self.query('SELECT count(*) FROM migration_file_continuations')[0][0],0)
        self.assertEqual(self.query('SELECT count(*) FROM qq_official_file_artifacts')[0][0],0)

    async def test_source_tamper_or_mapping_revoke_blocks_copy_and_stored_send(self):
        source=self.legacy_file();plan=self.start(source);item,b=self.convert(plan);self.complete(plan,item)
        self.snapshot.chmod(0o600);self.snapshot.write_bytes(b'TEST corrupted')
        job=self.jobs.claim_due('TEST_BAD_BYTES',kinds=(job_kind(self.account),),now=self.now)[0]
        with self.assertRaises(MigrationDenied):self.engine._import(job)
        self.assertEqual(self.query('SELECT count(*) FROM qq_official_file_artifacts')[0][0],0)
        self.snapshot.write_bytes(self.original);self.engine._import(job)
        self.rotate_mapping()
        artifact=self.query('SELECT artifact_id FROM qq_official_file_artifacts')[0][0]
        with self.tx() as cursor,self.assertRaises(MigrationDenied):
            self.migration.current_artifact_source(cursor,artifact,now=self.now)
        self.assertEqual(self.fake.received,[])

    async def test_project_boolean_preview_cannot_select_an_unreviewed_grant(self):
        from src.plugins.ai_chat.adapters.qq_official.project_authority import ProjectGrantRef
        source=self.legacy_file()
        self.preview.project_guard=lambda *args:True
        with self.tx() as cursor:
            result=self.preview._mapped(cursor,self.selection,source,'201',project=7,operation='deliver')
        self.assertNotIn('project_grant',result)
        selected=ProjectGrantRef('a'*32,3)
        self.preview.project_guard=lambda *args:selected
        with self.tx() as cursor:
            result=self.preview._mapped(cursor,self.selection,source,'201',project=7,operation='deliver')
        self.assertEqual(result['project_grant'],{'grant_id':selected.grant_id,'revision':3})

    async def test_import_source_deadline_never_renews(self):
        source=self.legacy_file();plan=self.start(source);item,b=self.convert(plan);self.complete(plan,item)
        self.now=self.execution_deadline
        job=self.jobs.claim_due('TEST_EXPIRED_IMPORT',kinds=(job_kind(self.account),),now=self.now)[0]
        with self.assertRaises(MigrationDenied):self.engine._import(job)
        self.assertEqual(self.query('SELECT count(*) FROM qq_official_file_artifacts')[0][0],0)
        self.assertEqual(self.query('SELECT execution_deadline,result_deadline FROM migration_file_continuations')[0],
            {'execution_deadline':self.execution_deadline,'result_deadline':self.result_deadline})

    async def project_file(self):
        from tests.qq_official_migration_project_fixture import install_project_owner
        from src.plugins.ai_chat.projects import ProjectStore,ProjectAccess
        from src.plugins.ai_chat.message_ir import MediaNode,MessageBody
        from src.plugins.ai_chat.adapters.qq_official.migration_project_grants import ProjectMigrationGrants
        install_project_owner(self)
        self.projects=self.owned(ProjectStore(self.database,self.private/'old-projects'))
        principal=self.ledger.ensure_principal_identity('onebot-v11','201','TEST old author')
        project=self.projects.create(ProjectAccess(self.source_scope,principal,member=True),'TEST original files',now=self.now)
        self.original=b'%PDF-1.7\nTEST original project bytes\n%%EOF\n'
        blob=self.projects.blobs.put(self.original)
        with self.tx() as cursor:
            self.projects._register_blob(cursor,blob,self.now)
            operation=cursor.execute('''INSERT INTO project_io
                (project_id,actor_principal_id,request_key,token,tool_name,arguments_sha256,sandbox_id,generation,fence,
                 mutating,reserved_bytes,state,result,evidence,created_at,updated_at)
                VALUES (?,?,?,?,'send_file_from_sandbox',?,'TEST_CAPTURED_GUEST',1,0,FALSE,0,'completed','{}','{}',?,?)
                RETURNING operation_id''',(project['project_id'],principal,'b'*64,'c'*32,'d'*64,self.now,self.now)).fetchone()[0]
            delivery,_=self.deliveries.enqueue(idempotency_key='project-file:'+str(operation),
                source_scope_key=self.source_scope.key,target_scope=self.source_scope,
                body=MessageBody((MediaNode(0,'file',name='original.pdf',source_type='project_attachment'),)),
                transaction_cursor=cursor,now=self.now)
            cursor.execute('''INSERT INTO project_file_deliveries
                (delivery_id,operation_id,project_id,sha256,blob_sha256,byte_size,filename,path)
                VALUES (?,?,?,?,?,?,'original.pdf','TEST_PATH_NOT_EXPORTED')''',
                (delivery.delivery_id,operation,project['project_id'],blob.sha256,blob.sha256,blob.byte_size))
        host=SimpleNamespace(control=self.mapping_authority,routes=self.routes,mappings=self.mapping_store,
            journal=self.migration,transaction=self.tx,database_calls=self.calls,clock=lambda:self.now,
            baseline_source=lambda c,s,n:self.routes._baseline(c,s.account.key,n))
        self.project_grants=ProjectMigrationGrants(host)
        self.grant=await self.project_grants.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,
            action='project_grant_stage',request=dict(source_endpoint=self.source.key,source_scope=self.source_scope.key,
                source_actor=self.source_actor.key,target_endpoint=self.endpoint.key,target_actor=self.actor.key,
                issuer_actor=self.actor.key,project_id=project['project_id'],mapping_id=self.mapping_evidence.mapping_id,
                mapping_revision=self.mapping_evidence.revision,capabilities=['project.deliver'],expires_at=self.now+600))
        self.preview.project_guard=self.project_grants.preview
        self.engine.projects,self.engine.project_grants=self.projects,self.project_grants
        self.old_delivery=delivery
        return SourceRef('file','project:'+str(delivery.delivery_id))

    def start_project(self,source):
        from tests.qq_official_migration_journal_fixture import JournalFixture
        with self.tx() as cursor:
            plan=self.migration.plan(cursor,source_endpoint=self.source,source_scope=self.source_scope,
                target_endpoint=self.endpoint,items=((source.kind,source.key),),authorization=self.authorization,
                baseline_source=self.baseline_source,deadline=self.now+500,now=self.now)
            self.project_grants.bind_plan(cursor,migration_id=plan.migration_id,now=self.now)
        JournalFixture.switch(self,plan)
        return plan

    def complete_project(self,plan,item):
        from tests import test_qq_official_receipts as receipt_fixture
        # The real owner binding advanced the actual endpoint epoch. Preserve
        # that fact in this existing synthetic transport receipt fixture.
        epoch=self.query('SELECT route_epoch FROM qq_official_endpoints WHERE endpoint_key=?',(self.endpoint.key,))[0][0]
        context=receipt_fixture.OfficialAttemptContext
        def current_context(token,identifier,fields):
            return context(token,identifier,tuple((k,epoch if k=='route_epoch' else v) for k,v in fields))
        original_window=self.window
        def current_window(**kwargs):
            identifier=original_window(**kwargs)
            self.query('UPDATE qq_official_reply_windows SET route_epoch=? WHERE window_id=?',(epoch,identifier))
            return identifier
        with patch.object(receipt_fixture,'OfficialAttemptContext',side_effect=current_context),patch.object(self,'window',side_effect=current_window):
            self.complete(plan,item)

    async def test_project_cas_and_console_deliver_only_grant_continue_without_execution_power(self):
        source=await self.project_file()
        with self.tx() as cursor:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            preview=self.preview.preview(cursor,self.selection,source,now=self.now)
        self.assertEqual(preview.export['mapping']['project_grant'],{'grant_id':self.grant['grant_id'],'revision':1})
        plan=self.start_project(source);item,b=self.convert(plan)
        self.assertEqual(self.deliveries.get(self.old_delivery.delivery_id).status,'cancelled')
        self.complete_project(plan,item)
        self.assertEqual(await self.tick(),1)
        self.assertEqual(self.jobs.get(b['job_id']).status,'succeeded',self.logger.mock_calls)
        artifact=self.query('SELECT * FROM qq_official_file_artifacts')[0]
        self.assertEqual(self.files.artifacts.blobs.read(artifact['sha256']),self.original)
        self.assertIsNone(artifact['source_task_id'])
        stored=self.query('SELECT source_json,grant_id,grant_revision FROM migration_file_continuations')[0]
        self.assertNotIn('TEST_PATH_NOT_EXPORTED',stored['source_json'])
        self.assertEqual((stored['grant_id'],stored['grant_revision']),(self.grant['grant_id'],1))
        window=self.window(source='TEST_PROJECT_FILE_REPLY')
        epoch=self.query('SELECT route_epoch FROM qq_official_endpoints WHERE endpoint_key=?',(self.endpoint.key,))[0][0]
        self.query('UPDATE qq_official_reply_windows SET route_epoch=? WHERE window_id=?',(epoch,window))
        self.fake.add_window('group',self.endpoint.native_conversation_id,'TEST_PROJECT_FILE_REPLY')
        self.worker.register(file_job_kind(self.account),self.transport())
        self.assertEqual(await self.tick(),1)
        self.assertEqual(len(self.fake.media_calls),1)
        plan_id=self.query('SELECT plan_id FROM migration_artifact_plans')[0][0]
        delivery=self.query('SELECT delivery_id FROM deliveries WHERE plan_id=?',(plan_id,))[0][0]
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        await self.project_grants.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,action='project_grant_revoke',
            request={'grant_id':self.grant['grant_id'],'expected_revision':1})
        with self.tx() as cursor,self.assertRaises(MigrationDenied):
            self.migration.current_artifact_source(cursor,artifact['artifact_id'],now=self.now)
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertEqual(len(self.fake.received),1)

    async def test_project_unknown_upload_is_not_converted_even_with_current_grant(self):
        source=await self.project_file()
        self.query('UPDATE project_file_deliveries SET upload_started_at=? WHERE delivery_id=?',(self.now,self.old_delivery.delivery_id))
        with self.tx() as cursor:value=self.preview.preview(cursor,self.selection,source,now=self.now)
        self.assertEqual(value.disposition,'unknown');self.assertIsNone(value.export)
        self.assertEqual(self.query('SELECT count(*) FROM migration_file_continuations')[0][0],0)
