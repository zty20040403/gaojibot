"""A real task may capture locally after recipient evidence ages out.

Existing M6 upload and M3 send still demand fresh evidence under the original
grant. All credentials and transports are fake and confined to loopback.
"""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_qq_official_project_sessions as session_fixture
from tests.test_qq_official_file_dispatcher import FileMediaFake
from tests.qq_official_file_fixture import PDF
from src.plugins.ai_chat.adapters.qq_official.binding import BindingReducer
from src.plugins.ai_chat.adapters.qq_official.inbox import OfficialInboxStore
from src.plugins.ai_chat.adapters.qq_official.ingest import process_inbox
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.actor_evidence import recent_actor_evidence
from src.plugins.ai_chat.adapters.qq_official.file_artifacts import file_job_kind
from src.plugins.ai_chat.adapters.qq_official.file_services import NativeRouteEvidence
from src.plugins.ai_chat.adapters.qq_official.sender import OfficialSender
from src.plugins.ai_chat.project_usage import ProjectUsageStore
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker
from src.qq_official.credentials import Credentials
from src.qq_official.http import RestClient
from src.qq_official.token import TokenManager


class ProjectCaptureAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # A locally instantiated fixture is not discovered as another test
        # suite. Its schema and cleanups belong to this actual async test.
        class Fixture(session_fixture.ProjectSessionTests):
            pass
        f=self.f=Fixture('test_real_per_step_owners_baseline_and_cross_step_denial')
        self.addCleanup(Fixture.doClassCleanups)
        try:
            f.setUp()
        finally:
            for callback,args,kwargs in f._cleanups:
                self.addCleanup(callback,*args,**kwargs)
            f._cleanups.clear()
        await f.asyncSetUp();self.addAsyncCleanup(f.asyncTearDown)
        f.outbox.protected_payloads=f.files.payloads
        f.admit();f.tasks.set_task_state(f.task.task_id,'running',now=f.now)
        with f.tx() as cursor:
            self.provider,self.access=f.sessions._provider(cursor,f.task_job,f.tasks.get(f.task.task_id))
            self.source=f.authority.issue(cursor,access=self.access,project_id=f.project['project_id'])
        self.original_deadlines=(self.source.deadline_at,self.source.pending_deadline_at)
        self.original_grant=f.query('SELECT * FROM qq_official_project_grants')[0]

    async def register(self,key):
        f=self.f
        project=f.store.get(self.access,f.project['project_id'])
        ProjectUsageStore(f.store).record(self.access,project['project_id'],
            dict(project_id=project['project_id'],generation=project['workspace_generation'],
                fence=project['lifecycle_fence'],quota_mode='soft',quota_bytes=project['quota_bytes'],
                used_bytes=0,sampled_at=f.now,complete=True),expected_version=project['version'],now=f.now)
        return await f.calls(f.files.artifacts.register,self.source,content=PDF,
            filename='report.pdf',logical_request_key=hashlib.sha256(key.encode()).hexdigest())

    def observe(self, *, member=None):
        """Actual authenticated TxA receipt + TxB canonical actor evidence."""
        f=self.f
        if not hasattr(self,'inbox_store'):
            self.inbox_store=OfficialInboxStore(f.database,jobs=f.jobs,clock=lambda:f.now,
                binding=BindingReducer(b'TEST_CAPTURE_BINDING_PEPPER'*2,epoch_guard=assert_epoch,clock=lambda:f.now))
            self.addCleanup(self.inbox_store.close)
            self.sequence=0
            self.inbox_store.accept({'op':0,'t':'READY','s':0,'d':{'session_id':'TEST_CAPTURE'}},
                token=f.token,session_id='TEST_CAPTURE')
        self.sequence+=1
        native='TEST_CAPTURE_OBSERVED_'+str(self.sequence)
        accepted=self.inbox_store.accept({'op':0,'t':'GROUP_AT_MESSAGE_CREATE','s':self.sequence,
            'd':{'id':native,'group_openid':f.endpoint.native_conversation_id,
                 'author':{'member_openid':member or f.actor.native_user_id},'content':'TEST current file recipient',
                 'timestamp':datetime.fromtimestamp(f.now,timezone.utc).isoformat()}},
            token=f.token,session_id='TEST_CAPTURE')
        self.assertEqual(accepted.disposition,'inbox',accepted)
        with f.tx() as cursor:
            result=process_inbox(cursor,inbox_id=accepted.inbox_id,lease_token=f.token,
                ledger=f.ledger,jobs=f.jobs,epoch_guard=assert_epoch,
                admission=self.inbox_store.admitted,now=f.now)
        self.assertEqual(result.status,'processed')
        if hasattr(self,'fake'):
            self.fake.add_window('group',f.endpoint.native_conversation_id,native)
        return native

    async def transport(self):
        f=self.f
        self.fake=fake=FileMediaFake();fake.app_id=f.account.app_id;fake.clock=lambda:f.now;fake.receipt_ttl=1800
        await fake.start();self.addAsyncCleanup(fake.close)
        tokens=TokenManager(Credentials(f.account.app_id,fake.app_secret),fake.base_url,allow_test_server=True)
        self.addAsyncCleanup(tokens.aclose)
        self.rest=RestClient(tokens,allow_test_server=True);self.addAsyncCleanup(self.rest.aclose)
        route=f.query('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?',(f.endpoint.key,))[0]
        evidence=NativeRouteEvidence(f.endpoint,route['generation'],route['route_epoch'],
            route['capability_revision'],1024*1024,f.now,f.now+3600)
        f.files.config=replace(f.config,native_evidence=lambda *_:evidence)
        f.files.dispatcher.rest=self.rest
        self.worker=DurableJobWorker(f.jobs,logger=SimpleNamespace(error=lambda *_:None,warning=lambda *_:None),database_calls=f.calls)
        self.worker.register(file_job_kind(f.account),f.files.dispatcher)
        # Explicit configured capability: a new real observation supplies a new
        # reply window; neither the old window nor grant lifetime is renewed.
        f.outbox.allow_newer_windows=True
        async def current():
            await f.calls(f.files._fence_sync)
        self.sender=OfficialSender(f.account,f.token,f.outbox,self.rest,f.ledger,database_calls=f.calls,
            ensure_current=current,wall_clock=lambda:f.now)
        self.addAsyncCleanup(self.sender.stop)

    async def run_file(self):
        f=self.f
        with patch('src.plugins.ai_chat.storage.jobs.time',SimpleNamespace(time=lambda:f.now)):
            self.assertEqual(await self.worker.run_once(),1)

    async def test_after_301_seconds_same_artifact_and_plan_wait_for_real_inbox_without_renewal(self):
        f=self.f;f.now+=301
        with f.tx() as cursor:
            self.assertFalse(recent_actor_evidence(cursor,endpoint=f.endpoint,actor=f.actor,now=f.now,max_age_seconds=300))
        captured=await f.calls(f.files.artifacts.capture_verified,self.source,content=PDF,filename='report.pdf')
        self.assertEqual((captured.sha256,captured.byte_size),(hashlib.sha256(PDF).hexdigest(),len(PDF)))
        artifact=await self.register('TEST_CAPTURE_STALE_RECIPIENT')
        await self.transport()
        original=dict(f.query('SELECT * FROM qq_official_file_artifacts WHERE artifact_id=?',(artifact.artifact_id,))[0])
        await self.run_file()
        state=f.query('SELECT * FROM qq_official_file_deliveries WHERE artifact_id=?',(artifact.artifact_id,))[0]
        self.assertEqual(state['state'],'stored')
        self.assertEqual(f.jobs.get(state['job_id']).status,'pending')
        self.assertEqual(f.query('SELECT * FROM qq_official_file_stage_attempts'),[])
        self.assertEqual(self.fake.media_calls,[]);self.assertEqual(self.fake.received,[])
        self.observe();f.now+=16
        f.tasks.set_task_state(f.task.task_id,'completed',now=f.now)
        await self.run_file()
        self.assertEqual(len(self.fake.media_calls),1)
        state=f.query('SELECT * FROM qq_official_file_deliveries WHERE artifact_id=?',(artifact.artifact_id,))[0]
        self.assertEqual(state['state'],'handed_off',dict(state))
        delivery=f.query('SELECT delivery_id FROM qq_official_file_message_plans WHERE artifact_id=?',(artifact.artifact_id,))[0][0]
        plan_before=dict(f.query('SELECT * FROM delivery_plans WHERE plan_id=?',(state['active_plan_id'],))[0])
        f.now+=301
        outcome=await self.sender.send_delivery(delivery)
        self.assertEqual((outcome.status,outcome.reason),('pending','recipient_evidence_unavailable'))
        self.assertEqual(f.query('SELECT * FROM delivery_attempts'),[])
        self.assertEqual(self.fake.received,[])
        self.observe();f.now+=16
        outcome=await self.sender.send_delivery(delivery)
        self.assertEqual(outcome.status,'committed',outcome)
        self.assertEqual(len(self.fake.media_calls),1);self.assertEqual(len(self.fake.received),1)
        self.assertEqual(len(f.query('SELECT * FROM qq_official_file_artifacts')),1)
        after=dict(f.query('SELECT * FROM qq_official_file_artifacts WHERE artifact_id=?',(artifact.artifact_id,))[0])
        self.assertEqual(after['pending_deadline_at'],original['pending_deadline_at'])
        self.assertLessEqual(after['retain_until'],original['retain_until'])
        self.assertEqual(after['created_at'],original['created_at'])
        self.assertEqual(dict(f.query('SELECT * FROM delivery_plans WHERE plan_id=?',(state['active_plan_id'],))[0]),plan_before)
        self.assertEqual((self.source.deadline_at,self.source.pending_deadline_at),self.original_deadlines)
        self.assertEqual(f.query('SELECT * FROM qq_official_project_grants')[0],self.original_grant)

    async def prepared_file(self, key):
        artifact=await self.register(key)
        await self.transport()
        self.observe()
        self.f.tasks.set_task_state(self.f.task.task_id,'completed',now=self.f.now)
        return artifact

    async def test_confirmed_upload_waits_and_restores_same_credential_after_new_evidence(self):
        f=self.f;artifact=await self.prepared_file('TEST_AFTER_UPLOAD_FRESH')
        original=self.rest.upload
        async def upload(*args,**kwargs):
            response=await original(*args,**kwargs)
            f.now+=301
            return response
        with patch.object(self.rest,'upload',upload):
            await self.run_file()
        row=f.query('SELECT * FROM qq_official_file_stage_attempts WHERE artifact_id=?',(artifact.artifact_id,))[0]
        self.assertEqual(row['state'],'confirmed')
        self.assertEqual(len(self.fake.media_calls),1)
        self.assertEqual(f.query('SELECT * FROM qq_official_file_message_plans'),[])
        self.observe();f.now+=16
        await self.run_file()
        self.assertEqual(len(self.fake.media_calls),1)
        self.assertEqual(dict(f.query('SELECT * FROM qq_official_file_stage_attempts WHERE artifact_id=?',(artifact.artifact_id,))[0]),dict(row))
        delivery=f.query('SELECT delivery_id FROM qq_official_file_message_plans WHERE artifact_id=?',(artifact.artifact_id,))[0][0]
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')

    async def test_after_stage_claim_proven_unsent_waits_but_preserves_original_attempt_evidence(self):
        f=self.f;artifact=await self.prepared_file('TEST_PRE_UPLOAD_FRESH')
        original=f.files.dispatcher._begin_stage
        def begin(*args):
            result=original(*args);f.now+=301;return result
        with patch.object(f.files.dispatcher,'_begin_stage',begin):
            await self.run_file()
        rows=f.query('SELECT * FROM qq_official_file_stage_attempts WHERE artifact_id=?',(artifact.artifact_id,))
        self.assertEqual([(r['state'],r['failure_code']) for r in rows],[('unsent','recipient_evidence_unavailable')])
        self.assertEqual(self.fake.media_calls,[])
        self.observe();f.now+=16
        await self.run_file()
        self.assertEqual(len(self.fake.media_calls),1)
        self.assertEqual(dict(f.query('SELECT * FROM qq_official_file_stage_attempts WHERE stage_attempt_id=?',(rows[0]['stage_attempt_id'],))[0]),dict(rows[0]))
        self.assertEqual(len(f.query('SELECT * FROM qq_official_file_stage_attempts WHERE artifact_id=?',(artifact.artifact_id,))),2)

    async def test_link_preinsert_authority_wait_preserves_original_publication_expiry(self):
        f=self.f;artifact=await self.prepared_file('TEST_PRE_PUBLICATION_FRESH')
        f.files.config=replace(f.files.config,native_evidence=None)
        factory=f.files.dispatcher.publisher_factory
        def publisher(authority):
            value=factory(authority)
            original=value.store.store._authority
            def expire(*args):
                f.now+=301
                return original(*args)
            value.store.store._authority=expire
            return value
        with patch.object(f.files.dispatcher,'publisher_factory',publisher):
            await self.run_file()
        row=f.query('SELECT * FROM qq_official_file_deliveries WHERE artifact_id=?',(artifact.artifact_id,))[0]
        self.assertEqual(row['reason_code'],'publication_not_created_recipient_wait')
        self.assertEqual(f.query('SELECT * FROM qq_official_file_publications'),[])
        self.observe();f.now+=16
        await self.run_file()
        publication=f.query('SELECT * FROM qq_official_file_publications')[0]
        self.assertEqual(publication['request_key'],row['publication_request_key'])
        self.assertEqual(publication['expires_at'],row['publication_expires_at'])
        self.assertEqual(len(f.query('SELECT * FROM qq_official_file_message_plans')),1)
        self.assertEqual(self.fake.media_calls,[])

    async def test_wait_does_not_mask_revocation_or_original_grant_expiry(self):
        f=self.f;artifact=await self.prepared_file('TEST_WAIT_REVOKE')
        f.now+=301
        await self.run_file()
        f.query("UPDATE qq_official_project_grants SET state='revoked',revision=revision+1,revoked_at=?",(f.now,))
        f.now+=16
        await self.run_file()
        row=f.query('SELECT * FROM qq_official_file_deliveries WHERE artifact_id=?',(artifact.artifact_id,))[0]
        self.assertEqual(row['state'],'cancelled')
        self.assertEqual(self.fake.media_calls,[])
        self.assertEqual(f.query('SELECT * FROM qq_official_file_stage_attempts'),[])

    async def test_mark_freshness_race_is_confirmed_unsent_and_same_plan_can_resume(self):
        f=self.f;artifact=await self.prepared_file('TEST_MARK_FRESH')
        await self.run_file()
        delivery=f.query('SELECT delivery_id FROM qq_official_file_message_plans WHERE artifact_id=?',(artifact.artifact_id,))[0][0]
        f.now+=300
        self.observe(member='TEST_OTHER_MEMBER')
        original=f.outbox.mark_dispatching
        def mark(*args):
            f.now+=1
            return original(*args)
        with patch.object(f.outbox,'mark_dispatching',mark):
            outcome=await self.sender.send_delivery(delivery)
        self.assertEqual(outcome.status,'pending',outcome)
        attempt=f.query('SELECT * FROM delivery_attempts WHERE delivery_id=?',(delivery,))[0]
        self.assertEqual((attempt['state'],attempt['acceptance']),('cancelled_unsent','confirmed_no'))
        self.assertEqual(self.fake.received,[])
        self.observe();f.now+=16
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertEqual(len(self.fake.media_calls),1);self.assertEqual(len(self.fake.received),1)

    async def test_original_deadline_ends_wait_without_upload(self):
        f=self.f;artifact=await self.prepared_file('TEST_WAIT_EXPIRES')
        f.now+=301
        await self.run_file()
        f.now=self.original_grant['expires_at']
        await self.run_file()
        row=f.query('SELECT * FROM qq_official_file_deliveries WHERE artifact_id=?',(artifact.artifact_id,))[0]
        self.assertEqual(row['state'],'cancelled')
        self.assertEqual(self.fake.media_calls,[])
        self.assertEqual(f.query('SELECT * FROM qq_official_file_stage_attempts'),[])

    async def test_unknown_upload_is_not_revived_by_missing_or_new_evidence(self):
        f=self.f;artifact=await self.prepared_file('TEST_UPLOAD_UNKNOWN')
        self.fake.drop_response=True
        await self.run_file()
        row=f.query('SELECT * FROM qq_official_file_deliveries WHERE artifact_id=?',(artifact.artifact_id,))[0]
        self.assertEqual(row['state'],'upload_unknown')
        self.fake.drop_response=False;f.now+=301
        self.observe()
        self.assertEqual(f.jobs.get(row['job_id']).status,'succeeded')
        with patch('src.plugins.ai_chat.storage.jobs.time',SimpleNamespace(time=lambda:f.now)):
            self.assertEqual(await self.worker.run_once(),0)
        self.assertEqual(f.query('SELECT state FROM qq_official_file_stage_attempts')[0][0],'unknown')
        self.assertEqual(len(self.fake.media_calls),1)
        self.assertEqual(f.query('SELECT * FROM qq_official_file_message_plans'),[])

    async def test_link_committed_before_freshness_loss_restores_original_publication(self):
        f=self.f;artifact=await self.prepared_file('TEST_POST_PUBLICATION_FRESH')
        f.files.config=replace(f.files.config,native_evidence=None)
        factory=f.files.dispatcher.publisher_factory
        def publisher(authority):
            value=factory(authority)
            original=value.publish
            async def publish(*args,**kwargs):
                result=await original(*args,**kwargs)
                f.now+=301
                return result
            value.publish=publish
            return value
        with patch.object(f.files.dispatcher,'publisher_factory',publisher):
            await self.run_file()
        original=dict(f.query('SELECT * FROM qq_official_file_publications')[0])
        self.assertEqual(f.query('SELECT * FROM qq_official_file_message_plans'),[])
        self.assertEqual(f.query('SELECT state FROM qq_official_file_deliveries WHERE artifact_id=?',(artifact.artifact_id,))[0][0],'publishing')
        self.observe();f.now+=16
        await self.run_file()
        self.assertEqual([dict(r) for r in f.query('SELECT * FROM qq_official_file_publications')],[original])
        delivery=f.query('SELECT delivery_id FROM qq_official_file_message_plans WHERE artifact_id=?',(artifact.artifact_id,))[0][0]
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertEqual(self.fake.media_calls,[]);self.assertEqual(len(self.fake.received),1)

    async def test_prepare_freshness_loss_allocates_no_attempt_window_or_sequence(self):
        f=self.f;artifact=await self.prepared_file('TEST_PREPARE_FRESH')
        await self.run_file()
        delivery=f.query('SELECT delivery_id FROM qq_official_file_message_plans WHERE artifact_id=?',(artifact.artifact_id,))[0][0]
        f.now+=300;self.observe(member='TEST_OTHER_MEMBER')
        windows=[dict(r) for r in f.query('SELECT * FROM qq_official_reply_windows ORDER BY window_id')]
        original=f.outbox._content
        def content(*args,**kwargs):
            f.now+=1
            return original(*args,**kwargs)
        with patch.object(f.outbox,'_content',content):
            outcome=await self.sender.send_delivery(delivery)
        self.assertEqual((outcome.status,outcome.reason),('pending','recipient_evidence_unavailable'))
        self.assertEqual(f.query('SELECT * FROM delivery_attempts'),[])
        self.assertEqual([dict(r) for r in f.query('SELECT * FROM qq_official_reply_windows ORDER BY window_id')],windows)
        self.assertEqual(self.fake.received,[])
        self.observe();f.now+=16
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')

    async def test_fresh_unsent_then_confirmed_native_rejection_uses_existing_link_route(self):
        f=self.f;artifact=await self.prepared_file('TEST_FRESH_REJECTION_LINK')
        original=f.files.dispatcher._begin_stage
        def begin(*args):
            result=original(*args);f.now+=301;return result
        with patch.object(f.files.dispatcher,'_begin_stage',begin):
            await self.run_file()
        self.assertEqual(self.fake.media_calls,[])
        before=dict(f.query('SELECT * FROM qq_official_file_stage_attempts')[0])
        self.assertEqual((before['state'],before['failure_code']),('unsent','recipient_evidence_unavailable'))
        self.observe();f.now+=16
        self.fake.response_status=400;self.fake.response_code=50015014
        await self.run_file()
        self.assertEqual(len(self.fake.media_calls),1)
        row=f.query('SELECT * FROM qq_official_file_deliveries WHERE artifact_id=?',(artifact.artifact_id,))[0]
        self.assertEqual((row['route'],row['state']),('link','handed_off'))
        self.assertEqual(len(f.query('SELECT * FROM qq_official_file_publications')),1)
        self.assertEqual(dict(f.query('SELECT * FROM qq_official_file_stage_attempts WHERE stage_attempt_id=?',
            (before['stage_attempt_id'],))[0]),before)
        delivery=f.query('SELECT delivery_id FROM qq_official_file_message_plans WHERE artifact_id=?',(artifact.artifact_id,))[0][0]
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertEqual(len(self.fake.media_calls),1);self.assertEqual(len(self.fake.received),1)

    async def test_revoking_original_grant_still_blocks_capture_before_disk_write(self):
        f=self.f;f.now+=301
        f.query("UPDATE qq_official_project_grants SET state='revoked',revision=revision+1,revoked_at=?",
            (f.now,))
        with patch.object(f.files.artifacts,'_store_blob',wraps=f.files.artifacts._store_blob) as store:
            with self.assertRaises(PermissionError):
                await f.calls(f.files.artifacts.capture_verified,self.source,content=PDF,filename='report.pdf')
        store.assert_not_called()
        self.assertEqual(f.query('SELECT * FROM qq_official_file_artifacts'),[])

    async def test_execute_without_original_deliver_capability_cannot_capture(self):
        f=self.f
        f.query("UPDATE qq_official_project_grants SET capabilities=array_remove(capabilities,'project.deliver')")
        with self.assertRaises(PermissionError):
            await f.calls(f.files.artifacts.capture_verified,self.source,content=PDF,filename='report.pdf')
        self.assertEqual(f.query('SELECT * FROM qq_official_file_artifacts'),[])
