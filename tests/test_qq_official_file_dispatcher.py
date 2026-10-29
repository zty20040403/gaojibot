"""Actual durable file jobs, PostgreSQL, private vaults, loopback upload and M3."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.file_artifacts import file_job_kind
from src.plugins.ai_chat.adapters.qq_official.file_dispatcher import OfficialFileDispatcher, FileDispatchUnavailable, native_rejected
from src.plugins.ai_chat.adapters.qq_official.file_links import FileCredentialVault, FileLinkPublisher
from src.plugins.ai_chat.adapters.qq_official.file_payloads import TrustedFilePayloads
from src.plugins.ai_chat.adapters.qq_official.file_publications import PgPublicationStore
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.native_vault import NativeUploadVault
from src.plugins.ai_chat.adapters.qq_official.outbox import OfficialOutbox
from src.plugins.ai_chat.adapters.qq_official.sender import OfficialSender
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker, JobDeferred
from src.qq_official.credentials import Credentials
from src.qq_official.http import RestClient
from src.qq_official.token import TokenManager
from tests.qq_official_file_fixture import FileFixture
from tests.test_qq_official_native_upload import MediaFake


class FileMediaFake(MediaFake):
    """Type-7 fixture contract; retain M0's actual window/sequence/quota checks."""
    async def _send(self, request):
        body = await request.json()
        if body.get('msg_type') != 7:
            return await super()._send(request)
        if body.get('media') != {'file_info': 'TEST_FILE_INFO'}:
            from aiohttp import web
            return web.json_response({'code': 50041}, status=400)
        original = request
        class WindowRequest:
            def __getattr__(self, name):
                return getattr(original, name)
            async def json(self):
                return {**body, 'msg_type': 0, 'content': 'TEST media-window accounting'}
        count = len(self.received)
        response = await super()._send(WindowRequest())
        if len(self.received) > count:
            self.received[-1]['payload'] = body
        return response


class FileDispatcherTests(FileFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = DatabaseCalls()
        self.temporary = tempfile.TemporaryDirectory(prefix='qq-file-dispatch-')
        self.addCleanup(self.temporary.cleanup)
        self.native_vault = NativeUploadVault(Path(self.temporary.name)/'native', clock=lambda:self.now)
        self.link_vault = FileCredentialVault(Path(self.temporary.name)/'links')
        self.addCleanup(self.native_vault.close)
        self.addCleanup(self.link_vault.close)
        self.native_allowed, self.link_allowed, self.ready, self.local_current = True, True, True, True
        self.payloads = TrustedFilePayloads(admission=self.admission, route_policy=self.policy,
            native_vault=self.native_vault, link_vault=self.link_vault, link_origin='https://download.example.test')
        self.deliveries = DeliveryStore(self.database, recover_interrupted=False)
        self.addCleanup(self.deliveries.close)
        self.outbox = OfficialOutbox(self.deliveries, admission=self.admission, pepper=b'TEST'*8,
            clock=lambda:self.now, protected_payloads=self.payloads)
        self.fake = FileMediaFake()
        self.fake.app_id = self.account.app_id
        self.fake.clock = lambda:self.now
        self.fake.receipt_ttl = 1800
        await self.fake.start()
        self.tokens = TokenManager(Credentials(self.account.app_id,self.fake.app_secret), self.fake.base_url,
                                   allow_test_server=True)
        self.rest = RestClient(self.tokens, allow_test_server=True)
        self.sender = OfficialSender(self.account,self.token,self.outbox,self.rest,self.ledger,
            database_calls=self.calls,ensure_current=self.ensure_current,wall_clock=lambda:self.now)
        with self.tx() as cursor:
            observe_ready(cursor,self.token,{'op':0,'t':'READY','d':{'session_id':'TEST_FILE_READY',
                'user':{'id':'TEST_FILE_BOT','bot':True}}},self.now)
        self.window(source='TEST_INPUT')
        self.fake.add_window('group',self.endpoint.native_conversation_id,'TEST_INPUT')
        self.publisher_hook = None
        self.publisher_calls = 0
        self.dispatcher = self.make_dispatcher()

    async def asyncTearDown(self):
        await self.sender.stop()
        await self.calls.close()
        await self.rest.aclose()
        await self.tokens.aclose()
        await self.fake.close()

    def policy(self,cursor,artifact,route):
        return self.native_allowed if route=='native' else self.link_allowed

    async def ensure_current(self):
        def check():
            with self.tx() as cursor: assert_epoch(cursor,self.token)
        await self.calls(check)

    @asynccontextmanager
    async def unused_download(self,artifact):
        raise AssertionError('Dispatcher must not fetch its own download')
        yield

    def publisher_factory(self,authority):
        store=PgPublicationStore(transaction_factory=self.tx,calls=self.calls,clock=lambda:self.now,
                                 publication_authority=authority)
        publisher=FileLinkPublisher(store=store,vault=self.link_vault,open_artifact=self.unused_download,
            origin='https://download.example.test',clock=lambda:self.now)
        original=publisher.publish
        async def publish(*args,**kwargs):
            self.publisher_calls+=1
            if self.publisher_hook: return await self.publisher_hook(original,*args,**kwargs)
            return await original(*args,**kwargs)
        publisher.publish=publish
        return publisher

    def make_dispatcher(self,**kwargs):
        options=dict(calls=self.calls,lease_check=lambda:self.local_current,
            publisher_factory=self.publisher_factory,check_delivery_ready=lambda *_:self.ready,clock=lambda:self.now)
        options.update(kwargs)
        return OfficialFileDispatcher(self.token,self.files,self.payloads,self.outbox,self.rest,**options)

    def prepare_file(self,**kwargs):
        self.item=self.put(**kwargs)
        turn=self.query('SELECT journal_turn_id FROM qq_official_turn_executions')[0]['journal_turn_id']
        self.result(self.handle,turn_id=turn)
        self.jobs.mark_succeeded(self.job.job_id,self.job.lease_owner,now=self.now,
                                 lease_generation=self.job.lease_generation)
        return self.item

    def claim_file(self):
        return self.jobs.claim_due('TEST_FILE_DISPATCH',kinds=(file_job_kind(self.account),),now=self.now)[0]

    def file_row(self):
        return self.query('SELECT * FROM qq_official_file_deliveries WHERE artifact_id=?',(self.item.artifact_id,))[0]

    def stages(self):
        return self.query('SELECT * FROM qq_official_file_stage_attempts')

    async def send_result(self):
        delivery=self.query('SELECT delivery_id FROM qq_official_file_message_plans')[0]['delivery_id']
        result=await self.sender.send_delivery(delivery)
        self.assertEqual(result.status,'committed')
        return self.fake.received[-1]['payload']

    async def test_real_worker_small_upload_handoff_m3_send_and_redacted_receipt(self):
        self.prepare_file()
        worker=DurableJobWorker(self.jobs,logger=SimpleNamespace(error=lambda *_:None,warning=lambda *_:None),
                               database_calls=self.calls)
        worker.register(file_job_kind(self.account),self.dispatcher)
        with patch('src.plugins.ai_chat.storage.jobs.time',SimpleNamespace(time=lambda:self.now)):
            self.assertEqual(await worker.run_once(),1)
        self.assertEqual(self.file_row()['state'],'handed_off')
        self.assertEqual(len(self.fake.media_calls),1)
        self.assertEqual(self.fake.received,[])
        wire=await self.send_result()
        self.assertEqual(wire['msg_type'],7)
        self.assertEqual(wire['media']['file_info'],'TEST_FILE_INFO')
        self.assertEqual(self.file_row()['state'],'message_accepted')
        self.assertEqual(self.stages()[0]['state'],'confirmed')
        stored=json.dumps([dict(row) for row in self.query('SELECT * FROM delivery_attempts')])
        self.assertNotIn('TEST_FILE_INFO',stored)
        self.assertEqual(self.query("SELECT status FROM durable_jobs WHERE kind=?",(file_job_kind(self.account),))[0]['status'],'succeeded')

    async def test_upload_unknown_is_never_retried_or_replaced_by_link(self):
        self.prepare_file();job=self.claim_file();self.fake.drop_response=True
        self.assertEqual((await self.dispatcher(job))['status'],'upload_unknown')
        self.fake.drop_response=False
        self.assertEqual((await self.dispatcher(job))['status'],'upload_unknown')
        self.assertEqual(len(self.fake.media_calls),1)
        self.assertEqual(self.publisher_calls,0)
        self.assertEqual(self.stages()[0]['state'],'unknown')
        self.assertFalse(self.query('SELECT * FROM delivery_plans'))

    async def test_fsync_evidence_recovers_after_commit_boundary_without_second_upload(self):
        self.prepare_file();job=self.claim_file();original=self.native_vault.save
        def interrupted(*args):
            original(*args)
            raise RuntimeError('TEST private failure after fsync')
        with patch.object(self.native_vault,'save',side_effect=interrupted), self.assertRaises(FileDispatchUnavailable):
            await self.dispatcher(job)
        self.assertEqual(self.stages()[0]['state'],'dispatched')
        self.assertEqual((await self.make_dispatcher()(job))['status'],'handed_off')
        self.assertEqual(len(self.fake.media_calls),1)
        self.assertEqual(self.stages()[0]['state'],'confirmed')

    async def test_cancelled_upload_keeps_intent_and_reclaim_cannot_resend(self):
        self.prepare_file();job=self.claim_file();self.fake.files_release=asyncio.Event()
        operation=asyncio.create_task(self.dispatcher(job))
        await asyncio.wait_for(self.fake.files_seen.wait(),3)
        operation.cancel()
        with self.assertRaises(asyncio.CancelledError): await operation
        self.fake.files_release.set()
        self.now+=301
        replacement=self.claim_file()
        self.assertEqual((await self.dispatcher(replacement))['status'],'upload_unknown')
        self.assertEqual(len(self.fake.media_calls),1)

    async def test_revoked_source_before_auth_makes_zero_transport_calls(self):
        self.prepare_file();job=self.claim_file();self.admitted=False
        self.assertEqual((await self.dispatcher(job))['status'],'cancelled')
        self.assertEqual(self.fake.token_calls,0)
        self.assertEqual(self.fake.media_calls,[])

    async def test_revoke_during_token_wait_prevents_upload(self):
        self.prepare_file();job=self.claim_file();self.fake.token_release=asyncio.Event()
        operation=asyncio.create_task(self.dispatcher(job))
        await asyncio.wait_for(self.fake.token_seen.wait(),3)
        self.admitted=False;self.fake.token_release.set()
        self.assertEqual((await operation)['status'],'cancelled')
        self.assertEqual(self.fake.media_calls,[])

    async def test_link_with_actual_publication_then_m3_draft_reason(self):
        self.native_allowed=False
        self.prepare_file(draft=True,draft_reason='TEST unfinished tables')
        job=self.claim_file()
        self.assertEqual((await self.dispatcher(job))['status'],'handed_off')
        row=self.file_row()
        self.assertEqual(row['publication_expires_at'],row['publication_started_at']+3600)
        wire=await self.send_result()
        self.assertIn('未完成部分：TEST unfinished tables',wire['content'])
        self.assertIn('https://download.example.test/files/',wire['content'])
        self.assertEqual(self.fake.media_calls,[])
        self.assertEqual(self.publisher_calls,1)
        self.assertEqual((await self.dispatcher(job))['status'],'message_accepted')
        self.assertEqual(self.publisher_calls,1)

    async def test_link_default_ready_denies_before_token_or_publication(self):
        self.native_allowed=False;self.prepare_file();job=self.claim_file()
        dispatcher=self.make_dispatcher(check_delivery_ready=None)
        with self.assertRaises(JobDeferred): await dispatcher(job)
        self.assertIsNone(self.file_row()['publication_request_key'])
        self.assertEqual(self.publisher_calls,0)
        self.assertEqual(self.fake.token_calls,0)

    async def test_link_cancel_after_commit_restores_exact_url_not_resign(self):
        self.native_allowed=False;self.prepare_file();job=self.claim_file()
        async def cancel_after(original,*args,**kwargs):
            await original(*args,**kwargs)
            raise asyncio.CancelledError
        self.publisher_hook=cancel_after
        with self.assertRaises(asyncio.CancelledError): await self.dispatcher(job)
        initial=dict(self.query('SELECT * FROM qq_official_file_publications')[0])
        self.publisher_hook=None
        self.assertEqual((await self.make_dispatcher()(job))['status'],'handed_off')
        self.assertEqual(dict(self.query('SELECT * FROM qq_official_file_publications')[0]),initial)
        self.assertEqual(self.publisher_calls,1)

    async def test_link_unknown_without_row_never_generates_replacement(self):
        self.native_allowed=False;self.prepare_file();job=self.claim_file()
        async def unknown(*args,**kwargs): raise RuntimeError('TEST uncertain publication')
        self.publisher_hook=unknown
        self.assertEqual((await self.dispatcher(job))['status'],'publication_unknown')
        self.publisher_hook=None
        self.assertEqual((await self.dispatcher(job))['status'],'publication_unknown')
        self.assertEqual(self.publisher_calls,1)
        self.assertFalse(self.query('SELECT * FROM qq_official_file_publications'))

    async def test_native_draft_is_denied_even_when_native_policy_is_true(self):
        self.link_allowed=False
        self.prepare_file(draft=True,draft_reason='TEST missing appendix')
        result=await self.dispatcher(self.claim_file())
        self.assertEqual(result['reason'],'native_draft_details_unavailable')
        self.assertEqual(self.fake.media_calls,[])
        self.assertEqual(self.publisher_calls,0)

    async def test_large_native_without_link_has_explicit_non_network_outcome(self):
        self.link_allowed=False
        content=b'%PDF-1.4\n'+b'TEST'*900000
        self.item=self.files.register(self.source,content=content,filename='large.pdf',logical_request_key='a'*64)
        result=await self.dispatcher(self.claim_file())
        self.assertEqual(result['reason'],'large_upload_unavailable')
        self.assertEqual(self.fake.token_calls,0)
        self.assertFalse(self.stages())

    async def test_late_upload_evidence_survives_source_revoke_without_handoff(self):
        self.prepare_file();job=self.claim_file();original=self.native_vault.save
        def revoke_after_receipt(*args):
            reference=original(*args)
            self.admitted=False
            return reference
        with patch.object(self.native_vault,'save',side_effect=revoke_after_receipt):
            self.assertEqual((await self.dispatcher(job))['status'],'cancelled')
        self.assertEqual(self.stages()[0]['state'],'confirmed')
        self.assertIsNotNone(self.stages()[0]['receipt_commitment'])
        self.assertFalse(self.query('SELECT * FROM delivery_plans'))
        self.assertEqual(len(self.fake.media_calls),1)

    async def test_stale_job_cannot_auth_or_reuse_new_owner_authority(self):
        self.prepare_file();old=self.claim_file();self.now+=301;current=self.claim_file()
        self.assertEqual((await self.dispatcher(old))['status'],'needs_attention')
        self.assertEqual(self.fake.token_calls,0)
        self.assertEqual((await self.dispatcher(current))['status'],'handed_off')
        self.assertEqual(len(self.fake.media_calls),1)

    async def test_unknown_stage_commitment_swap_never_restores_or_reuploads(self):
        self.prepare_file();job=self.claim_file();original=self.native_vault.save
        def interrupted(*args):
            original(*args)
            raise RuntimeError('TEST crash after vault')
        with patch.object(self.native_vault,'save',side_effect=interrupted), self.assertRaises(FileDispatchUnavailable):
            await self.dispatcher(job)
        self.query("UPDATE qq_official_file_stage_attempts SET request_commitment=?",('f'*64,))
        self.assertEqual((await self.make_dispatcher()(job))['status'],'upload_unknown')
        self.assertEqual(len(self.fake.media_calls),1)
        self.assertFalse(self.query('SELECT * FROM qq_official_file_credentials'))

    async def test_known_auth_rejection_is_single_stage_and_no_link_fallback(self):
        self.prepare_file();job=self.claim_file();self.fake.response_status=401
        self.fake.response_code=11244
        self.assertEqual((await self.dispatcher(job))['status'],'failed')
        self.assertEqual(self.stages()[0]['state'],'rejected')
        self.assertEqual(len(self.fake.media_calls),1)
        self.assertEqual(self.publisher_calls,0)

    async def test_link_publication_unknown_commit_recovers_existing_credential(self):
        self.native_allowed=False;self.prepare_file();job=self.claim_file()
        async def lost_return(original,*args,**kwargs):
            await original(*args,**kwargs)
            raise RuntimeError('TEST committed publication return lost')
        self.publisher_hook=lost_return
        self.assertEqual((await self.dispatcher(job))['status'],'handed_off')
        self.assertEqual(self.publisher_calls,1)
        self.assertEqual(len(self.query('SELECT * FROM qq_official_file_publications')),1)

    async def test_link_absolute_expiry_uses_shorter_retention(self):
        self.native_allowed=False;self.prepare_file();job=self.claim_file()
        self.query('UPDATE qq_official_file_artifacts SET retain_until=? WHERE artifact_id=?',
                   (self.now+10,self.item.artifact_id))
        self.assertEqual((await self.dispatcher(job))['status'],'handed_off')
        self.assertEqual(self.file_row()['publication_expires_at'],self.now+10)
        self.assertEqual(self.query('SELECT expires_at FROM delivery_plans')[0]['expires_at'],self.now+10)

    async def test_explicit_native_unsupported_falls_back_once_to_approved_link(self):
        self.prepare_file();job=self.claim_file()
        self.fake.response_status=400;self.fake.response_code=50015014
        self.assertEqual((await self.dispatcher(job))['status'],'handed_off')
        stage=self.stages()[0]
        self.assertEqual((stage['state'],stage['failure_code']),('rejected','native_file_unavailable_v1'))
        self.assertEqual(self.file_row()['route'],'link')
        self.assertEqual(self.publisher_calls,1)
        self.assertEqual(len(self.fake.media_calls),1)
        wire=await self.send_result()
        self.assertEqual(wire['msg_type'],0)
        self.assertIn('https://download.example.test/files/',wire['content'])

    async def test_native_rejection_restart_retains_stage_and_never_reuploads(self):
        self.prepare_file();job=self.claim_file();self.fake.response_code=50015014
        original=self.dispatcher._rejected_fallback
        def crash_after_rejection(cursor,grant):
            if cursor.execute("SELECT 1 FROM qq_official_file_stage_attempts WHERE state='rejected'").fetchone():
                raise RuntimeError('TEST crash after durable rejection')
            return original(cursor,grant)
        with patch.object(self.dispatcher,'_rejected_fallback',side_effect=crash_after_rejection), self.assertRaises(FileDispatchUnavailable):
            await self.dispatcher(job)
        self.assertEqual(self.stages()[0]['state'],'rejected')
        self.assertEqual(self.publisher_calls,0)
        self.assertEqual((await self.make_dispatcher()(job))['status'],'handed_off')
        self.assertEqual(len(self.fake.media_calls),1)
        self.assertEqual(self.publisher_calls,1)

    async def test_content_rejection_cannot_change_to_link_channel(self):
        self.prepare_file();job=self.claim_file()
        self.fake.response_status=400;self.fake.response_code=40034006
        self.assertEqual((await self.dispatcher(job))['status'],'failed')
        self.assertEqual(self.stages()[0]['failure_code'],'content_rejected')
        self.assertEqual(self.publisher_calls,0)
        self.assertFalse(self.query('SELECT * FROM delivery_plans'))

    async def test_unsupported_code_on_5xx_remains_unknown_without_fallback(self):
        self.prepare_file();job=self.claim_file()
        self.fake.response_status=503;self.fake.response_code=50015014
        self.assertEqual((await self.dispatcher(job))['status'],'upload_unknown')
        self.assertEqual(self.stages()[0]['state'],'unknown')
        self.assertEqual(self.publisher_calls,0)
        with self.tx() as cursor:
            self.assertFalse(native_rejected(cursor,self.endpoint,endpoint_generation=1,route_epoch=1,verified_at=0))

    async def test_error_plus_message_receipt_is_unknown_and_cannot_change_channel(self):
        self.prepare_file();job=self.claim_file()
        self.fake.response_code=50015014;self.fake.implicit_id='TEST_CONTRADICTORY_MESSAGE'
        self.assertEqual((await self.dispatcher(job))['status'],'upload_unknown')
        self.assertEqual(self.stages()[0]['state'],'unknown')
        self.assertEqual(self.stages()[0]['failure_code'],'unexpected_upload_message_evidence')
        self.assertEqual(self.publisher_calls,0)
        self.assertEqual(len(self.fake.media_calls),1)
        self.assertFalse(self.query('SELECT * FROM delivery_plans'))

    async def test_persisted_native_rejection_has_exact_namespace_version_and_evidence_cutoff(self):
        self.prepare_file();job=self.claim_file();self.fake.response_code=50015014
        await self.dispatcher(job)
        def check(endpoint=None,**kwargs):
            options=dict(endpoint_generation=1,route_epoch=1,verified_at=self.now)
            options.update(kwargs)
            with self.tx() as cursor:
                return native_rejected(cursor,endpoint or self.endpoint,**options)
        self.assertTrue(check())
        self.assertFalse(check(verified_at=self.now+1))
        self.assertFalse(check(endpoint_generation=2))
        self.assertFalse(check(route_epoch=2))
        self.assertFalse(check(file_type=1))
        self.assertFalse(check(contract='TEST_FUTURE_PROTOCOL'))
        for endpoint in (
            EndpointKey(AccountKey('qq-official','TEST_OTHER_REJECTION','test'),'group',self.endpoint.native_conversation_id),
            EndpointKey(AccountKey('qq-official',self.account.app_id,'sandbox'),'group',self.endpoint.native_conversation_id),
            EndpointKey(self.account,'private',self.endpoint.native_conversation_id),
            EndpointKey(self.account,'group','TEST_OTHER_GROUP'),
        ):
            self.assertFalse(check(endpoint))
        with patch('src.plugins.ai_chat.adapters.qq_official.file_dispatcher.ERROR_CONTRACT','TEST_FUTURE_ERROR_CONTRACT'):
            self.assertFalse(check())
        with self.assertRaises(ValueError): check(verified_at=True)

    async def test_float_wall_clock_is_supported_and_never_rewinds(self):
        self.prepare_file();job=self.claim_file()
        dispatcher=self.make_dispatcher(clock=lambda:float(self.now)+0.75)
        self.assertEqual((await dispatcher(job))['status'],'handed_off')
        first=dispatcher._now()
        dispatcher.clock=lambda:float(first)-10.5
        self.assertEqual(dispatcher._now(),first)
        self.assertEqual(len(self.fake.media_calls),1)

    async def test_invalid_wall_clocks_cannot_start_auth_or_stages(self):
        self.prepare_file();job=self.claim_file()
        for value in (float('nan'),float('inf'),float('-inf'),True,False,-1,2**63,'1800000000'):
            with self.subTest(value=value):
                dispatcher=self.make_dispatcher(clock=lambda:value)
                with self.assertRaisesRegex(ValueError,'invalid_file_dispatch_clock'):
                    dispatcher._now()
                with self.assertRaises(FileDispatchUnavailable):
                    await dispatcher(job)
        self.assertEqual(self.fake.token_calls,0)
        self.assertEqual(self.fake.media_calls,[])
        self.assertFalse(self.stages())
