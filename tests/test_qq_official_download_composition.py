"""Protected host JSON -> real M6/M3/PG -> actual loopback TLS download.

Test-only trust maps the fixed synthetic public origin to a local TLS proxy.
No QQ credential, public service, recipient accessibility or domain approval is
implied by these loopback tests.
"""
import asyncio
import hashlib
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official import execution
from src.plugins.ai_chat.adapters.qq_official.download_runtime import DownloadHost,CONTRACT
from src.plugins.ai_chat.adapters.qq_official.file_artifacts import foreground_source,file_job_kind
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.message_ir import MessageBody,TextNode
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred
from tests.test_qq_official_host_composition import HostCompositionFixture
from tests.qq_official_file_fixture import PDF
from tests.qq_official_download_fixture import TLSProxy,ORIGIN,free_port


class DownloadCompositionTests(HostCompositionFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        port=free_port();self.proxy=await TLSProxy(self.temp.name,port).start()
        row=self.query('SELECT generation,route_epoch,capability_revision FROM qq_official_endpoints WHERE endpoint_key=?',
            (self.endpoint.key,))[0]
        self.raw['files']['download']=dict(origin=ORIGIN,listen_host='127.0.0.1',listen_port=port,
            routes=[dict(endpoint=self.endpoint.key,endpoint_generation=row['generation'],route_epoch=row['route_epoch'],
                capability_revision=row['capability_revision'],verified_at=self.now,expires_at=self.now+100,contract=CONTRACT)])
        cls=DownloadHost
        # Only networking clock/trust is injected. All services, factories,
        # account/source authority, persistence and send paths remain real.
        with patch('src.plugins.ai_chat.adapters.qq_official.download_runtime.DownloadHost',
                side_effect=lambda config,account:cls(config,account,probe=self.proxy.probe,clock=lambda:self.now)):
            await self.build(file_link_ttl_seconds=120)
        # OfficialRuntime installs this exact port after its awaited factory.
        self.outbox.protected_payloads=self.files.payloads
        await asyncio.sleep(.02)
        self.assertTrue(await self.host.download_host.check())
        self.assertEqual(self.files.config.link_ttl_seconds,120)
        self.assertEqual(self.files.dispatcher.link_ttl_seconds,120)

    async def asyncTearDown(self):
        if getattr(self,'host',None) is not None:await self.host.close()
        await self.proxy.close()
        await super().asyncTearDown()

    async def prepare(self):
        job=self.jobs.claim_due('TEST_DOWNLOAD_SOURCE',kinds=(account_job_kind('turn',self.account.key),),now=self.now)[0]
        admission=self.runtime.store.admitted
        with self.tx() as c:
            handle=execution.claim_execution(c,token=self.token,job=job,now=self.now,deadline_at=self.now+200,
                admission=admission).handle
        turn=self.journal.start_turn(self.endpoint.scope,trigger_canonical_message_id=self.ingested.canonical_message_id,
            objective='TEST file',provider='test',model='test',started_at=self.now)
        args=dict(token=self.token,handle=handle,job=job,now=self.now,admission=admission)
        with self.tx() as c:
            execution.begin_execution(c,**args,journal_turn_id=turn.turn_id)
            source=foreground_source(c,**args,pending_deadline_at=self.now+1000)
        item=self.files.artifacts.register(source,content=PDF,filename='report.pdf',
            logical_request_key=hashlib.sha256(b'TEST_DOWNLOAD').hexdigest())
        with self.tx() as c:
            execution.capture_result(c,**args,body=MessageBody((TextNode(0,'TEST created'),)),turn_id=turn.turn_id,status='succeeded')
        self.jobs.mark_succeeded(job.job_id,job.lease_owner,now=self.now,lease_generation=job.lease_generation)
        file_job=self.jobs.claim_due('TEST_DOWNLOAD_FILE',kinds=(file_job_kind(self.account),),now=self.now)[0]
        return item,file_job

    async def publish(self):
        item,job=await self.prepare()
        self.assertEqual((await self.files.dispatcher(job))['status'],'handed_off')
        pub=self.query('SELECT * FROM qq_official_file_publications')[0]
        published=await self.files._maintenance_publisher.restore(pub['credential_ref'])
        delivery=self.query('SELECT delivery_id FROM qq_official_file_message_plans')[0]['delivery_id']
        return item,job,pub,published,delivery

    async def test_actual_protected_factory_m3_receipt_head_get_and_current_source_revocation(self):
        item,job,pub,published,delivery=await self.publish()
        outcome=await self.sender.send_delivery(delivery)
        self.assertEqual(outcome.status,'committed')
        self.assertIn(published.url,self.fake.received[-1]['payload']['content'])
        self.assertEqual(self.fake.media_calls,[])
        self.assertNotIn(published.url,str(self.query('SELECT body_json,raw_event_json FROM messages')))
        self.assertEqual(self.query('SELECT state FROM qq_official_file_deliveries')[0]['state'],'message_accepted')
        self.assertEqual((await self.proxy.request('HEAD',published.url))[0],200)
        self.assertEqual(self.query('SELECT served_complete_count FROM qq_official_file_publications')[0]['served_complete_count'],0)
        status,_,body=await self.proxy.request('GET',published.url)
        self.assertEqual((status,body),(200,PDF))
        self.assertEqual(hashlib.sha256(body).hexdigest(),item.sha256)
        async with asyncio.timeout(2):
            while self.files.download_publisher._active:await asyncio.sleep(.01)
        self.assertEqual(self.query('SELECT served_complete_count FROM qq_official_file_publications')[0]['served_complete_count'],1)
        # Evidence expiry stops new sends, not the original already-published
        # URL. Current authority, publication expiry and byte retention remain.
        self.now+=101;self.proxy.wrong=True
        self.assertFalse(await self.host.download_host.check())
        self.assertEqual((await self.proxy.request('GET',published.url))[2],PDF)
        self.assertEqual(self.query('SELECT expires_at FROM qq_official_file_publications')[0]['expires_at'],pub['expires_at'])
        self.query("UPDATE qq_official_endpoints SET binding_state='revoked' WHERE endpoint_key=?",(self.endpoint.key,))
        self.assertEqual((await self.proxy.request('GET',published.url))[0],404)
        self.assertEqual(len(self.fake.received),1)

    async def test_new_link_uses_configured_ttl_and_reconstructed_dispatcher_keeps_original_expiry(self):
        from src.plugins.ai_chat.adapters.qq_official.file_dispatcher import OfficialFileDispatcher
        _,job,pub,published,delivery=await self.publish()
        self.assertEqual(pub['expires_at']-pub['created_at'],120)
        await self.sender.send_delivery(delivery)
        restarted=OfficialFileDispatcher(self.token,self.files.artifacts,self.files.payloads,self.outbox,self.rest,
            calls=self.calls,lease_check=self.files._local,publisher_factory=self.files.publisher_factory,
            check_delivery_ready=self.files.check_delivery_ready,clock=lambda:self.now,link_ttl_seconds=3600)
        await restarted(job)
        current=self.query('SELECT * FROM qq_official_file_publications')[0]
        self.assertEqual((current['publication_ref'],current['token_digest'],current['expires_at']),
            (pub['publication_ref'],pub['token_digest'],pub['expires_at']))
        self.now=pub['expires_at']
        self.assertEqual((await self.proxy.request('GET',published.url))[0],404)
        self.assertEqual(len(self.fake.received),1)

    async def test_unknown_message_does_not_republish_resign_or_resend(self):
        _,job,pub,published,delivery=await self.publish()
        self.fake.drop_next_response=True
        result=await self.sender.send_delivery(delivery)
        self.assertEqual(result.status,'ambiguous')
        self.fake.drop_next_response=False
        await self.sender.send_delivery(delivery)
        await self.files.dispatcher(job)
        after=self.query('SELECT * FROM qq_official_file_publications')
        self.assertEqual(len(after),1)
        self.assertEqual((after[0]['publication_ref'],after[0]['token_digest'],after[0]['expires_at']),
            (pub['publication_ref'],pub['token_digest'],pub['expires_at']))
        restored=await self.files._maintenance_publisher.restore(pub['credential_ref'])
        self.assertEqual(restored.url,published.url)
        self.assertEqual(len(self.fake.received),1)
        # Normal conversation/history never receives a bearer URL.
        messages=self.query('SELECT body_json FROM messages')
        self.assertNotIn(published.url,str(messages))

    async def test_wrong_target_evidence_and_probe_failure_create_no_publication_or_attempt(self):
        _,job=await self.prepare()
        self.proxy.wrong=True;self.assertFalse(await self.host.download_host.check())
        with self.assertRaises(JobDeferred):await self.files.dispatcher(job)
        self.assertFalse(self.query('SELECT * FROM qq_official_file_publications'))
        self.assertFalse(self.query('SELECT * FROM delivery_attempts'))
        self.assertFalse(self.fake.received)
        self.proxy.wrong=False;self.assertTrue(await self.host.download_host.check())
        self.query('UPDATE qq_official_endpoints SET capability_revision=capability_revision+1 WHERE endpoint_key=?',
            (self.endpoint.key,))
        with self.assertRaises(JobDeferred):await self.files.dispatcher(job)
        self.assertFalse(self.query('SELECT * FROM qq_official_file_publications'))
        self.assertFalse(self.fake.received)
