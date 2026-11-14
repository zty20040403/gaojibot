"""Real PG receipts and logged-in admin projections; no live model/platform calls."""
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from src.plugins.ai_chat.admin import AdminServices
from src.plugins.ai_chat.billing_audit import snapshot
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody,TextNode
from src.plugins.ai_chat.model_billing import BillingSource,ModelBilling
from src.plugins.ai_chat.model_pricing import ModelPricing
from src.plugins.ai_chat.model_usage import TokenUsage
from src.plugins.ai_chat.platform_types import AccountKey,ActorKey,EndpointKey
from tests.admin_session_fixture import ApprovedClient,register_admin
from tests.ops_action_fixture import OpsFixture
from tests.postgres_fixture import isolated_postgres_for_test
from tests.test_llm_gateway import profile
from tests import test_admin_host_apis as auth_fixture
from tests import test_billing_sources as source_fixture


class AdminBillingTests(OpsFixture,unittest.IsolatedAsyncioTestCase):
    test_anonymous_and_member_cannot_read_any_scope = auth_fixture.AdminHostAPITests.test_anonymous_and_member_cannot_read_any_scope

    def setUp(self):
        with patch('tests.qq_official_outbox_fixture.postgres_for_test',side_effect=isolated_postgres_for_test):
            OpsFixture.setUp(self)
        self.store = ModelBilling(self.database,clock=lambda:self.now)
        self.profile = replace(profile('test',api_key='TEST_PRIVATE_KEY'),pricing=ModelPricing('2','0.2','8'))
        self.source = BillingSource(self.endpoint,'background_agent',self.actor,background_job_id=self.job.job_id)

    async def asyncSetUp(self):
        clock = patch('src.plugins.ai_chat.admin_api.billing.time.time',side_effect=lambda:self.now+10)
        clock.start(); self.addCleanup(clock.stop)
        self.app = FastAPI()
        register_admin(self.app,AdminServices(version='TEST',started_at=1,database=self.database,
            settings=SimpleNamespace(admin_origin='https://test',alert_notify_enabled=False)),test_case=self)
        self.client = ApprovedClient(transport=httpx.ASGITransport(app=self.app),base_url='https://test')
        self.addAsyncCleanup(self.client.aclose)
        self.path = '/bot-admin/api/v1/model-costs'

    def call(self,*,source=None,usage=TokenUsage(1000,50,600,0),model=None):
        call = self.store.begin(source or self.source,model or self.profile)
        if usage is not None:self.store.finish(call,usage)
        return call

    async def view(self,**params):
        response = await self.client.get(self.path,headers={'X-Test-Login':'yes'},
            params=dict(as_of=self.now+1,**params))
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    async def test_receipts_and_frozen_names_prices_are_not_legacy_quota_or_current_profile(self):
        self.call(); self.call(source=replace(self.source,purpose='turn',background_job_id=None))
        self.profile = replace(self.profile,pricing=ModelPricing('20','2','80'))
        ledger = MessageLedger(self.database); self.addCleanup(ledger.close)
        ledger.record_message(self.endpoint.scope,native_message_id='TEST_RENAME',sender_native_user_id=self.actor.native_user_id,
            sender_display='TEST_RENAMED',body=MessageBody((TextNode(0,'TEST'),)),occurred_at=self.now,
            actor=self.actor,source_endpoint=self.endpoint)
        actors = await self.view(dimension='actor')
        self.assertEqual(actors['items'][0]['label'],'TEST_OWNER')
        self.assertIsInstance(actors['items'][0]['person'],str)
        self.assertEqual(actors['totals']['cost_usd'],'0.00264')
        self.assertTrue(actors['totals']['cost_complete'])
        self.assertEqual(actors['totals']['cache_percent'],'60.0')
        jobs = await self.view(dimension='job')
        self.assertEqual({row['job'] for row in jobs['items']},{str(self.job.job_id),None})
        self.assertEqual(sum(Decimal(row['cost_usd']) for row in jobs['items']),Decimal('0.00264'))
        for row in actors['items']:
            self.assertFalse({'actor_key','pricing_json','source_metadata_json','call_id'} & row.keys())
        self.assertNotIn('TEST_PRIVATE_KEY',str(actors)); self.assertEqual(self.app.state.auth_messages,[])

    async def test_unknown_usage_missing_price_pending_and_bigint_totals_stay_explicit(self):
        self.call(); self.call(usage=TokenUsage(1000,50,None,0))
        self.call(model=replace(self.profile,pricing=None)); self.call(usage=None)
        totals = (await self.view())['totals']
        self.assertEqual(totals['cost_usd'],'0.00172'); self.assertFalse(totals['cost_complete'])
        self.assertEqual(totals['input_tokens'],'3000'); self.assertEqual(totals['pending_calls'],1)
        self.assertEqual(totals['incomplete_cost_calls'],3); self.assertIsNone(totals['cache_percent'])
        other = EndpointKey(self.account,'private','TEST_BIGINT'); self.provision(other)
        self.call(source=BillingSource(other,'memory'),usage=TokenUsage(9007199254740993,0,0,0))
        row = (await self.view(scope_key=other.scope.key))['totals']
        self.assertEqual(row['input_tokens'],'9007199254740993')
        self.assertEqual(row['cost_usd'],'18014398509.481986')

    async def test_scope_route_and_account_do_not_merge_identically_named_sources(self):
        self.call()
        foreign = EndpointKey(AccountKey('qq-official','TEST_FOREIGN','test'),'private',self.endpoint.native_conversation_id)
        self.provision(foreign)
        actor = ActorKey(foreign.account,'c2c_user','',foreign.native_conversation_id)
        self.call(source=BillingSource(foreign,'turn',actor))
        history = EndpointKey(self.account,'private','TEST_ARCHIVE')
        self.query('UPDATE qq_official_endpoints SET canonical_scope_key=? WHERE endpoint_key=?',(history.scope.key,self.endpoint.key))
        all_scopes = await self.view()
        self.assertEqual(all_scopes['group_count'],2)
        self.assertEqual({row['scope_key'] for row in all_scopes['items']},{self.endpoint.scope.key,foreign.scope.key})
        physical = await self.view(scope_key=self.endpoint.scope.key)
        self.assertEqual(physical['totals']['calls'],1)
        self.assertEqual((await self.view(scope_key=history.scope.key))['totals']['calls'],0)
        self.assertEqual(len((await self.view(dimension='actor'))['items']),2)

    async def test_fixed_window_seek_pages_and_incompatible_cursors_are_rejected(self):
        original = self.now
        old = self.now-86400
        self.now = old; self.call()
        self.now = old+1; self.call()
        self.now = original
        for index in range(3):
            endpoint = EndpointKey(self.account,'private',f'TEST_PAGE_{index}'); self.provision(endpoint)
            self.call(source=BillingSource(endpoint,'memory'))
        first = await self.view(days=1,limit=2)
        second = await self.view(days=1,limit=2,after=first['next_cursor'])
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(len({row['id'] for row in first['items']+second['items']}),4)
        self.assertEqual(first['totals']['calls'],4)
        self.assertEqual(first['totals'],second['totals'])
        for params in ({'dimension':'TEST_BAD'},{'days':0},{'limit':101},{'after':'0 OR TRUE'},
                {'after':'e30'},{'after':first['next_cursor'],'days':2},{'as_of':self.now+20}):
            response = await self.client.get(self.path,params=dict(params))
            self.assertEqual(response.status_code,422,response.text)
        self.assertEqual((await self.client.post(self.path,json={'budget':1})).status_code,405)

    async def test_late_earlier_bucket_does_not_shift_seek_pages_or_repeat_items(self):
        for index in range(4):
            endpoint = EndpointKey(self.account,'private',f'TEST_PAGE_{index}'); self.provision(endpoint)
            self.call(source=BillingSource(endpoint,'memory'))
        first = await self.view(limit=2)
        late = min((EndpointKey(self.account,'private',name) for name in ('TEST_000','TEST_A_ZERO','TEST_PAGE_ZERO')),
            key=lambda endpoint:endpoint.scope.key)
        self.assertLess(late.scope.key,first['items'][-1]['scope_key'])
        self.provision(late); self.call(source=BillingSource(late,'memory'))
        second = await self.view(limit=2,after=first['next_cursor'])
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(len({row['id'] for row in first['items']+second['items']}),4)
        self.assertEqual(second['group_count'],5)
        self.assertEqual(len((await self.view())['items']),5)  # Refresh picks up late earlier buckets.

    async def test_legacy_missing_attribution_is_not_backfilled_from_current_identity(self):
        call = self.call()
        with self.tx() as cursor:
            cursor.execute('''INSERT INTO model_usage_calls(call_id,endpoint_key,scope_key,actor_key,purpose,
                provider,model,profile,started_at,finished_at,input_tokens,output_tokens,cached_tokens,cache_write_tokens,
                cost_usd,cost_complete,pricing_json) SELECT ?,endpoint_key,scope_key,actor_key,purpose,
                provider,model,profile,started_at,finished_at,input_tokens,output_tokens,cached_tokens,cache_write_tokens,
                cost_usd,cost_complete,pricing_json FROM model_usage_calls WHERE call_id=?''',('f'*32,call))
        rows = (await self.view(dimension='actor'))['items']
        self.assertEqual({row['label'] for row in rows},{'TEST_OWNER','未记录发起人'})
        self.assertEqual(sum(row['calls'] for row in rows),2)


class SharedBillingTests(source_fixture.ProgramBillingSourcesTests):

    def test_program_and_command_completion_share_one_joint_origin(self):
        import json
        from src.plugins.ai_chat.adapters.qq_official.background_commands import BackgroundCommands
        run = self.batch()
        command,_ = BackgroundCommands(self.sessions).reserve(self.current,generation=(0,0),program='TEST_A',
            request_id='TEST_COMMAND',argv=['printf','TEST'])
        self.query('UPDATE qq_official_program_actions SET source_command_id=? WHERE action_id=?',
            (command['command_id'],json.loads(run['input_ids_json'])[0]))
        store = ModelBilling(self.database,clock=lambda:self.now)
        call = store.begin(BillingSource(self.endpoint,'program_wake',program_wake_id=run['wake_id']),
            replace(profile('test'),pricing=ModelPricing('2','0.2','8')))
        store.finish(call,TokenUsage(1000,50,600,0))
        for dimension in ('job','program'):
            view = snapshot(self.database,dimension=dimension,days=1,as_of=self.now+1,scope_key=self.endpoint.scope.key)
            self.assertEqual(len(view['items']),1)
            item = view['items'][0]
            self.assertEqual(item['commands'],[command['command_id']]); self.assertEqual(item['programs'],['TEST_A','TEST_B'])
            self.assertTrue(item['shared']); self.assertEqual(item['cost_usd'],'0.00132')

    def test_coalesced_programs_are_one_joint_cost_and_not_a_fake_person_or_agent(self):
        run = self.batch()
        store = ModelBilling(self.database,clock=lambda:self.now)
        model = replace(profile('test'),pricing=ModelPricing('2','0.2','8'))
        call = store.begin(BillingSource(self.endpoint,'program_wake',program_wake_id=run['wake_id']),model)
        store.finish(call,TokenUsage(1000,50,600,0))
        for dimension in ('program','actor','job'):
            view = snapshot(self.database,dimension=dimension,days=1,as_of=self.now+1,scope_key=self.endpoint.scope.key)
            self.assertEqual(view['totals']['cost_usd'],'0.00132')
            self.assertEqual(len(view['items']),1)
            item = view['items'][0]
            self.assertEqual(item['cost_usd'],'0.00132')
            self.assertIsNone(item['person']); self.assertIsNone(item['job'])
            if dimension=='program':
                self.assertEqual(item['programs'],['TEST_A','TEST_B']); self.assertTrue(item['shared'])
            if dimension=='actor':self.assertEqual(item['label'],'程序或系统（不归属真人）')
