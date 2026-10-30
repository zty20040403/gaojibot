"""Real console source + original windows + distinct text/type7 receipts."""
import hashlib
from pathlib import Path
import secrets
import tempfile

from src.plugins.ai_chat.adapters.qq_official.authority import OwnerAuthority
from src.plugins.ai_chat.adapters.qq_official.binding import BindingContext,parse_binding_command
from src.plugins.ai_chat.adapters.qq_official import execution
from src.plugins.ai_chat.adapters.qq_official.file_artifacts import ArtifactStore,foreground_source,file_job_kind
from src.plugins.ai_chat.adapters.qq_official.file_dispatcher import OfficialFileDispatcher
from src.plugins.ai_chat.adapters.qq_official.file_links import FileCredentialVault
from src.plugins.ai_chat.adapters.qq_official.file_payloads import TrustedFilePayloads
from src.plugins.ai_chat.adapters.qq_official.native_vault import NativeUploadVault
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.ingest import process_inbox
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.adapters.qq_official.sender import OfficialSender
from src.plugins.ai_chat.message_ir import MessageBody,TextNode
from src.plugins.ai_chat.platform_types import ActorKey,EndpointKey
from src.plugins.ai_chat.project_blobs import ProjectBlobStore
from src.qq_official.credentials import Credentials
from src.qq_official.http import RestClient
from src.qq_official.token import TokenManager
from tests.test_qq_official_migration_services import MigrationServicesFixture
from tests.qq_official_file_http_fixture import FileMediaFake
from tests.qq_official_file_fixture import PDF


class VerificationHostFixture(MigrationServicesFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.temp=tempfile.TemporaryDirectory(prefix='TEST-verification-')
        self.addCleanup(self.temp.cleanup)
        private=EndpointKey(self.account,'private','TEST_VERIFY_OWNER')
        owner=ActorKey(self.account,'c2c_user','','TEST_VERIFY_OWNER')
        self.private,self.owner=private,owner
        self.provision(private)
        authority=OwnerAuthority(clock=lambda:self.now)
        def apply(c,endpoint,actor,text):
            return self.store.binding.apply(c,context=BindingContext(endpoint,actor,secrets.token_hex(16),self.now),
                command=parse_binding_command(text),lease_token=self.token)
        with self.tx() as c:
            authority.apply_trusted_config(c,self.account,(owner.native_user_id,),'TEST_CONFIG')
            c.execute("UPDATE qq_official_endpoints SET binding_state='unbound' WHERE endpoint_key=?",(self.endpoint.key,))
            issue=apply(c,private,owner,'/bind create')
            self.assertEqual(issue.status,'sending')
            self.store.binding.mark_issued(c,account=self.account,challenge_id=issue.challenge_id,outcome='success',lease_token=self.token)
            candidate=apply(c,self.endpoint,self.actor,f'/bind submit {issue.challenge_id} {issue.effect.code}')
            self.assertEqual(candidate.status,'pending')
            confirmed=apply(c,private,owner,f'/bind confirm {candidate.candidate_id} 1 --grant-owner')
            self.assertEqual(confirmed.status,'confirmed')
            observe_ready(c,self.token,{'op':0,'t':'READY','d':{'session_id':'TEST_ASSEMBLY',
                'user':{'id':'TEST_REAL_BOT','bot':True}}},self.now)
        self.accepted=self.accept()
        with self.tx() as c:
            self.ingested=process_inbox(c,inbox_id=self.accepted.inbox_id,lease_token=self.token,ledger=self.ledger,
                jobs=self.jobs,epoch_guard=assert_epoch,admission=self.store.admitted,now=self.now)
        self.fake=FileMediaFake();self.fake.app_id=self.account.app_id;self.fake.clock=lambda:self.now
        await self.fake.start()
        self.tokens=TokenManager(Credentials(self.account.app_id,self.fake.app_secret),self.fake.base_url,allow_test_server=True)
        self.rest=RestClient(self.tokens,allow_test_server=True)
        self.sender=OfficialSender(self.account,self.token,self.outbox,self.rest,self.ledger,
            database_calls=self.calls,ensure_current=self.ensure_current,wall_clock=lambda:self.now)
        self.runtime.sender=self.sender
        self.fake.add_window('group',self.endpoint.native_conversation_id,'TEST_ASSEMBLY_1')

    async def asyncTearDown(self):
        await self.sender.stop();await self.rest.aclose();await self.tokens.aclose();await self.fake.close()
        await super().asyncTearDown()

    async def ensure_current(self):
        def check():
            with self.tx() as c:assert_epoch(c,self.token)
        await self.calls(check)

    async def verifying(self):
        value=await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,action='plan',
            request=dict(source_endpoint=self.source.key,source_scope=self.source_scope.key,target_endpoint=self.endpoint.key,items=[],deadline=self.now+300))
        for action in ('freeze','drain','switch','verify'):
            await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,action=action,
                request={'migration_id':value['migration_id']},preview_digest=value['preview_digest'])
        self.migration_plan=value
        return value

    async def request(self,kind='text',artifact=None,**kwargs):
        return await self.session.request_verification(cookie=self.mapping_cookie,csrf=self.mapping_csrf,
            migration_id=self.migration_plan['migration_id'],kind=kind,
            inbox_id=self.accepted.inbox_id,artifact_id=artifact,**kwargs)

    async def http_request(self,kind='text',artifact=None,*,headers=None,**payload):
        import httpx
        import nonebot
        from fastapi import FastAPI
        from types import SimpleNamespace
        from src.bot_security.service import MobileAuthorization
        from src.plugins.ai_chat.admin_security import SESSION_COOKIE
        nonebot.init()
        from src.plugins.ai_chat.admin import AdminServices,register_admin
        app=FastAPI()
        register_admin(app,AdminServices(version='test',started_at=1,database=self.database,
            settings=SimpleNamespace(admin_origin='https://test'),
            mobile_authorization=MobileAuthorization(self.mapping_security,sender=None,bot_selector=lambda:None),
            official_migration_service=lambda:self.service.console))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://test',
                headers={'Origin':'https://test','x-csrf-token':self.mapping_csrf}) as client:
            client.cookies.set(SESSION_COOKIE,self.mapping_cookie)
            return await client.post('/bot-admin/api/v1/qq-official/migrations/'+
                self.migration_plan['migration_id']+'/verification',headers=headers,
                json=dict(preview_digest=self.migration_plan['preview_digest'],kind=kind,
                          inbox_id=self.accepted.inbox_id,artifact_id=artifact)|payload)

    def artifact(self):
        job=self.jobs.claim_due('TEST_SOURCE',kinds=(account_job_kind('turn',self.account.key),),now=self.now)[0]
        with self.tx() as c:
            handle=execution.claim_execution(c,token=self.token,job=job,now=self.now,deadline_at=self.now+200,admission=self.store.admitted).handle
        turn=self.journal.start_turn(self.endpoint.scope,trigger_canonical_message_id=self.ingested.canonical_message_id,
            objective='TEST file',provider='test',model='test',started_at=self.now)
        args=dict(token=self.token,handle=handle,job=job,now=self.now,admission=self.store.admitted)
        with self.tx() as c:
            execution.begin_execution(c,**args,journal_turn_id=turn.turn_id)
            source=foreground_source(c,**args,pending_deadline_at=self.now+1000)
        self.blobs=ProjectBlobStore(Path(self.temp.name)/'objects')
        self.files=ArtifactStore(transaction=self.tx,jobs=self.jobs,blobs=self.blobs,clock=lambda:self.now)
        item=self.files.register(source,content=PDF,filename='verification.pdf',logical_request_key=hashlib.sha256(b'TEST_VERIFY_FILE').hexdigest())
        with self.tx() as c:
            execution.capture_result(c,**args,body=MessageBody((TextNode(0,'TEST created'),)),turn_id=turn.turn_id,status='succeeded')
        self.jobs.mark_succeeded(job.job_id,job.lease_owner,now=self.now,lease_generation=job.lease_generation)
        self.native=NativeUploadVault(Path(self.temp.name)/'native',clock=lambda:self.now)
        self.links=FileCredentialVault(Path(self.temp.name)/'links')
        self.addCleanup(self.native.close);self.addCleanup(self.links.close)
        self.payloads=TrustedFilePayloads(admission=self.store.admitted,route_policy=lambda c,a,r:r=='native',
            native_vault=self.native,link_vault=self.links,link_origin='https://download.example.test')
        self.outbox.protected_payloads=self.payloads
        return item



class VerificationHostTests(VerificationHostFixture):
    async def test_text_and_real_file_upload_require_distinct_committed_receipts(self):
        artifact=self.artifact()
        await self.verifying()
        text=await self.http_request()
        self.assertEqual(text.status_code,200,text.text)
        self.assertEqual(set(text.json()),{'state','plan_id'})
        repeated=await self.http_request()
        self.assertEqual(repeated.status_code,200,repeated.text)
        self.assertEqual(repeated.json(),text.json())
        self.assertEqual(len(self.query('SELECT * FROM delivery_plans')),1)
        self.assertFalse(self.query('SELECT * FROM migration_verification_receipts'))
        delivery=self.query('SELECT delivery_id FROM deliveries WHERE plan_id=?',(text.json()['plan_id'],))[0]['delivery_id']
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        await self.session.maintain()
        self.assertEqual(self.query('SELECT kind FROM migration_verification_receipts')[0]['kind'],'text')
        self.assertEqual((await self.http_request('file',None)).status_code,422)
        response=await self.http_request('file',artifact.artifact_id)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json(),{'state':'awaiting_file','plan_id':None})
        job=self.jobs.claim_due('TEST_VERIFY_FILE',kinds=(file_job_kind(self.account),),now=self.now)[0]
        dispatcher=OfficialFileDispatcher(self.token,self.files,self.payloads,self.outbox,self.rest,
            calls=self.calls,lease_check=lambda:True,check_delivery_ready=lambda *_:True,clock=lambda:self.now)
        result=await dispatcher(job)
        self.assertEqual(result['status'],'handed_off')
        file_plan=self.query('SELECT active_plan_id FROM qq_official_file_deliveries WHERE artifact_id=?',(artifact.artifact_id,))[0]['active_plan_id']
        delivery=self.query('SELECT delivery_id FROM deliveries WHERE plan_id=?',(file_plan,))[0]['delivery_id']
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertEqual([item['payload']['msg_type'] for item in self.fake.received],[0,7])
        self.now+=5
        await self.session.maintain()
        self.assertEqual({row['kind'] for row in self.query('SELECT kind FROM migration_verification_receipts')},{'text','file'})
        await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,action='complete',
            request={'migration_id':self.migration_plan['migration_id']},preview_digest=self.migration_plan['preview_digest'])
        self.assertEqual(self.query('SELECT phase FROM migration_routes')[0]['phase'],'completed')
        self.now+=5
        await self.session.maintain()
        self.assertEqual(len(self.query('SELECT * FROM migration_verification_receipts')),2)

    async def test_http_rejects_stale_review_forged_sources_and_csrf_before_send(self):
        await self.verifying()
        for payload,status in (({'preview_digest':'0'*64},409),({'inbox_id':'0'*32},409),
                               ({'kind':'file'},422),({'nonce':'forged'},422)):
            response=await self.http_request(**payload)
            self.assertEqual(response.status_code,status,response.text)
        self.assertEqual((await self.http_request(headers={'x-csrf-token':'WRONG'})).status_code,403)
        self.assertEqual((await self.http_request(headers={'Origin':'https://other.test'})).status_code,403)
        self.assertFalse(self.query('SELECT * FROM migration_verification_sources'))
        self.assertFalse(self.query('SELECT * FROM delivery_plans'))
        self.assertFalse(self.fake.received)

    async def test_unknown_or_expired_owner_source_cannot_refresh_verification(self):
        await self.verifying();plan=await self.request()
        await self.session.maintain()
        self.assertFalse(self.query('SELECT * FROM migration_verification_receipts'))
        fixed=dict(self.query('SELECT * FROM migration_verification_sources')[0])
        self.now+=31
        with self.assertRaises(MigrationDenied):await self.request()
        self.assertEqual(dict(self.query('SELECT * FROM migration_verification_sources')[0]),fixed)
        self.assertEqual((await self.sender.send_delivery(plan.delivery_ids[0])).status,'cancelled')
        self.assertFalse(self.fake.received)

    async def test_owner_revocation_stops_new_send_but_cannot_erase_applied_fact(self):
        await self.verifying();plan=await self.request()
        self.assertEqual((await self.sender.send_delivery(plan.delivery_ids[0])).status,'committed')
        self.query("UPDATE qq_official_owner_actor_links SET state='revoked' WHERE endpoint_key=?",(self.endpoint.key,))
        await self.session.maintain()
        self.assertEqual(len(self.query('SELECT * FROM migration_verification_receipts')),1)
        with self.assertRaises(MigrationDenied):await self.request()
        self.assertEqual(len(self.fake.received),1)
