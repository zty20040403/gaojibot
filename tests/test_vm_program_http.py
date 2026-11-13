"""Production VM lifecycle + HTTP action routes + real outbox to FakePlatform."""
import asyncio
import unittest
from unittest.mock import AsyncMock,patch

from tests import test_vm_lifecycle as fixture
from tests import test_qq_official_sender as sender_fixture
from tests.qq_official_outbox_fixture import OutboxFixture
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.platform_types import EndpointKey
from src.qq_official.credentials import Credentials
from src.qq_official.http import RestClient
from src.qq_official.testing.fake_platform import FakePlatform
from src.qq_official.token import TokenManager


class VMProgramHTTPTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    ensure_current = sender_fixture.OfficialSenderTests.ensure_current
    make_sender = sender_fixture.OfficialSenderTests.make_sender
    fast_sleep = sender_fixture.OfficialSenderTests.fast_sleep

    async def asyncSetUp(self):
        await fixture.VMLifecycleTests.asyncSetUp(self)
        self.base = f'http://127.0.0.1:{self.service.gateway.port}'
        self.deliveries,self.ledger = DeliveryStore(self.database),MessageLedger(self.database)
        self.addCleanup(self.deliveries.close); self.addCleanup(self.ledger.close)
        self.outbox = OfficialSendStore(self.deliveries,admission=lambda *_:False,
            pepper=b'TEST_PROGRAM_PEPPER'*3,clock=lambda:self.now)
        with self.tx() as cursor:
            observe_ready(cursor,self.token,{'op':0,'t':'READY','d':{'session_id':'TEST_SESSION',
                'user':{'id':'TEST_BOT','bot':True}}},self.now)
        self.query("UPDATE qq_official_endpoints SET proactive_state='unknown'")
        self.fake = FakePlatform(clock=lambda:self.now); self.fake.app_id = self.account.app_id
        await self.fake.start(); self.addAsyncCleanup(self.fake.close)
        self.fake.proactive_allowed[('group',self.endpoint.native_conversation_id)] = True
        self.tokens = TokenManager(Credentials(self.account.app_id,self.fake.app_secret),self.fake.base_url,allow_test_server=True)
        self.addAsyncCleanup(self.tokens.aclose)
        self.rest = RestClient(self.tokens,allow_test_server=True); self.addAsyncCleanup(self.rest.aclose)
        self.sender = self.make_sender(); self.addAsyncCleanup(self.sender.stop)
        self.service.runtime.sender = self.sender

    async def post(self, **changes):
        return await self.client.post(self.base+'/vm/say',json=dict(text='TEST_HELLO',request_id='TEST_REQUEST',program='TEST_PROGRAM')|changes)

    async def test_vm_speech_status_and_idempotency_without_job_or_human_identity(self):
        response = await self.post()
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['state'],'completed')
        self.assertEqual(self.service.gateway.channels,{})
        self.assertEqual((await self.post()).json(),response.json())
        self.assertEqual(len(self.fake.received),1)
        self.assertEqual((await self.post(text='TEST_CHANGED')).status_code,409)
        self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,1)',(self.endpoint.scope.key,))
        denied = await self.post(request_id='TEST_AFTER_STOP')
        self.assertEqual((denied.status_code,denied.json()['error']),(403,'conversation_stopped_or_replaced'))
        status = await self.client.get(self.base+'/vm/actions/'+response.json()['action_id'])
        self.assertEqual((status.status_code,status.json()['state']),(200,'completed'))
        # Resolve a lost response with the CLIENT's request id, even stopped.
        recovered = await self.client.get(self.base+'/vm/actions',params={'request_id':'TEST_REQUEST'})
        self.assertEqual(recovered.json(),status.json())
        self.assertEqual(len(self.fake.received),1)

    async def test_task_token_scope_injection_and_large_body_fail_without_side_effects(self):
        token,_ = self.service.gateway.open(source={'job_id':1,'endpoint_key':self.endpoint.key},current=AsyncMock())
        self.assertEqual((await self.client.post(self.base+'/vm/say',headers={'Authorization':'Bearer '+token},json={})).status_code,401)
        self.assertEqual((await self.client.get(self.base+'/vm/actions',headers={'Authorization':'Bearer '+token},
            params={'request_id':'TEST_REQUEST'})).status_code,401)
        self.assertEqual((await self.post(endpoint_key='TEST_OTHER')).status_code,400)
        large = await self.client.post(self.base+'/vm/say',content=b'X'*16385)
        self.assertEqual(large.status_code,413)
        self.assertEqual(self.query('SELECT * FROM qq_official_program_actions'),[])
        self.assertEqual(self.fake.received,[])

    async def test_quota_and_external_delivery_denial_are_explicit_http_failures(self):
        self.query('INSERT INTO conversation_settings(scope_key,program_say_limit) VALUES (?,1)',(self.endpoint.scope.key,))
        self.fake.proactive_allowed[('group',self.endpoint.native_conversation_id)] = False
        response = await self.post()
        self.assertEqual((response.status_code,response.json()['state'],response.json()['error_code']),
            (502,'failed','proactive_denied'))
        self.assertEqual((await self.post(request_id='TEST_SECOND')).status_code,429)
        self.assertEqual(self.query("SELECT * FROM messages WHERE direction='outbound'"),[])
        current = await self.lifecycle.credentials.authenticate(self.token_value)
        snapshot = await self.lifecycle.status.snapshot(self.endpoint,authorize=lambda cursor:self.lifecycle.store.check(cursor,current))
        self.assertEqual(snapshot['recent_failures'][0]['error_code'],'proactive_denied')
        self.assertEqual(snapshot['recent_failures'][0]['state'],'failed')

    async def test_slow_body_cannot_cross_stop_start(self):
        authenticated,release = asyncio.Event(),asyncio.Event()
        original = self.lifecycle.api.authorize
        async def authorize(*args,**kwargs):
            value = await original(*args,**kwargs)
            authenticated.set()
            return value
        async def body():
            yield b'{"request_id":"TEST_DELAYED",'
            await release.wait()
            yield b'"text":"TEST_OLD_BODY"}'
        with patch.object(self.lifecycle.api,'authorize',side_effect=authorize):
            pending = asyncio.create_task(self.client.post(self.base+'/vm/say',content=body()))
            try:
                await asyncio.wait_for(authenticated.wait(),3)
                self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,FALSE,1)',(self.endpoint.scope.key,))
                release.set()
                response = await asyncio.wait_for(pending,3)
                self.assertEqual((response.status_code,response.json()['error']),(403,'conversation_stopped_or_replaced'))
            finally:
                release.set(); pending.cancel(); await asyncio.gather(pending,return_exceptions=True)
        self.assertEqual(self.fake.received,[])
        self.assertEqual(self.query('SELECT * FROM qq_official_program_actions'),[])
        self.assertEqual((await self.post(request_id='TEST_NEW')).status_code,200)

    async def test_status_is_scoped_even_if_another_action_id_is_known(self):
        other = EndpointKey(self.account,'group','TEST_OTHER'); self.provision(other)
        current = self.lifecycle.store.issue(other,'s654321','00000000-0000-4000-8000-000000000001',expected_route=(1,1)).source
        action,_ = self.lifecycle.actions.reserve(current,generation=0,kind='say',program='TEST_OTHER',request_id='TEST_OTHER',payload={'text':'TEST'})
        response = await self.client.get(self.base+'/vm/actions/'+action['action_id'])
        self.assertEqual((response.status_code,response.json()['error']),(404,'program_action_not_found'))
        by_request = await self.client.get(self.base+'/vm/actions',params={'request_id':'TEST_OTHER'})
        self.assertEqual(by_request.status_code,404)
