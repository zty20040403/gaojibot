"""Exact host evidence -> existing file job -> real TLS PUT -> native M3 receipt."""
import asyncio
from dataclasses import replace
import hashlib
from urllib.parse import urlsplit
from unittest.mock import patch

import httpx2

from src.bot_storage.database import DatabaseError

from src.plugins.ai_chat.adapters.qq_official import execution
from src.plugins.ai_chat.adapters.qq_official.file_artifacts import foreground_source,file_job_kind
from src.plugins.ai_chat.adapters.qq_official.native_chunk_dispatch import MultipartDispatcher
from src.plugins.ai_chat.adapters.qq_official.native_media import CONTRACT
from src.plugins.ai_chat.adapters.qq_official.native_upload import OfficialMediaUploader
from src.plugins.ai_chat.adapters.qq_official.native_session_vault import NativeSessionVault
from src.plugins.ai_chat.adapters.qq_official.project_authority import RecipientEvidenceUnavailable
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.message_ir import MessageBody,TextNode
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred
from tests.test_qq_official_host_composition import HostCompositionFixture
from tests.qq_official_download_fixture import TLSProxy,ORIGIN

CONTENT=b'%PDF-1.4\n'+b'x'*4_000_000+b'\n%%EOF\n'


class NativeChunksTests(HostCompositionFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        row=self.query('SELECT generation,route_epoch,capability_revision FROM qq_official_endpoints WHERE endpoint_key=?',
            (self.endpoint.key,))[0]
        self.raw['files']['native_routes']=[dict(endpoint=self.endpoint.key,endpoint_generation=row['generation'],
            route_epoch=row['route_epoch'],capability_revision=row['capability_revision'],max_bytes=8_000_000,
            verified_at=self.now,expires_at=self.now+1000,contract=CONTRACT,file_type=4,multipart=True)]
        self.raw['files']['multipart']=dict(allowed_part_hosts=['download.example.test'],max_parts=8,max_session_seconds=120)
        await self.build()
        self.outbox.protected_payloads=self.files.payloads
        self.fake.block=2_000_000;self.fake.receipt_ttl=1800
        self.proxy=await TLSProxy(self.temp.name,urlsplit(self.fake.base_url).port).start()
        count=(len(CONTENT)+self.fake.block-1)//self.fake.block
        self.fake.prepare_response_override=dict(upload_id='TEST_UPLOAD',block_size=str(self.fake.block),
            parts=[dict(index=i,block_size=str(min(self.fake.block,len(CONTENT)-i*self.fake.block)),
                presigned_url=ORIGIN+f'/put/{i}?signed=TEST_SIGNED_SECRET') for i in range(count)],
            upload_config=dict(concurrency=2,retry_timeout=120,retry_delay=1))
        async def resolver(host,port):
            self.assertEqual((host,port),('download.example.test',443));return ('93.184.216.34',)
        proxy=self.proxy
        def factory():
            inner=httpx2.AsyncHTTPTransport(verify=proxy.trust,trust_env=False,retries=0)
            class LoopbackMapping(httpx2.AsyncBaseTransport):
                async def handle_async_request(self,request):
                    # Only fake transport address is replaced; production DNS,
                    # source recheck, SNI, TLS/headers and permit are unchanged.
                    return await inner.handle_async_request(httpx2.Request(request.method,
                        request.url.copy_with(host='127.0.0.1',port=proxy.port),headers=request.headers,
                        stream=request.stream,extensions=request.extensions))
                async def aclose(self):await inner.aclose()
            return LoopbackMapping()
        self.part=self.files.dispatcher.multipart.transport
        self.part.resolver,self.part.transport_factory=resolver,factory

    async def asyncTearDown(self):
        if getattr(self,'proxy',None) is not None:await self.proxy.close()
        await super().asyncTearDown()

    async def prepare(self):
        job=self.jobs.claim_due('TEST_CHUNK_SOURCE',kinds=(account_job_kind('turn',self.account.key),),now=self.now)[0]
        admission=self.runtime.store.admitted
        with self.tx() as c:
            handle=execution.claim_execution(c,token=self.token,job=job,now=self.now,deadline_at=self.now+200,
                admission=admission).handle
        turn=self.journal.start_turn(self.endpoint.scope,trigger_canonical_message_id=self.ingested.canonical_message_id,
            objective='TEST chunks',provider='test',model='test',started_at=self.now)
        args=dict(token=self.token,handle=handle,job=job,now=self.now,admission=admission)
        with self.tx() as c:
            execution.begin_execution(c,**args,journal_turn_id=turn.turn_id)
            source=foreground_source(c,**args,pending_deadline_at=self.now+1000)
        item=self.files.artifacts.register(source,content=CONTENT,filename='report.pdf',logical_request_key=hashlib.sha256(b'TEST_CHUNKS').hexdigest())
        with self.tx() as c:
            execution.capture_result(c,**args,body=MessageBody((TextNode(0,'TEST ready'),)),turn_id=turn.turn_id,status='succeeded')
        self.jobs.mark_succeeded(job.job_id,job.lease_owner,now=self.now,lease_generation=job.lease_generation)
        job=self.jobs.claim_due('TEST_CHUNK_FILE',kinds=(file_job_kind(self.account),),now=self.now)[0]
        return item,job

    def stages(self):return self.query('SELECT * FROM qq_official_file_stage_attempts ORDER BY created_at,stage_attempt_id')
    def put_count(self):return sum(method=='PUT' for method,_ in self.proxy.requests)
    def stage_calls(self,operation):return sum(row[2].endswith('/'+operation) for row in self.fake.media_calls)

    async def send(self):
        delivery=self.query('SELECT delivery_id FROM qq_official_file_message_plans')[0]['delivery_id']
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertEqual(self.fake.received[-1]['payload']['msg_type'],7)

    async def test_real_protected_factory_tls_multipart_merge_then_one_m3_send(self):
        item,job=await self.prepare()
        self.assertEqual((await self.files.dispatcher(job))['status'],'handed_off')
        self.assertEqual(self.fake.uploaded[('groups',self.endpoint.native_conversation_id)],CONTENT)
        self.assertEqual(self.stage_calls('upload_prepare'),1);self.assertEqual(self.stage_calls('files'),1)
        self.assertEqual(self.put_count(),3);self.assertEqual(self.stage_calls('upload_part_finish'),3)
        self.assertTrue(all(row['state']=='confirmed' for row in self.stages()))
        self.assertEqual(len(self.query('SELECT * FROM qq_official_file_upload_sessions')),1)
        self.assertNotIn('TEST_SIGNED_SECRET',str(self.query('SELECT * FROM qq_official_file_upload_sessions')))
        import psycopg
        for statement in ('UPDATE qq_official_file_upload_sessions SET expires_at=expires_at+1',
                          'DELETE FROM qq_official_file_upload_sessions'):
            with self.assertRaises(DatabaseError) as error:self.query(statement)
            self.assertIsInstance(error.exception.__cause__,psycopg.errors.RaiseException)
        await self.send();self.assertEqual(len(self.fake.received),1)
        row=self.query('SELECT state FROM qq_official_file_deliveries WHERE artifact_id=?',(item.artifact_id,))[0]
        self.assertEqual(row['state'],'message_accepted')

    async def test_confirmed_put_resumes_same_session_after_interruption_without_reput(self):
        _,job=await self.prepare()
        async def interrupted(*args,**kwargs):raise asyncio.CancelledError()
        with patch.object(OfficialMediaUploader,'finish_part',side_effect=interrupted):
            with self.assertRaises(asyncio.CancelledError):await self.files.dispatcher(job)
        self.assertEqual(self.put_count(),1)
        original=self.query('SELECT * FROM qq_official_file_upload_sessions')[0]
        self.files.session_vault.close()
        self.files.session_vault=NativeSessionVault(self.files.directory/'upload-sessions',clock=lambda:self.now)
        self.files.dispatcher.multipart=MultipartDispatcher(self.files.dispatcher,self.files.config.multipart_configuration,
            self.files.session_vault,self.part)
        # Existing durable job is reclaimed with a new generation; source and
        # original session/deadline remain fixed, no synthetic fresh admission.
        self.query('UPDATE durable_jobs SET lease_until=? WHERE job_id=?',(self.now-1,job.job_id))
        newer=self.jobs.claim_due('TEST_CHUNK_RESTART',kinds=(file_job_kind(self.account),),now=self.now)[0]
        self.assertGreater(newer.lease_generation,job.lease_generation)
        self.assertEqual((await self.files.dispatcher(newer))['status'],'handed_off')
        self.assertEqual(self.put_count(),3);self.assertEqual(self.stage_calls('upload_prepare'),1)
        self.assertEqual(self.query('SELECT * FROM qq_official_file_upload_sessions')[0],original)
        await self.send()

    async def test_unknown_put_never_restarts_session_or_falls_back(self):
        _,job=await self.prepare();self.fake.put_drop=True
        self.assertEqual((await self.files.dispatcher(job))['status'],'upload_unknown')
        self.fake.put_drop=False
        self.assertEqual((await self.files.dispatcher(job))['status'],'upload_unknown')
        self.assertEqual(self.put_count(),1);self.assertEqual(self.stage_calls('upload_prepare'),1)
        self.assertEqual(self.stage_calls('upload_part_finish'),0);self.assertEqual(self.stage_calls('files'),0)
        self.assertFalse(self.query('SELECT * FROM qq_official_file_publications'))
        self.assertFalse(self.fake.received)

    async def test_final_file_info_restores_after_prepare_expired_and_private_session_collected(self):
        _,job=await self.prepare()
        with patch.object(self.files.dispatcher,'_handoff_native',side_effect=RecipientEvidenceUnavailable):
            with self.assertRaises(JobDeferred):await self.files.dispatcher(job)
        self.assertEqual(self.stage_calls('files'),1)
        self.now+=121
        self.assertEqual(self.files.session_vault.collect_expired()['removed'],1)
        self.assertEqual((await self.files.dispatcher(job))['status'],'handed_off')
        self.assertEqual(self.stage_calls('upload_prepare'),1);self.assertEqual(self.put_count(),3);self.assertEqual(self.stage_calls('files'),1)
        await self.send()

    async def test_endpoint_revoked_during_dns_recheck_blocks_first_physical_put(self):
        _,job=await self.prepare()
        original=self.part.resolver
        async def revoked(*args):
            self.query("UPDATE qq_official_endpoints SET binding_state='revoked' WHERE endpoint_key=?",(self.endpoint.key,))
            return await original(*args)
        self.part.resolver=revoked
        result=await self.files.dispatcher(job)
        self.assertIn(result['status'],{'failed','cancelled'})
        self.assertEqual(self.put_count(),0);self.assertEqual(self.stage_calls('upload_prepare'),1)
        self.assertFalse(self.fake.received)
        puts=[row for row in self.stages() if row['operation']=='upload_put']
        self.assertEqual(len(puts),1);self.assertEqual(puts[0]['state'],'unsent')

    async def test_old_small_only_target_evidence_does_not_upgrade_from_host_configuration(self):
        _,job=await self.prepare()
        key=(self.endpoint.key,4)
        self.host.evidence[key]=replace(self.host.evidence[key],multipart=False)
        with self.assertRaises(JobDeferred):await self.files.dispatcher(job)
        self.assertFalse(self.stages());self.assertFalse(self.fake.media_calls);self.assertEqual(self.put_count(),0)

    async def test_unknown_merge_keeps_original_stage_no_new_session_or_m3(self):
        _,job=await self.prepare();self.fake.drop_response=True
        self.assertEqual((await self.files.dispatcher(job))['status'],'upload_unknown')
        self.fake.drop_response=False
        self.assertEqual((await self.files.dispatcher(job))['status'],'upload_unknown')
        self.assertEqual(self.stage_calls('upload_prepare'),1);self.assertEqual(self.put_count(),3)
        self.assertEqual(self.stage_calls('files'),1);self.assertFalse(self.fake.received)
        self.assertFalse(self.query('SELECT * FROM qq_official_file_publications'))

    async def test_expired_partial_session_never_allocates_replacement(self):
        _,job=await self.prepare()
        async def interrupted(*args,**kwargs):raise asyncio.CancelledError()
        with patch.object(OfficialMediaUploader,'finish_part',side_effect=interrupted):
            with self.assertRaises(asyncio.CancelledError):await self.files.dispatcher(job)
        self.now+=121
        self.files.session_vault.collect_expired()
        self.assertEqual((await self.files.dispatcher(job))['status'],'upload_unknown')
        self.assertEqual(self.stage_calls('upload_prepare'),1);self.assertEqual(self.put_count(),1)
        self.assertEqual(self.stage_calls('files'),0);self.assertFalse(self.fake.received)

    async def test_cancel_after_physical_put_remains_unresolved_never_reput(self):
        _,job=await self.prepare();original=self.part.put
        async def lost_result(*args,**kwargs):
            await original(*args,**kwargs)
            raise asyncio.CancelledError()
        with patch.object(self.part,'put',side_effect=lost_result):
            with self.assertRaises(asyncio.CancelledError):await self.files.dispatcher(job)
        self.assertEqual(self.put_count(),1)
        self.assertEqual((await self.files.dispatcher(job))['status'],'upload_unknown')
        self.assertEqual(self.put_count(),1);self.assertEqual(self.stage_calls('upload_part_finish'),0)
        self.assertEqual(self.stage_calls('files'),0);self.assertFalse(self.fake.received)

    async def test_job_lease_loss_during_dns_retains_unsent_historical_evidence(self):
        _,job=await self.prepare();original=self.part.resolver
        async def lost(*args):
            self.query('UPDATE durable_jobs SET lease_generation=lease_generation+1 WHERE job_id=?',(job.job_id,))
            return await original(*args)
        self.part.resolver=lost
        self.assertEqual((await self.files.dispatcher(job))['status'],'needs_attention')
        row=[s for s in self.stages() if s['operation']=='upload_put'][0]
        self.assertEqual((row['state'],row['failure_code']),('unsent','upload_source_unavailable'))
        self.assertEqual(self.put_count(),0);self.assertEqual(self.stage_calls('files'),0)
        self.assertFalse(self.fake.received)

    async def test_session_requires_exact_prepare_receipt_and_is_immutable(self):
        import psycopg
        item,job=await self.prepare()
        self.query("""INSERT INTO qq_official_file_stage_attempts
            (stage_attempt_id,artifact_id,operation,part_index,request_commitment,job_id,job_lease_generation,
             account_epoch,state,created_at,expires_at,receipt_commitment,settled_at)
            VALUES (?,?, 'upload_put',0,?,?,?,?, 'confirmed',?,?,?,?)""",
            ('a'*32,item.artifact_id,'b'*64,job.job_id,job.lease_generation,self.token.epoch,self.now,self.now+60,'c'*64,self.now))
        with self.assertRaises(DatabaseError) as error:
            self.query("""INSERT INTO qq_official_file_upload_sessions
                (artifact_id,prepare_stage_attempt_id,credential_ref,binding_digest,prepared_at,expires_at,part_count,contract)
                VALUES (?,?,?,?,?,?,1,?)""",(item.artifact_id,'a'*32,'d'*32,'c'*64,self.now,self.now+60,CONTRACT))
        self.assertIsInstance(error.exception.__cause__,psycopg.errors.ForeignKeyViolation)
        # The fixed prepare operation cannot be overridden to borrow a PUT.
        with self.assertRaises(DatabaseError) as error:
            self.query("""INSERT INTO qq_official_file_upload_sessions
                (artifact_id,prepare_stage_attempt_id,prepare_operation,credential_ref,binding_digest,prepared_at,expires_at,part_count,contract)
                VALUES (?,?,'upload_put',?,?,?,?,1,?)""",(item.artifact_id,'a'*32,'d'*32,'c'*64,self.now,self.now+60,CONTRACT))
        self.assertIsInstance(error.exception.__cause__,psycopg.errors.CheckViolation)
