"""Actual runtime ingress/model/outbox fences across a rapid off/on change."""
import asyncio
from datetime import datetime,timezone
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.conversation_config import change
from src.plugins.ai_chat.platform_types import EndpointKey
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.adapters.qq_official.native_background_factory import background_factory,ProactiveConfiguration
from tests import test_qq_official_turn_endtoend as base
from tests.test_qq_official_runtime_sender import verified_ready_platform


class ProactiveConfigTests(unittest.IsolatedAsyncioTestCase):
    setUp = base.OfficialTurnEndToEndTests.setUp
    runtime = base.OfficialTurnEndToEndTests.runtime
    query = base.OfficialTurnEndToEndTests.query
    wait_db = base.OfficialTurnEndToEndTests.wait_db
    application = base.OfficialTurnEndToEndTests.application
    emit = base.OfficialTurnEndToEndTests.emit

    async def test_off_skips_ingress_and_rapid_restore_never_sends_old_model_or_plan(self):
        app = self.application()
        entered,release,cancelled = asyncio.Event(),asyncio.Event(),asyncio.Event()
        async def completion(*args,**kwargs):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return {'reply':'TEST new proactive reply'}
        with patch('src.plugins.ai_chat.application.shared_proactive.ask_deepseek_json',side_effect=completion) as model, \
                patch('src.plugins.ai_chat.adapters.qq_official.proactive_ingress.DEBOUNCE_SECONDS',0):
            async with verified_ready_platform() as fake:
                runtime = self.runtime(loaded=base.runtime_fixture.loaded_config(fake.app_id,base=fake.base_url),
                    application=app,ledger=app.message_ledger,journal=app.turn_journal,
                    background_factory=background_factory(proactive_configuration=ProactiveConfiguration(enabled=True),proactive_profile='test'))
                self.assertTrue(await runtime.start())
                await asyncio.wait_for(fake.wait_ready(),10)
                await self.wait_db("SELECT bot_native_id FROM qq_official_accounts WHERE bot_identity_source='ready'")
                await self.emit(fake,native='TEST_SETUP',content='TEST setup',group=True)
                await self.wait_db("SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id='TEST_SETUP'")
                endpoint = EndpointKey(runtime.loaded.account,'group','TEST_GROUP')
                with runtime.store.transaction() as cursor:
                    cursor.execute("UPDATE qq_official_endpoints SET binding_state='active' WHERE endpoint_key=?",(endpoint.key,))
                fake.proactive_allowed[('group','TEST_GROUP')] = True
                await runtime.sender.refresh_bound_groups()
                def configure(value):
                    with runtime.store.transaction() as cursor:
                        change(cursor,endpoint,'proactive_enabled',value,actor='TEST_ADMIN',now=int(runtime.store.clock()))
                async def topic(native):
                    await fake.emit('GROUP_MESSAGE_CREATE',{'id':native,'content':'TEST conversation topic',
                        'timestamp':datetime.now(timezone.utc).isoformat(),
                        'group_openid':'TEST_GROUP','author':{'member_openid':'TEST_MEMBER'}})
                    return (await self.wait_db("SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id=? AND status='processed'",(native,),timeout=20))[0]['inbox_id']
                configure(False)
                disabled = await topic('TEST_DISABLED')
                configure(True)
                with runtime.store.transaction() as cursor,self.assertRaises(MigrationDenied):
                    runtime.background_service.proactive._load(cursor,disabled)
                self.assertEqual(model.call_count,0)
                pending = await topic('TEST_INFLIGHT')
                await asyncio.wait_for(entered.wait(),20)
                configure(False); configure(True)
                await asyncio.wait_for(cancelled.wait(),10)
                release.set()
                await self.wait_db("SELECT inbox_id FROM qq_official_proactive_observations WHERE inbox_id=? AND state='discarded'",(pending,),timeout=20)
                self.assertFalse(fake.received)
                fresh = await topic('TEST_FRESH')
                await self.wait_db("SELECT d.status FROM deliveries d JOIN qq_official_proactive_observations o USING(plan_id) WHERE o.inbox_id=? AND d.status='committed'",(fresh,),timeout=20)
                self.assertEqual(model.call_count,2)
                self.assertEqual(len(fake.received),1)
                configure(False); configure(True)
                with runtime.store.transaction() as cursor:
                    plan = cursor.execute('SELECT p.* FROM delivery_plans p JOIN qq_official_proactive_observations o USING(plan_id) WHERE o.inbox_id=?',(fresh,)).fetchone()
                    with self.assertRaises(MigrationDenied):
                        runtime.background_service.proactive.plan_guard(cursor,token=runtime._token,plan=plan,operation='send',now=int(runtime.store.clock()))
                # Policy changes cannot erase a real accepted platform receipt.
                self.assertEqual(self.query('SELECT status FROM deliveries')[0]['status'],'committed')
                await runtime.stop()
