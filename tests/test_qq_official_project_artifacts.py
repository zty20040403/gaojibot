"""Real project IO/grants and the existing M6 durable queue; no live platform."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.file_artifacts import file_job_kind
from src.plugins.ai_chat.adapters.qq_official.file_authority import assert_file_current, assert_file_read_current
from src.plugins.ai_chat.adapters.qq_official.file_services import FileServiceConfig, FileServices
from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
from src.plugins.ai_chat.adapters.qq_official.project_artifacts import ProjectArtifactAuthority, ProjectFileBridge
from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.project_io import ProjectIOStore
from src.plugins.ai_chat.projects import ProjectQuotaExceeded
from src.plugins.ai_chat.project_usage import ProjectUsageStore
from tests.qq_official_file_fixture import PDF
from tests.qq_official_message_fixture import record_evidence
from tests.qq_official_project_fixture import ProjectFixture


class ProjectArtifactTests(ProjectFixture):
    def setUp(self):
        super().setUp()
        record_evidence(self)
        self.query('''UPDATE qq_official_inbound_events i SET endpoint_generation=e.generation,route_epoch=e.route_epoch
            FROM qq_official_endpoints e WHERE e.endpoint_key=i.endpoint_key''')
        self.project = self.create()
        self.pid = self.project['project_id']
        self.store.open(self.access,self.pid,now=self.now)
        self.query("UPDATE projects SET status='active',sandbox_id='TEST_PROJECT_SANDBOX',workspace_generation=1 WHERE project_id=?",(self.pid,))
        self.project=self.store.get(self.access,self.pid)
        self.usage=ProjectUsageStore(self.store)
        self.usage.record(self.access,self.pid,dict(project_id=self.pid,generation=1,fence=self.project['lifecycle_fence'],
            quota_mode='soft',quota_bytes=self.project['quota_bytes'],used_bytes=0,sampled_at=self.now,complete=True),
            expected_version=self.project['version'],now=self.now)
        self.io=ProjectIOStore(self.store,self.usage)
        self.projects=SimpleNamespace(store=self.store,io=self.io)
        self.calls=DatabaseCalls()
        self.addCleanup(lambda:asyncio.run(self.calls.close()))
        self.deliveries=DeliveryStore(self.database,recover_interrupted=False)
        self.addCleanup(self.deliveries.close)
        self.outbox=OfficialSendStore(self.deliveries,admission=self.admission,pepper=b'TEST_PROJECT_FILES_PEPPER'*2,clock=lambda:self.now)
        self.runtime=SimpleNamespace(_calls=self.calls,deliveries=self.deliveries,jobs=self.jobs,outbox=self.outbox,
            _stopping=False,_token=self.token,store=SimpleNamespace(admitted=self.admission,clock=lambda:self.now))
        temp=tempfile.TemporaryDirectory(prefix='TEST_project_files_')
        self.addCleanup(temp.cleanup)
        self.read_live=True
        self.authority=ProjectArtifactAuthority(token=self.token,clock=lambda:self.now,route_guard=self.route_guard,
            read_route_guard=lambda c,**kw:self.read_live)
        self.config=FileServiceConfig(Path(temp.name),SimpleNamespace(),
            source_authority=self.authority,min_free_bytes=0)
        self.files=FileServices(self.runtime,self.token,None,self.config)
        self.addCleanup(lambda:asyncio.run(self.files.close()))
        self.outbox.protected_payloads=self.files.payloads
        self.bridge=ProjectFileBridge(files=self.files,projects=self.projects,authority=self.authority)
        self.grant=self.operation()

    def operation(self,path='report.pdf'):
        args={'path':path}
        digest=hashlib.sha256(json.dumps(['send_file_from_sandbox',args],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        turn=self.query('SELECT journal_turn_id FROM qq_official_turn_executions')[0]['journal_turn_id']
        grant=self.io.admit(self.access,self.pid,'send_file_from_sandbox',request_key=hashlib.sha256(path.encode()).hexdigest(),
            arguments_sha256=digest,expected_version=self.project['version'],journal_turn_id=turn,
            native_message_id=self.handle.source_native_message_id,now=self.now)
        self.io.dispatch(self.access,grant,now=self.now)
        return grant

    def put(self,**changes):
        return asyncio.run(self.bridge(self.access,changes.get('grant',self.grant),changes.get('content',PDF),
                                       changes.get('path','report.pdf'),changes.get('filename')))

    def check(self,artifact,*,read=False):
        args=dict(token=self.token,artifact_id=artifact,admission=self.admission,now=self.now,source_authority=self.authority)
        if not read:
            args['job']=self.file_job
        with self.tx() as cursor:
            return (assert_file_read_current if read else assert_file_current)(cursor,**args)

    def test_actual_io_uses_existing_queue_and_immutable_binding_once(self):
        result=self.put()
        self.assertEqual(result['status'],'stored')
        self.assertFalse(result['delivery_confirmed'])
        self.assertEqual(self.put(),result)
        artifact=result['artifact_handle'].split(':',1)[1]
        self.file_job=self.jobs.claim_due('TEST_PROJECT_FILE',kinds=(file_job_kind(self.account),),now=self.now)[0]
        self.assertEqual(self.check(artifact)[0]['artifact_id'],artifact)
        self.assertEqual(len(self.query('SELECT * FROM qq_official_project_artifacts')),1)
        self.assertEqual(len(self.query('SELECT * FROM qq_official_file_deliveries')),1)
        self.assertEqual(self.query('SELECT * FROM project_file_deliveries'),[])
        with self.tx() as cursor:
            self.assertEqual(self.store._scope_bytes(cursor,self.access.scope.key),len(PDF))
        self.assertEqual(self.usage.status(self.access,self.pid,now=self.now)['delivery_bytes'],len(PDF))

    def test_forged_io_or_changed_args_never_registers(self):
        for change in ({'grant':replace(self.grant,token='TEST_FORGED')},{'path':'other.pdf'},{'filename':'renamed.pdf'}):
            with self.subTest(change=tuple(change)):
                with self.assertRaises(PermissionError):self.put(**change)
        self.assertEqual(self.query('SELECT * FROM qq_official_file_artifacts'),[])

    def test_queue_and_source_binding_rollback_together(self):
        with patch.object(self.jobs,'enqueue',side_effect=RuntimeError('TEST rollback')):
            with self.assertRaises(RuntimeError):self.put()
        self.assertEqual(self.query('SELECT * FROM qq_official_file_artifacts'),[])
        self.assertEqual(self.query('SELECT * FROM qq_official_project_artifacts'),[])
        self.assertEqual(self.put()['status'],'stored')

    def test_scope_quota_prevents_durable_copy_and_job(self):
        self.query('UPDATE project_scope_limits SET quota_bytes=1')
        with self.assertRaises(ProjectQuotaExceeded):self.put()
        self.assertEqual(self.query('SELECT * FROM qq_official_file_artifacts'),[])
        self.assertEqual(self.query('SELECT * FROM qq_official_file_deliveries'),[])

    def test_read_survives_logical_freeze_but_never_revoked_grant(self):
        artifact=self.put()['artifact_handle'].split(':',1)[1]
        self.file_job=self.jobs.claim_due('TEST_PROJECT_FILE',kinds=(file_job_kind(self.account),),now=self.now)[0]
        self.routes_live=False
        with self.assertRaises((PermissionError,LeaseLost)):self.check(artifact)
        self.assertEqual(self.check(artifact,read=True)[0]['artifact_id'],artifact)
        self.query("UPDATE qq_official_project_grants SET state='revoked',revision=revision+1,revoked_at=issued_at")
        with self.assertRaises((PermissionError,LeaseLost)):self.check(artifact,read=True)

    def test_old_message_can_deliver_but_revoked_grant_cannot(self):
        artifact=self.put()['artifact_handle'].split(':',1)[1]
        self.file_job=self.jobs.claim_due('TEST_PROJECT_FILE',kinds=(file_job_kind(self.account),),now=self.now)[0]
        self.query('UPDATE qq_official_inbound_events SET received_at=received_at-301')
        self.assertEqual(self.check(artifact)[0]['artifact_id'],artifact)
        self.assertEqual(self.check(artifact,read=True)[0]['artifact_id'],artifact)
        self.query('UPDATE qq_official_project_grants SET revision=revision+1')
        with self.assertRaises((PermissionError,LeaseLost)):self.check(artifact,read=True)

    def test_late_io_receipt_and_artifact_send_share_scope_before_io_locks(self):
        artifact=self.put()['artifact_handle'].split(':',1)[1]
        self.file_job=self.jobs.claim_due('TEST_PROJECT_FILE',kinds=(file_job_kind(self.account),),now=self.now)[0]
        scope_held,send_started=threading.Event(),threading.Event()
        original_owned=self.io._owned

        def receipt_owned(cursor,grant):
            cursor.execute("SET LOCAL lock_timeout='2s'")
            cursor.execute('SELECT pg_advisory_xact_lock(hashtextextended(?,0))',
                           ('project-scope:'+self.access.scope.key,))
            scope_held.set()
            if not send_started.wait(5):
                raise AssertionError('artifact send did not reach route authorization')
            return original_owned(cursor,grant)

        def route(cursor,**kwargs):
            send_started.set()
            return self.route_guard(cursor,**kwargs)

        self.authority.route_guard=route
        with patch.object(self.io,'_owned',side_effect=receipt_owned),ThreadPoolExecutor(max_workers=1) as pool:
            observed=pool.submit(self.io.observe_result,self.grant,{'ok':True},now=self.now)
            self.assertTrue(scope_held.wait(5))
            self.assertEqual(self.check(artifact)[0]['artifact_id'],artifact)
            observed.result(timeout=5)
        self.assertEqual(self.query('SELECT result FROM project_io WHERE operation_id=?',
                                   (self.grant.operation_id,))[0]['result'],{'ok':True})

    async def physical_delivery(self,*,revoke_before_send=False):
        from src.plugins.ai_chat.adapters.qq_official.sender import OfficialSender
        from src.qq_official.credentials import Credentials
        from src.qq_official.http import RestClient
        from src.qq_official.token import TokenManager
        from tests.qq_official_file_http_fixture import FileMediaFake
        fake=FileMediaFake()
        fake.app_id=self.account.app_id
        fake.clock=lambda:self.now
        fake.proactive_allowed[('group',self.endpoint.native_conversation_id)]=True
        self.query("UPDATE qq_official_endpoints SET proactive_state='allowed' WHERE endpoint_key=?",(self.endpoint.key,))
        await fake.start()
        tokens=TokenManager(Credentials(self.account.app_id,fake.app_secret),fake.base_url,allow_test_server=True)
        rest=RestClient(tokens,allow_test_server=True)
        sender=OfficialSender(self.account,self.token,self.outbox,rest,self.ledger,
            journal=self.journal,database_calls=self.calls,ensure_current=self.files._fence,wall_clock=lambda:self.now)
        self.runtime.sender=sender
        self.files.uploader.rest=rest
        original_upload=self.files.uploader.upload
        async def upload(*args):
            result=await original_upload(*args)
            if revoke_before_send:
                self.query("UPDATE qq_official_project_grants SET state='revoked',revision=revision+1,revoked_at=issued_at")
            return result
        self.files.uploader.upload=upload
        try:
            result=await self.bridge(self.access,self.grant,PDF,'report.pdf')
            artifact=result['artifact_handle'].split(':',1)[1]
            if revoke_before_send:
                with self.assertRaises(Exception):
                    await self.files.dispatcher.send_artifact(artifact)
                self.assertEqual(fake.received,[])
            else:
                self.assertEqual((await self.files.dispatcher.send_artifact(artifact)).status,'committed')
                self.assertEqual(len(fake.received),1)
                self.assertEqual(fake.received[0]['payload']['msg_type'],7)
                self.assertEqual(self.query('SELECT state FROM qq_official_file_deliveries WHERE artifact_id=?',(artifact,))[0]['state'],'message_accepted')
            self.assertEqual(len(fake.media_calls),1)
        finally:
            await sender.stop()
            await rest.aclose()
            await tokens.aclose()
            await fake.close()

    def test_actual_project_io_file_worker_native_upload_and_m3_receipt(self):
        asyncio.run(self.physical_delivery())

    def test_revoke_between_upload_and_m3_prevents_physical_message(self):
        asyncio.run(self.physical_delivery(revoke_before_send=True))
