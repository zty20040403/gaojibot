"""Real consumer/proactive factories + PostgreSQL/FakePlatform + local provider."""
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.llm_gateway import LLMGateway
from src.plugins.ai_chat.model_billing import ModelBilling
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.model_pricing import ModelPricing
from src.plugins.ai_chat.platform_types import EndpointKey
from src.plugins.ai_chat.adapters.qq_official.native_background_factory import background_factory,ProactiveConfiguration
from tests import test_qq_official_turn_endtoend as fixture
from tests.test_qq_official_runtime_sender import verified_ready_platform
from tests.test_llm_gateway import FakeStream,stream_chunk
from tests.test_model_budget import completion


class NativeBillingTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.OfficialTurnEndToEndTests.setUp
    runtime = fixture.OfficialTurnEndToEndTests.runtime
    query = fixture.OfficialTurnEndToEndTests.query
    wait_db = fixture.OfficialTurnEndToEndTests.wait_db
    open_runtime = fixture.OfficialTurnEndToEndTests.open_runtime
    emit = fixture.OfficialTurnEndToEndTests.emit
    complete = fixture.OfficialTurnEndToEndTests.complete

    def application(self,*,silent=False):
        app = fixture.OfficialTurnEndToEndTests.application(self)
        app.model_billing = ModelBilling(self.database)
        selected = replace(app.model_catalog.resolve('test'),pricing=ModelPricing('2','0.2','8'))
        app.model_catalog = ModelCatalog({'test':selected},default_profile='test')
        class Provider:
            async def create_completion(self,profile,**kwargs):
                if kwargs.get('stream'):
                    return FakeStream(stream_chunk('TEST final'),stream_chunk(finish='stop'),completion(1000,50,600))
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"reply":""}' if silent else 'TEST final',
                    tool_calls=[]))],usage=completion(1000,50,600).usage)
        app.llm_gateway = LLMGateway({'openai-chat':Provider()},catalog=app.model_catalog)
        return app

    async def test_actual_private_and_aliased_group_turns_keep_physical_scope_actor_and_turn(self):
        app = self.application()
        with patch('src.plugins.ai_chat.agent_loop._runtime',return_value=(app.model_catalog,app.llm_gateway)):
            async with verified_ready_platform() as fake:
                runtime = await self.open_runtime(fake,app)
                await self.emit(fake,native='TEST_BILL_PRIVATE',content='TEST question')
                first = (await self.complete('TEST_BILL_PRIVATE'))[0]
                await self.emit(fake,native='TEST_BILL_SETUP',content='TEST setup',group=True)
                await self.wait_db("SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id='TEST_BILL_SETUP'")
                endpoint = EndpointKey(runtime.loaded.account,'group','TEST_GROUP')
                history = EndpointKey(runtime.loaded.account,'group','TEST_HISTORY')
                with runtime.store.transaction() as cursor:
                    cursor.execute("UPDATE qq_official_endpoints SET binding_state='active',canonical_scope_key=?,route_epoch=route_epoch+1 WHERE endpoint_key=?",
                        (history.scope.key,endpoint.key))
                await self.emit(fake,native='TEST_BILL_GROUP',content='TEST question',group=True)
                second = (await self.complete('TEST_BILL_GROUP'))[0]
                rows = self.query('SELECT * FROM model_usage_calls ORDER BY started_at,call_id')
                self.assertEqual(len(rows),2)
                self.assertEqual({row['turn_id'] for row in rows},{first['journal_turn_id'],second['journal_turn_id']})
                grouped = next(row for row in rows if row['endpoint_key']==endpoint.key)
                self.assertEqual(grouped['scope_key'],endpoint.scope.key)
                self.assertNotEqual(grouped['scope_key'],history.scope.key)
                self.assertTrue(all(row['actor_key'] and row['cost_complete'] and row['cost_usd']==Decimal('0.00132') for row in rows))
                await runtime.stop()

    async def test_silent_proactive_classification_still_records_its_real_cost(self):
        app = self.application(silent=True)
        with patch('src.plugins.ai_chat.agent_loop._runtime',return_value=(app.model_catalog,app.llm_gateway)):
            async with verified_ready_platform() as fake:
                runtime = self.runtime(loaded=fixture.runtime_fixture.loaded_config(fake.app_id,base=fake.base_url),application=app,
                    ledger=app.message_ledger,journal=app.turn_journal,
                    background_factory=background_factory(proactive_configuration=ProactiveConfiguration(enabled=True),proactive_profile='test'))
                self.assertTrue(await runtime.start())
                await fake.wait_ready()
                await self.emit(fake,native='TEST_PROACTIVE_BILL_SETUP',content='TEST setup',group=True)
                await self.wait_db("SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id='TEST_PROACTIVE_BILL_SETUP'")
                endpoint = EndpointKey(runtime.loaded.account,'group','TEST_GROUP')
                with runtime.store.transaction() as cursor:
                    cursor.execute("UPDATE qq_official_endpoints SET binding_state='active' WHERE endpoint_key=?",(endpoint.key,))
                from datetime import datetime,timezone
                await fake.emit('GROUP_MESSAGE_CREATE',{'id':'TEST_PROACTIVE_COST','content':'TEST conversation topic worth considering',
                    'timestamp':datetime.now(timezone.utc).isoformat(),'group_openid':'TEST_GROUP','author':{'member_openid':'TEST_MEMBER'}})
                rows = await self.wait_db("SELECT * FROM model_usage_calls WHERE purpose='proactive' AND finished_at IS NOT NULL",timeout=10)
                self.assertEqual(len(rows),1)
                self.assertEqual((rows[0]['scope_key'],rows[0]['cost_usd']),(endpoint.scope.key,Decimal('0.00132')))
                self.assertTrue(rows[0]['cost_complete'])
                self.assertTrue(rows[0]['actor_key'])
                await runtime.stop()
