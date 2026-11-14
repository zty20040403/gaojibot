"""Status through the actual command consumer factory, without any model call."""
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official import turns
from src.plugins.ai_chat.model_billing import BillingSource,ModelBilling
from src.plugins.ai_chat.model_pricing import ModelPricing
from src.plugins.ai_chat.model_usage import TokenUsage
from src.plugins.ai_chat.platform_types import EndpointKey
from tests import test_qq_official_turn_consumer as base
from tests.test_llm_gateway import profile


class NativeStatusTests(unittest.IsolatedAsyncioTestCase):
    setUp = base.ConsumerTests.setUp
    asyncSetUp = base.ConsumerTests.asyncSetUp
    asyncTearDown = base.ConsumerTests.asyncTearDown

    def bill(self,*,endpoint=None,usage=TokenUsage(1000,50,600,0),priced=True):
        source = BillingSource(endpoint,'proactive') if endpoint else BillingSource(self.f.endpoint,'turn',self.f.actor)
        selected = replace(profile('test',api_key='TEST_SECRET'),pricing=ModelPricing('2','0.2','8') if priced else None)
        store = ModelBilling(self.f.database,clock=lambda:self.f.now)
        call = store.begin(source,selected)
        if usage is not None:store.finish(call,usage)

    def build(self,text='/status',*,enabled=True):
        self.f.query('UPDATE qq_official_inbound_events SET turn_text=?,turn_text_version=1 WHERE inbox_id=?',(text,self.f.inbox))
        app = SimpleNamespace(running_tasks=self.tasks,model_preferences=object(),reasoning_preferences=object(),usage_store=None,
            model_billing=ModelBilling(self.f.database) if enabled else None)
        runtime = SimpleNamespace(agent_service=None,store=SimpleNamespace(admitted=self.f.admission,clock=lambda:self.f.now,
            authority=SimpleNamespace(owner_for_actor=lambda *_:None)),deliveries=self.deliveries,outbox=self.outbox,
            journal=self.journal,_calls=self.calls,sender=None,_token=self.f.token,_gateway=SimpleNamespace(state='ready'),
            loaded=SimpleNamespace(account=self.f.account,credentials=object(),delivery=SimpleNamespace(turn_policy=lambda:None)))
        self.native_runtime = runtime
        with patch.object(turns,'PlatformChatService'),patch.object(turns,'build_platform_chat_orchestrator',return_value=self.orchestrator):
            return turns.build_turn_consumer(runtime,self.f.token,app)

    def reply_text(self):
        manifest = json.loads(self.f.query('SELECT manifest_json FROM delivery_plans')[0]['manifest_json'])
        return '\n'.join(part['canonical_text'] for part in json.loads(manifest['rendered'])['parts'])

    async def run_status(self,*,text='/status',enabled=True):
        consumer = self.build(text,enabled=enabled)
        self.assertEqual(await consumer(self.f.job),{'status':'handed_off'})
        self.agent.assert_not_awaited()
        self.assertEqual(self.f.query('SELECT * FROM agent_turns'),[])
        return consumer,self.reply_text()

    async def test_any_admitted_member_sees_only_physical_totals_and_duplicate_uses_saved_reply(self):
        self.bill()
        other = EndpointKey(self.f.account,'group','TEST_OTHER_COST')
        with self.f.tx() as cursor:
            cursor.execute('''INSERT INTO qq_official_endpoints(endpoint_key,account_key,kind,native_conversation_id,canonical_scope_key,
                binding_state,membership_state,created_at,updated_at) VALUES (?,?,'group',?,?,'revoked','absent',1,1)''',
                (other.key,self.f.account.key,other.native_conversation_id,other.scope.key))
            # Point this physical endpoint at a foreign history. Billing does
            # not join that alias and may not leak foreign counts or prices.
            cursor.execute('UPDATE qq_official_endpoints SET canonical_scope_key=? WHERE endpoint_key=?',
                (other.scope.key,self.f.endpoint.key))
        self.bill(endpoint=other,usage=TokenUsage(12345678,0,0,0))
        consumer,text = await self.run_status()
        self.assertIn('24 小时',text); self.assertIn('7 天',text)
        self.assertIn('输入 1,000',text); self.assertIn('缓存命中 60.0%',text)
        self.assertIn('$0.00132',text); self.assertNotIn('12,345,678',text)
        self.assertNotIn('TEST_SECRET',text); self.assertNotIn(self.f.actor.native_user_id,text)
        self.bill()
        self.assertEqual((await consumer(self.f.job))['status'],'handed_off')
        self.assertEqual(self.reply_text(),text)

    async def test_stopped_status_still_reads_without_resetting_control_or_sending_old_work(self):
        self.bill(priced=False)
        self.bill(usage=None)
        self.f.query('''INSERT INTO conversation_settings(scope_key,stopped,stop_generation,command_generation)
            VALUES (?,TRUE,4,7)''',(self.f.endpoint.scope.key,))
        _,text = await self.run_status()
        self.assertIn('已停止',text); self.assertIn('≥ $0',text)
        self.assertIn('2 次费用不完整',text)
        self.assertEqual(tuple(self.f.query('SELECT stopped,stop_generation,command_generation FROM conversation_settings')[0].values()),(True,4,7))

    async def test_daily_budget_is_physical_and_unknowns_and_queued_notice_do_not_claim_delivery(self):
        from src.plugins.ai_chat.conversation_config import change
        self.bill(); self.bill(priced=False); self.bill(usage=None)
        with self.f.tx() as cursor:
            change(cursor,self.f.endpoint,'daily_budget_usd','0.001',actor='TEST_ADMIN',now=self.f.now)
            cursor.execute("UPDATE conversation_budget_days SET notice_state='sending' WHERE scope_key=?",(self.f.endpoint.scope.key,))
        before = self.f.query('SELECT * FROM conversation_settings')
        _,text = await self.run_status()
        self.assertIn('今日预算：$0.001',text); self.assertIn('已记录费用 ≥ $0.00132',text)
        self.assertIn('预算限制中',text); self.assertIn('已排队，尚未确认',text)
        self.assertIn('2次费用不完整（1次未收尾）',text)
        self.assertNotIn('已确认送达',text); self.assertIn('不是供应商硬限额',text)
        self.assertEqual([dict(row) for row in self.f.query('SELECT * FROM conversation_settings')],[dict(row) for row in before])
        self.assertEqual(len(self.f.query('SELECT * FROM conversation_budget_days')),1)

    async def test_foreign_target_argument_has_no_query_or_write(self):
        _,text = await self.run_status(text='/status TEST_FOREIGN')
        self.assertIn('用法',text)
        self.assertEqual(self.f.query('SELECT * FROM model_usage_calls'),[])

    async def test_disabled_billing_never_falls_back_to_estimates_or_tasks(self):
        _,text = await self.run_status(enabled=False)
        self.assertIn('尚未配置',text); self.assertNotIn('$0',text)

    async def test_stale_route_prevents_projection_and_reply(self):
        consumer = self.build()
        # Replace the route after command admission but before its queued DB
        # read executes. The lifecycle must check again, not trust admission.
        async def calls(function,*args,**kwargs):
            if function.__name__=='read':
                self.f.query('UPDATE qq_official_endpoints SET route_epoch=route_epoch+1 WHERE endpoint_key=?',(self.f.endpoint.key,))
            return await self.calls(function,*args,**kwargs)
        self.native_runtime._calls = calls
        with patch('src.plugins.ai_chat.adapters.qq_official.status_commands.windows') as read:
            await consumer(self.f.job)
            read.assert_not_called()
        for plan in self.f.query('SELECT manifest_json FROM delivery_plans'):
            self.assertNotIn('宿主账单',plan['manifest_json'])
            self.assertNotIn('$',plan['manifest_json'])


class PrivateFixture(base.fixture.ExecutionFixture):
    endpoint_kind = 'private'


class PrivateStatusTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = NativeStatusTests.asyncSetUp
    asyncTearDown = NativeStatusTests.asyncTearDown
    bill = NativeStatusTests.bill
    build = NativeStatusTests.build
    run_status = NativeStatusTests.run_status
    reply_text = NativeStatusTests.reply_text

    def setUp(self):
        with patch.object(base.fixture,'ExecutionFixture',PrivateFixture):base.ConsumerTests.setUp(self)

    async def test_admitted_private_peer_can_read_own_bill_without_owner_grant(self):
        self.bill()
        _,text = await self.run_status()
        self.assertIn('$0.00132',text)
        self.assertIn('24 小时',text)
        self.assertNotIn(self.f.actor.native_user_id,text)
