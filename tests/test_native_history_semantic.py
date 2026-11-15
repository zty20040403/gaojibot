from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from src.plugins.ai_chat.llm_gateway import LLMGateway
from src.plugins.ai_chat.message_ir import MessageBody,TextNode
from src.plugins.ai_chat.message_vectors import REVISION,embedding_identity
from src.plugins.ai_chat.platform_types import ActorKey,EndpointKey
from src.plugins.ai_chat.semantic_recall import EmbeddingClient,PgVectorBackend,SemanticDocument,SemanticRecallService
from tests import test_native_billing as fixture
from tests.test_llm_gateway import FakeStream,stream_chunk
from tests.test_model_budget import completion
from tests.test_qq_official_runtime_sender import verified_ready_platform


class NativeHistorySemanticTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.NativeBillingTests.setUp
    application = fixture.NativeBillingTests.application
    open_runtime = fixture.NativeBillingTests.open_runtime
    runtime = fixture.NativeBillingTests.runtime
    query = fixture.NativeBillingTests.query
    wait_db = fixture.NativeBillingTests.wait_db
    emit = fixture.NativeBillingTests.emit
    complete = fixture.NativeBillingTests.complete

    async def test_actual_consumer_tool_loop_keeps_native_turn_and_actor_on_query_receipt(self):
        app = self.application(); app.settings = replace(app.settings,semantic_enabled=True)
        embedder = EmbeddingClient(base_url='https://embedding.example.invalid/v1',api_key='',model='TEST_EMBED',dimensions=1024,
            transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'data':[{'index':0,'embedding':[1]+[0]*1023}],
                'usage':{'prompt_tokens':6}})))
        backend = PgVectorBackend(self.database.dsn,dimensions=1024,schema=self.database.schema)
        app.semantic_recall = SemanticRecallService(embedder,backend)
        seen = []
        class Provider:
            async def create_completion(self,profile,**kwargs):
                seen.append(kwargs)
                if len(seen)==1:
                    chunk = stream_chunk()
                    chunk.choices[0].delta.tool_calls = [SimpleNamespace(index=0,id='TEST_HISTORY_TOOL',type='function',
                        function=SimpleNamespace(name='history',arguments='{"query":"怎么排版幻灯片","limit":1}'))]
                    return FakeStream(chunk,stream_chunk(finish='tool_calls'),completion(1000,50,600))
                return FakeStream(stream_chunk('TEST found SlidesForge'),stream_chunk(finish='stop'),completion(1000,50,600))
        app.llm_gateway = LLMGateway({'openai-chat':Provider()},catalog=app.model_catalog)
        with patch('src.plugins.ai_chat.agent_loop._runtime',return_value=(app.model_catalog,app.llm_gateway)):
            async with verified_ready_platform() as fake:
                runtime = await self.open_runtime(fake,app)
                try:
                    endpoint = EndpointKey(runtime.loaded.account,'private','TEST_RUNTIME_OWNER')
                    actor = ActorKey(runtime.loaded.account,'c2c_user','','TEST_RUNTIME_OWNER')
                    message = app.message_ledger.record_message(endpoint.scope,native_message_id='TEST_OLD_PRESENTATION',
                        sender_native_user_id=actor.native_user_id,sender_display='TEST owner',actor=actor,source_endpoint=endpoint,
                        body=MessageBody((TextNode(0,'TEST SlidesForge builds presentations'),)),occurred_at=1_700_000_000)
                    revision = self.query(f'SELECT {REVISION} AS revision FROM messages_with_captions m WHERE canonical_message_id=?',
                        (message.canonical_message_id,))[0]['revision']
                    backend.upsert(SemanticDocument(endpoint.scope.key,'message','msg#'+str(message.canonical_message_id),message.prompt_text,
                        {'source_revision':revision,'embedding_identity':embedding_identity(embedder)}),[1]+[0]*1023,model=embedder.model)
                    await self.emit(fake,native='TEST_SEMANTIC_QUESTION',content='TEST ask a paraphrase')
                    result = (await self.complete('TEST_SEMANTIC_QUESTION',timeout=15))[0]
                    rows = self.query("SELECT actor_key,turn_id,input_tokens FROM model_usage_calls WHERE profile='embedding' AND purpose='turn'")
                    self.assertEqual(rows,[dict(actor_key=actor.key,turn_id=result['journal_turn_id'],input_tokens=6)])
                    tool_results = [item for item in seen[-1]['messages'] if item.get('role')=='tool']
                    self.assertTrue(any('SlidesForge' in str(item) and 'results' in str(item) for item in tool_results))
                finally:await runtime.stop()
