"""Configured delivery is wired to real turns, sender gates and stored limits."""
import asyncio
from dataclasses import replace
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official.config import OfficialConfig
from src.plugins.ai_chat.adapters.qq_official.delivery_configuration import DeliveryConfiguration
from tests import test_qq_official_runtime as runtime_fixture
from tests import test_qq_official_turn_endtoend as turns
from tests.test_qq_official_runtime_sender import verified_ready_platform


class DeliveryConfigurationTests(unittest.TestCase):
    def test_disabled_ignores_invalid_dormant_delivery_options_without_reading(self):
        with patch('src.qq_official.credentials.os.open', side_effect=AssertionError('credential I/O')):
            value=OfficialConfig.from_env({'AI_QQ_OFFICIAL_ENABLED':'false',
                'AI_QQ_OFFICIAL_MAX_TEXT_CHARS':'invalid','AI_QQ_OFFICIAL_CERTIFICATION':'invalid',
                'AI_QQ_OFFICIAL_RATE_PASSIVE_PER_SECOND':'-1'})
            self.assertFalse(value.enabled)
            self.assertEqual(value.delivery,DeliveryConfiguration())

    def test_enabled_parsing_preserves_configured_values_and_renderer_byte_limit(self):
        config=OfficialConfig.from_env({'AI_QQ_OFFICIAL_ENABLED':'true','AI_QQ_OFFICIAL_MARKDOWN':'false',
            'AI_QQ_OFFICIAL_MAX_TEXT_CHARS':'300','AI_QQ_OFFICIAL_MAX_TEXT_PARTS':'2',
            'AI_QQ_OFFICIAL_MAX_TEXT_BYTES':'600','AI_QQ_OFFICIAL_DEFER_MAX_HOURS':'2',
            'AI_QQ_OFFICIAL_ALLOW_NEWER_WINDOWS':'true','AI_QQ_OFFICIAL_RATE_GROUP_PROACTIVE_PER_MINUTE':'5',
            'AI_QQ_OFFICIAL_FILE_LINK_TTL_SECONDS':'120'})
        policy=config.delivery.turn_policy()
        self.assertEqual((policy.markdown,policy.max_chars,policy.max_parts,policy.max_bytes),(False,300,2,600))
        self.assertTrue(policy.allow_newer_windows)
        self.assertEqual(config.delivery.max_wait_seconds,7200)
        self.assertEqual(config.delivery.file_link_ttl_seconds,120)

    def test_invalid_delivery_values_rejected_before_credentials(self):
        for name,values in {
            'MARKDOWN':['maybe'], 'MAX_TEXT_CHARS':['0','20001','true','nan','-1'],
            'MAX_TEXT_PARTS':['0','17'], 'MAX_TEXT_BYTES':['0','1048577'],
            'DEFER_MAX_HOURS':['0','25'], 'CERTIFICATION':['admin'],
            'FILE_LINK_TTL_SECONDS':['0','59','3601','true','1.5'],
        }.items():
            for value in values:
                with self.subTest(name=name,value=value),self.assertRaises(ValueError):
                    OfficialConfig.from_env({'AI_QQ_OFFICIAL_ENABLED':'true','AI_QQ_OFFICIAL_'+name:value})

class ConfiguredDeliveryRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp=runtime_fixture.OfficialRuntimeTests.setUp
    runtime=runtime_fixture.OfficialRuntimeTests.runtime
    query=runtime_fixture.OfficialRuntimeTests.query
    wait_db=runtime_fixture.OfficialRuntimeTests.wait_db
    application=turns.OfficialTurnEndToEndTests.application
    emit=turns.OfficialTurnEndToEndTests.emit

    async def test_actual_m2_help_turn_uses_plain_render_limits_and_shared_sender_caps(self):
        app=self.application()
        settings=DeliveryConfiguration.from_env({'AI_QQ_OFFICIAL_MARKDOWN':'false',
            'AI_QQ_OFFICIAL_MAX_TEXT_CHARS':'500','AI_QQ_OFFICIAL_MAX_TEXT_PARTS':'2',
            'AI_QQ_OFFICIAL_MAX_TEXT_BYTES':'1000','AI_QQ_OFFICIAL_DEFER_MAX_HOURS':'2',
            'AI_QQ_OFFICIAL_CERTIFICATION':'certified','AI_QQ_OFFICIAL_RATE_PASSIVE_PER_SECOND':'10'})
        async with verified_ready_platform() as fake:
            loaded=replace(runtime_fixture.loaded_config(fake.app_id,base=fake.base_url),delivery=settings)
            runtime=self.runtime(loaded=loaded,application=app,ledger=app.message_ledger,journal=app.turn_journal)
            self.assertTrue(await runtime.start());await asyncio.wait_for(fake.wait_ready(),5)
            await self.wait_db("SELECT bot_native_id FROM qq_official_accounts WHERE bot_identity_source='ready'")
            self.assertEqual(runtime.outbox.max_wait_seconds,7200)
            self.assertEqual(runtime.outbox.certification,'certified')
            self.assertEqual(runtime.turn_consumer.policy,settings.turn_policy())
            await self.emit(fake,native='TEST_CONFIGURED_HELP',content='/help')
            await self.wait_db('''SELECT x.execution_id FROM qq_official_turn_executions x
                WHERE x.plan_id IS NOT NULL AND EXISTS(SELECT 1 FROM deliveries d WHERE d.plan_id=x.plan_id)
                AND NOT EXISTS(SELECT 1 FROM deliveries d WHERE d.plan_id=x.plan_id AND d.status<>'committed')''')
            self.assertTrue(fake.received)
            self.assertLessEqual(len(fake.received),2)
            for received in fake.received:
                body=received['payload'];self.assertEqual(body['msg_type'],0)
                self.assertLessEqual(len(body['content']),500)
                self.assertLessEqual(len(body['content'].encode()),1000)
            rows=self.query('''SELECT x.deadline_at-i.received_at AS duration FROM qq_official_turn_executions x
                JOIN qq_official_inbound_events i ON i.inbox_id=x.inbox_id''')
            self.assertEqual(rows[0]['duration'],7200)
            await runtime.stop()
