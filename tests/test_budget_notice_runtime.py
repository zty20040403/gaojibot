"""Actual account runtime starts notice reconciliation without a model/VM call."""
import asyncio
import unittest

from src.plugins.ai_chat.conversation_config import change
from src.plugins.ai_chat.platform_types import EndpointKey
from tests import test_qq_official_turn_endtoend as base
from tests.test_qq_official_runtime_sender import verified_ready_platform


class BudgetNoticeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp = base.OfficialTurnEndToEndTests.setUp
    runtime = base.OfficialTurnEndToEndTests.runtime
    query = base.OfficialTurnEndToEndTests.query
    wait_db = base.OfficialTurnEndToEndTests.wait_db
    application = base.OfficialTurnEndToEndTests.application
    emit = base.OfficialTurnEndToEndTests.emit

    async def test_background_runtime_delivers_once_and_does_not_manufacture_model_usage(self):
        app = self.application()
        async with verified_ready_platform() as fake:
            runtime = self.runtime(loaded=base.runtime_fixture.loaded_config(fake.app_id,base=fake.base_url),
                application=app,ledger=app.message_ledger,journal=app.turn_journal)
            self.assertTrue(await runtime.start())
            await asyncio.wait_for(fake.wait_ready(),10)
            await self.wait_db("SELECT bot_native_id FROM qq_official_accounts WHERE bot_identity_source='ready'")
            await self.emit(fake,native='TEST_BUDGET_SETUP',content='TEST setup',group=True)
            # This first event discovers an unbound group; it is intentionally
            # not eligible for normal processing until the host activates it.
            await self.wait_db("SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id='TEST_BUDGET_SETUP'",timeout=20)
            endpoint = EndpointKey(runtime.loaded.account,'group','TEST_GROUP')
            with runtime.store.transaction() as cursor:
                cursor.execute("UPDATE qq_official_endpoints SET binding_state='active' WHERE endpoint_key=?",(endpoint.key,))
            fake.proactive_allowed[('group','TEST_GROUP')] = True
            await runtime.sender.refresh_bound_groups()
            with runtime.store.transaction() as cursor:
                change(cursor,endpoint,'daily_budget_usd','0',actor='TEST_ADMIN',now=int(runtime.store.clock()))
            await self.wait_db("SELECT notice_state FROM conversation_budget_days WHERE endpoint_key=? AND notice_state='committed'",(endpoint.key,),timeout=20)
            self.assertEqual(len(fake.received),1)
            self.assertIn('今日模型预算',fake.received[0]['payload']['content'])
            self.assertEqual(self.query('SELECT count(*) AS n FROM model_usage_calls')[0]['n'],0)
            self.assertEqual(self.query('SELECT count(*) AS n FROM program_quota')[0]['n'],0)
            await runtime.stop()
