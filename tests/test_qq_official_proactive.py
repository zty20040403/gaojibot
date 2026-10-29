import asyncio
from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

from tests.qq_official_native_schedule_fixture import NativeFixture
from src.plugins.ai_chat.adapters.qq_official.proactive_ingress import ProactiveIngress
from src.plugins.ai_chat.adapters.qq_official.native_background import NativeBackgroundService
from src.plugins.ai_chat.application.shared_proactive import SharedProactiveScorer
from src.plugins.ai_chat.adapters.qq_official.authority import OwnerAuthority
from src.plugins.ai_chat.adapters.qq_official.binding import BindingReducer
from src.plugins.ai_chat.adapters.qq_official.inbox import OfficialInboxStore
from src.plugins.ai_chat.adapters.qq_official.ingest import process_inbox
from src.plugins.ai_chat.adapters.qq_official.migration_native_sources import NativeSources,SourceAdmission
from src.plugins.ai_chat.adapters.qq_official.migration_host_ports import MigrationPlanGuard
from src.plugins.ai_chat.adapters.qq_official.migration_routes import RouteService
from src.plugins.ai_chat.adapters.qq_official.migration_journal import Journal
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.rate_policy import LocalRatePermit
from src.plugins.ai_chat.adapters.qq_official.outbox_types import SendReservation
from src.plugins.ai_chat.model_catalog import ModelProfile
from src.qq_official.testing.fake_platform import FakePlatform
from src.qq_official.credentials import Credentials
from src.qq_official.token import TokenManager
from src.qq_official.http import RestClient


class ProactivePGTests(NativeFixture):
    def setUp(self):
        super().setUp()
        authority=OwnerAuthority(clock=lambda:self.now)
        binding=BindingReducer(b'TEST_PROACTIVE_PEPPER_0123456789012',authority=authority,epoch_guard=assert_epoch,clock=lambda:self.now)
        self.inbox=OfficialInboxStore(self.database,jobs=self.jobs,binding=binding,authority=authority,clock=lambda:self.now)
        self.addCleanup(self.inbox.close)
        self.inbox.accept({'op':0,'s':0,'t':'READY','d':{'session_id':'TEST_NEW_SESSION'}},token=self.token,session_id='TEST_NEW_SESSION')
        profile=ModelProfile(name='TEST',provider='test',protocol='openai',model='TEST',base_url='http://127.0.0.1',api_key_required=False)
        self.usage_calls=AsyncMock()
        self.scorer=SharedProactiveScorer(profile=profile,persona='TEST persona',clock_prompt=lambda:'TEST clock',
            threshold=90,max_reply_chars=120,record_usage=self.usage_calls)
        self.proactive=ProactiveIngress(token=self.token,transaction=self.tx,admission=self.admission,outbox=self.outbox,
            scorer=self.scorer,clock=lambda:self.now,database_calls=asyncio.to_thread,threshold=90,percent=100,max_checks_per_hour=5,random_value=lambda:0)
        # Actual configured-host selector, not a fixture with no prior guard.
        # TxA must record the original native source before its first receipt.
        self.routes=RouteService(tokens={self.account.key:self.token})
        self.native_sources=NativeSources(self.routes)
        self.inbox.source_authority=self.native_sources
        self.admission=SourceAdmission(self.admission,self.native_sources)
        self.inbox.admitted=self.admission
        self.proactive.admission=self.admission
        self.outbox.admission=self.admission
        self.outbox.source_guard=MigrationPlanGuard(native=self.native_sources,journal=Journal(self.routes))
        runtime=SimpleNamespace(outbox=self.outbox,migration_service=None,store=SimpleNamespace(transaction=self.tx),_calls=asyncio.to_thread)
        self.background=NativeBackgroundService(runtime=runtime,token=self.token,domain=self.engine,proactive=self.proactive)
        self.background.attach()
        self.seq=0

    def ingress(self,*,mention=False):
        self.seq+=1
        frame={'op':0,'s':self.seq,'t':'GROUP_AT_MESSAGE_CREATE' if mention else 'GROUP_MESSAGE_CREATE','id':f'TEST_EV_{self.seq}',
            'd':{'id':f'TEST_CHAT_{self.seq}','group_openid':self.endpoint.native_conversation_id,
                 'author':{'member_openid':self.actor.native_user_id},'content':'这是实际未点名的讨论消息',
                 'timestamp':datetime.fromtimestamp(self.now,timezone.utc).isoformat()}}
        accepted=self.inbox.accept(frame,token=self.token,session_id='TEST_NEW_SESSION')
        job=self.jobs.claim_due('TEST_INGEST',kinds=(account_job_kind('ingest',self.account.key),),now=self.now)[0]
        with self.tx() as c:
            result=process_inbox(c,inbox_id=accepted.inbox_id,lease_token=self.token,ledger=self.ledger,jobs=self.jobs,
                epoch_guard=assert_epoch,admission=self.admission,now=self.now)
        return accepted,job,result

    def test_real_ingress_shared_scorer_and_m3_receipt_once(self):
        accepted,job,result=self.ingress();self.assertEqual(result.status,'processed');self.assertIsNone(result.dispatch_job_id)
        payload={'interest':99,'reply':'TEST useful reply','voice_suitable':False}
        with patch('src.plugins.ai_chat.application.shared_proactive.ask_deepseek_json',new=AsyncMock(return_value=payload)) as llm:
            outcome=asyncio.run(self.background.on_ingested(job))
            self.assertEqual(outcome['status'],'queued');self.assertEqual(llm.await_count,1)
            self.assertEqual(asyncio.run(self.background.on_ingested(job))['status'],'discarded')
            self.assertEqual(llm.await_count,1)
        self.assertEqual(self.usage_calls.await_count,1)
        self.assertIsInstance(self.background.previous_guard,MigrationPlanGuard)
        self.assertEqual(len(self.query('SELECT * FROM qq_migration_native_sources')),1)
        self.assertTrue(self.jobs.mark_succeeded(job.job_id,job.lease_owner,now=self.now,lease_generation=job.lease_generation))
        observation=self.query('SELECT * FROM qq_official_proactive_observations')[0]
        self.assertEqual(observation['deadline_at'],self.now+30)
        delivery=self.query('SELECT * FROM deliveries WHERE plan_id=?',(observation['plan_id'],))[0]
        self.assertEqual(delivery['purpose'],'unsolicited')
        reservation=self.outbox.prepare(self.token,delivery['delivery_id'],LocalRatePermit(self.account.key,self.endpoint.key,'group','passive','unverified',float(self.now)))
        self.assertIsInstance(reservation,SendReservation);self.assertTrue(self.outbox.mark_dispatching(self.token,reservation))
        wire=self.outbox.request_started(reservation)
        async def send():
            async with FakePlatform(clock=lambda:self.now,nonmention_reply_allowed=True) as fake:
                fake.add_window(kind='group',target=self.endpoint.native_conversation_id,message_id='TEST_CHAT_1')
                tokens=TokenManager(Credentials(fake.app_id,fake.app_secret),base_url=fake.base_url,allow_test_server=True)
                rest=RestClient(tokens,allow_test_server=True)
                try:return await rest.send_message('group',self.endpoint.native_conversation_id,wire)
                finally:await rest.aclose();await tokens.aclose()
        receipt=self.outbox.capture_receipt(reservation,asyncio.run(send()))
        self.assertEqual(self.outbox.finalize(self.token,receipt,ledger=self.ledger).status,'committed')
        self.assertEqual(len(self.query('SELECT * FROM delivery_attempts')),1)
        self.assertEqual(observation['inbox_id'],accepted.inbox_id)

    def test_actual_no_window_scores_then_discards_without_plan(self):
        _,job,_=self.ingress();self.query("UPDATE qq_official_reply_windows SET state='expired'")
        with patch('src.plugins.ai_chat.application.shared_proactive.ask_deepseek_json',new=AsyncMock(return_value={'interest':99,'reply':'reply'})) as llm:
            outcome=asyncio.run(self.background.on_ingested(job))
            self.assertEqual(outcome['status'],'discarded');self.assertEqual(llm.await_count,1)
        self.assertFalse(self.query('SELECT * FROM delivery_plans'))
        self.assertEqual(self.query('SELECT state FROM qq_official_proactive_observations')[0][0],'discarded')

    def test_actual_mentioned_chat_never_proactively_scores(self):
        _,job,result=self.ingress(mention=True);self.assertIsNotNone(result.dispatch_job_id)
        with patch('src.plugins.ai_chat.application.shared_proactive.ask_deepseek_json',new=AsyncMock()) as llm:
            self.assertEqual(asyncio.run(self.background.on_ingested(job))['status'],'discarded')
            llm.assert_not_awaited()

    def test_actual_slow_scorer_cannot_obtain_new_deadline(self):
        _,job,_=self.ingress()
        async def slow(*args,**kwargs):self.now+=31;return {'interest':99,'reply':'late'}
        with patch('src.plugins.ai_chat.application.shared_proactive.ask_deepseek_json',new=slow):
            outcome=asyncio.run(self.background.on_ingested(job))
        self.assertEqual(outcome['status'],'discarded')
        self.assertFalse(self.query('SELECT * FROM delivery_plans'))
        self.assertFalse(self.query('SELECT * FROM delivery_attempts'))


    def test_actual_m5_source_epoch_changed_before_send_blocks_proactive_plan(self):
        _,job,_=self.ingress()
        with patch('src.plugins.ai_chat.application.shared_proactive.ask_deepseek_json',new=AsyncMock(return_value={'interest':99,'reply':'TEST reply'})):
            self.assertEqual(asyncio.run(self.background.on_ingested(job))['status'],'queued')
        observation=self.query('SELECT * FROM qq_official_proactive_observations')[0]
        delivery=self.query('SELECT delivery_id FROM deliveries WHERE plan_id=?',(observation['plan_id'],))[0][0]
        self.query('UPDATE qq_official_endpoints SET generation=generation+1 WHERE endpoint_key=?',(self.endpoint.key,))
        result=self.outbox.candidate(self.token,delivery)
        self.assertEqual(result.status,'cancelled')
        self.assertFalse(self.query('SELECT * FROM delivery_attempts'))
