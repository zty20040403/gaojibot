"""Real QQ runtime owns the optional index worker; provider remains a local fake."""
import asyncio
from dataclasses import replace
import json
import unittest

import httpx

from src.plugins.ai_chat.model_billing import ModelBilling
from src.plugins.ai_chat.platform_types import EndpointKey
from src.plugins.ai_chat.semantic_recall import EmbeddingClient,PgVectorBackend,SemanticRecallService
from tests import test_qq_official_turn_endtoend as base
from tests.test_qq_official_runtime_sender import verified_ready_platform


class MessageVectorRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp = base.OfficialTurnEndToEndTests.setUp
    runtime = base.OfficialTurnEndToEndTests.runtime
    query = base.OfficialTurnEndToEndTests.query
    wait_db = base.OfficialTurnEndToEndTests.wait_db
    application = base.OfficialTurnEndToEndTests.application
    emit = base.OfficialTurnEndToEndTests.emit

    async def test_real_lease_runtime_builds_and_stops_indexing_without_qq_or_vm_effects(self):
        app = self.application(); app.settings = replace(app.settings,semantic_enabled=True)
        app.model_billing = ModelBilling(self.database)
        calls = []
        def provider(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200,json={'data':[{'index':0,'embedding':[1]+[0]*1023}],'usage':{'prompt_tokens':21}})
        app.semantic_recall = SemanticRecallService(EmbeddingClient(base_url='https://embedding.example.invalid/v1',api_key='',
            model='TEST_EMBED',dimensions=1024,transport=httpx.MockTransport(provider)),
            PgVectorBackend(self.database.dsn,dimensions=1024,schema=self.database.schema))
        async def consume(_job):pass  # Suppress foreground model calls, not the index factory.
        async with verified_ready_platform() as fake:
            runtime = self.runtime(loaded=base.runtime_fixture.loaded_config(fake.app_id,base=fake.base_url),application=app,
                ledger=app.message_ledger,journal=app.turn_journal,turn_consumer_factory=lambda *_:consume)
            self.assertTrue(await runtime.start())
            try:
                await self.wait_db("SELECT bot_native_id FROM qq_official_accounts WHERE bot_identity_source='ready'",timeout=15)
                await self.emit(fake,native='TEST_VECTOR_DISCOVER',content='TEST initial discovery',group=True)
                await self.wait_db("SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id='TEST_VECTOR_DISCOVER'",timeout=15)
                endpoint = EndpointKey(runtime.loaded.account,'group','TEST_GROUP')
                with runtime.store.transaction() as cursor:
                    cursor.execute("UPDATE qq_official_endpoints SET binding_state='active' WHERE endpoint_key=?",(endpoint.key,))
                await self.emit(fake,native='TEST_VECTOR_RECOMMENDATION',content='TEST recommend a slideshow tool',group=True)
                await self.wait_db("SELECT source_handle FROM semantic_documents WHERE source_type='message'",timeout=25)
                receipts = self.query("SELECT input_tokens,finished_at FROM model_usage_calls WHERE purpose='semantic_index'")
                self.assertTrue(receipts); self.assertTrue(all(row['input_tokens']==21 and row['finished_at'] for row in receipts))
                self.assertTrue(any('TEST recommend a slideshow tool' in ' '.join(call['input']) for call in calls))
                self.assertEqual(fake.received,[])
            finally:await runtime.stop()
            count = len(calls)
            self.assertEqual(runtime.state,'stopped')
            await asyncio.sleep(.05)
            self.assertEqual(len(calls),count)
