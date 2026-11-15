"""Actual command consumer factory, original role evidence and durable replies."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official import turns
from tests import test_qq_official_turn_consumer as base


class NativeConfigTests(unittest.IsolatedAsyncioTestCase):
    setUp = base.ConsumerTests.setUp
    asyncSetUp = base.ConsumerTests.asyncSetUp
    asyncTearDown = base.ConsumerTests.asyncTearDown

    async def exercise(self,text,*,role='member',owner=False,available=False,expired=False):
        private = self.f.endpoint.kind=='private'
        raw = {'op':0,'t':'C2C_MESSAGE_CREATE' if private else 'GROUP_AT_MESSAGE_CREATE','d':{
            'id':'TEST_INPUT','content':text,'author':{
                'user_openid' if private else 'member_openid':self.f.actor.native_user_id,'member_role':role},
            'msg_elements':[{'author':{'member_role':'owner'},'content':'TEST forwarded owner'}]}}
        if not private:raw['d']['group_openid'] = self.f.endpoint.native_conversation_id
        self.f.query('UPDATE qq_official_inbound_events SET payload_json=?,payload_expires_at=?,turn_text=?,turn_text_version=1 WHERE inbox_id=?',
            (json.dumps(raw),self.f.now-1 if expired else self.f.now+86400,text,self.f.inbox))
        app = SimpleNamespace(running_tasks=self.tasks,model_preferences=object(),reasoning_preferences=object(),usage_store=None,
            vision_worker=object() if available else None,media_library=object() if available else None)
        runtime = SimpleNamespace(agent_service=None,store=SimpleNamespace(admitted=self.f.admission,clock=lambda:self.f.now,
            authority=SimpleNamespace(owner_for_actor=lambda *_:object() if owner else None)),deliveries=self.deliveries,
            outbox=self.outbox,journal=self.journal,_calls=self.calls,sender=None,_token=self.f.token,_gateway=SimpleNamespace(state='ready'),
            loaded=SimpleNamespace(account=self.f.account,credentials=object(),delivery=SimpleNamespace(turn_policy=lambda:None)))
        with patch.object(turns,'PlatformChatService'),patch.object(turns,'build_platform_chat_orchestrator',return_value=self.orchestrator):
            consumer = turns.build_turn_consumer(runtime,self.f.token,app)
        self.assertEqual(await consumer(self.f.job),{'status':'handed_off'})
        self.agent.assert_not_awaited()
        audits = self.f.query("SELECT * FROM admin_audit_log WHERE action='conversation.config'")
        self.assertEqual((await consumer(self.f.job))['status'],'handed_off')
        self.assertEqual(self.f.query("SELECT * FROM admin_audit_log WHERE action='conversation.config'"),audits)
        return self.f.query('SELECT manifest_json FROM delivery_plans')[0]['manifest_json'],audits

    async def test_member_can_read_fixed_settings_without_mutation(self):
        text,audits = await self.exercise('/config')
        self.assertIn('agent_timeout_hours',text); self.assertIn('program_wake_limit',text)
        self.assertEqual(audits,[])
        self.assertEqual(self.f.query('SELECT * FROM conversation_settings'),[])

    async def test_member_and_forwarded_owner_cannot_write(self):
        text,audits = await self.exercise('/config agent_timeout_hours 1')
        self.assertIn('普通成员',text); self.assertEqual(audits,[])
        self.assertEqual(self.f.query('SELECT * FROM conversation_settings'),[])

    async def test_original_admin_can_change_a_fixed_value_once(self):
        text,audits = await self.exercise('/config agent_timeout_hours 1',role='admin')
        self.assertIn('已保存',text); self.assertEqual(len(audits),1)
        row = self.f.query('SELECT scope_key,agent_timeout_hours FROM conversation_settings')[0]
        self.assertEqual((row['scope_key'],row['agent_timeout_hours']),(self.f.endpoint.scope.key,1))
        self.assertNotIn(self.f.actor.native_user_id,audits[0]['actor'])

    async def test_change_while_stopped_never_clears_stop_or_command_generations(self):
        self.f.query('''INSERT INTO conversation_settings(scope_key,stopped,stop_generation,command_generation,program_say_limit)
            VALUES (?,TRUE,4,9,2)''',(self.f.endpoint.scope.key,))
        text,audits = await self.exercise('/config program_say_limit default',role='admin')
        self.assertIn('已保存',text); self.assertEqual(len(audits),1)
        row = self.f.query('SELECT stopped,stop_generation,command_generation,program_say_limit FROM conversation_settings')[0]
        self.assertEqual(tuple(row.values()),(True,4,9,60))

    async def test_budget_admin_write_is_exact_once_and_default_off_is_a_mutation(self):
        text,audits = await self.exercise('/config daily_budget_usd 0.001000',role='admin')
        self.assertIn('daily_budget_usd = $0.001',text); self.assertEqual(len(audits),1)
        self.assertEqual(audits[0]['resource_key'],'budgets')
        row = self.f.query('SELECT daily_budget_usd,budget_generation FROM conversation_settings')[0]
        self.assertEqual(str(row['daily_budget_usd']),'0.001000'); self.assertEqual(row['budget_generation'],1)

    async def test_budget_off_does_not_get_misinterpreted_as_a_read(self):
        self.f.query('INSERT INTO conversation_settings(scope_key,daily_budget_usd,budget_generation) VALUES (?,1,1)',(self.f.endpoint.scope.key,))
        text,audits = await self.exercise('/config daily_budget_usd default',role='admin')
        self.assertIn('已保存',text); self.assertIn('daily_budget_usd = off',text); self.assertEqual(len(audits),1)
        row = self.f.query('SELECT daily_budget_usd,budget_generation FROM conversation_settings')[0]
        self.assertIsNone(row['daily_budget_usd']); self.assertEqual(row['budget_generation'],2)

    async def test_budget_member_cannot_write_even_with_forwarded_owner(self):
        text,audits = await self.exercise('/config daily_budget_usd 0')
        self.assertIn('普通成员',text); self.assertEqual(audits,[])
        self.assertEqual(self.f.query('SELECT * FROM conversation_settings'),[])

    async def test_explicit_foreign_target_is_rejected_without_writing_any_scope(self):
        text,audits = await self.exercise('/config program_wake_limit 0 TEST_FOREIGN',role='admin')
        self.assertIn('用法',text); self.assertEqual(audits,[])
        self.assertEqual(self.f.query('SELECT * FROM conversation_settings'),[])

    async def test_expired_admin_evidence_cannot_change_settings(self):
        text,audits = await self.exercise('/config proactive_enabled off',role='admin',expired=True)
        self.assertIn('普通成员',text); self.assertEqual(audits,[])

    async def test_current_host_owner_may_change_even_without_platform_admin_role(self):
        text,audits = await self.exercise('/config program_say_limit 0',owner=True)
        self.assertIn('已保存',text); self.assertEqual(len(audits),1)
        self.assertEqual(self.f.query('SELECT program_say_limit FROM conversation_settings')[0]['program_say_limit'],0)

    async def test_invalid_input_never_reaches_the_store(self):
        text,audits = await self.exercise('/config agent_timeout_hours 7',role='admin')
        self.assertIn('用法',text); self.assertEqual(audits,[])
        self.assertEqual(self.f.query('SELECT * FROM conversation_settings'),[])

    async def test_missing_caption_service_is_explicit_and_never_enables_the_flag(self):
        text,audits = await self.exercise('/config image_captions on',role='admin')
        self.assertIn('未配置',text); self.assertEqual(audits,[])
        self.assertEqual(self.f.query('SELECT * FROM conversation_settings'),[])

    async def test_caption_service_allows_original_admin_to_enable_it(self):
        text,audits = await self.exercise('/config image_captions on',role='admin',available=True)
        self.assertIn('已保存',text); self.assertEqual(len(audits),1)
        self.assertTrue(self.f.query('SELECT image_captions FROM conversation_settings')[0]['image_captions'])


class PrivateFixture(base.fixture.ExecutionFixture):
    endpoint_kind = 'private'


class PrivateConfigTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = NativeConfigTests.asyncSetUp
    asyncTearDown = NativeConfigTests.asyncTearDown
    exercise = NativeConfigTests.exercise

    def setUp(self):
        with patch.object(base.fixture,'ExecutionFixture',PrivateFixture):base.ConsumerTests.setUp(self)

    async def test_private_budget_default_off_is_explicitly_writable(self):
        text,audits = await self.exercise('/config daily_budget_usd off')
        self.assertIn('已保存',text); self.assertEqual(len(audits),1)
        row = self.f.query('SELECT scope_key,daily_budget_usd FROM conversation_settings')[0]
        self.assertEqual(row['scope_key'],self.f.endpoint.scope.key); self.assertIsNone(row['daily_budget_usd'])

    async def test_admitted_private_peer_changes_only_own_physical_conversation(self):
        text,audits = await self.exercise('/config program_wake_limit 0')
        self.assertIn('已保存',text); self.assertEqual(len(audits),1)
        row = self.f.query('SELECT scope_key,program_wake_limit FROM conversation_settings')[0]
        self.assertEqual((row['scope_key'],row['program_wake_limit']),(self.f.endpoint.scope.key,0))
