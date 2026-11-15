"""Real source consumer/Gateway/journal redaction; injected tool is not ask mount."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

from src.plugins.ai_chat.llm_gateway import LLMGateway
from src.plugins.ai_chat.tools.registry import FunctionTool,ToolRegistry
from tests import test_native_billing as fixture
from tests.test_llm_gateway import FakeStream,stream_chunk
from tests.test_model_budget import completion
from tests.test_qq_official_runtime_sender import verified_ready_platform

QUESTION = 'TEST_PRIVATE_QUESTION_BODY'
ANSWER = 'TEST_PRIVATE_ANSWER_BODY'


def definition(name):
    return {'type':'function','function':{'name':name,'description':'TEST injected private exchange',
        'parameters':{'type':'object','properties':{'target':{'type':'string'},'question':{'type':'string'}},
            'required':['target','question'],'additionalProperties':False}}}


class QuestionSourcePrivacyTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.NativeBillingTests.setUp
    application = fixture.NativeBillingTests.application
    open_runtime = fixture.NativeBillingTests.open_runtime
    runtime = fixture.NativeBillingTests.runtime
    query = fixture.NativeBillingTests.query
    wait_db = fixture.NativeBillingTests.wait_db
    emit = fixture.NativeBillingTests.emit
    complete = fixture.NativeBillingTests.complete

    async def run_private(self,*,fail=False):
        app = self.application(); seen = []
        class Provider:
            async def create_completion(self,profile,**kwargs):
                seen.append(deepcopy(kwargs))
                if len(seen)==1:
                    chunk = stream_chunk(QUESTION)
                    chunk.choices[0].delta.tool_calls = [SimpleNamespace(index=0,id='TEST_PRIVATE_ASK',type='function',
                        function=SimpleNamespace(name='ask',arguments=json.dumps({'target':'0'*32,'question':QUESTION})))]
                    return FakeStream(chunk,stream_chunk(finish='tool_calls'),completion(1000,50,600))
                if fail:raise RuntimeError('TEST_PROVIDER_FAILED '+ANSWER)
                if len(seen)==2:
                    chunk = stream_chunk('TEST tainted followup')
                    chunk.choices[0].delta.tool_calls = [SimpleNamespace(index=0,id='TEST_PRIVATE_FOLLOWUP',type='function',
                        function=SimpleNamespace(name='inspect_source',arguments=json.dumps({'target':'0'*32,'question':ANSWER})))]
                    return FakeStream(chunk,stream_chunk(finish='tool_calls'),completion(1000,50,600))
                return FakeStream(stream_chunk('TEST authenticated final summary'),stream_chunk(finish='stop'),completion(1000,50,600))
        app.llm_gateway = LLMGateway({'openai-chat':Provider()},catalog=app.model_catalog)
        async def exchange(_turn,_arguments):return json.dumps({'answer':ANSWER})
        catalog = ToolRegistry(FunctionTool(definition(name),lambda _:True,exchange,platform_neutral=True)
            for name in ('ask','inspect_source'))
        with patch('src.plugins.ai_chat.tools._base_registry',return_value=catalog), \
                patch('src.plugins.ai_chat.agent_loop._runtime',return_value=(app.model_catalog,app.llm_gateway)):
            async with verified_ready_platform() as fake:
                runtime = await self.open_runtime(fake,app)
                try:
                    await self.emit(fake,native='TEST_PRIVATE_SOURCE_TURN',content='TEST host-approved source request')
                    rows = await self.complete('TEST_PRIVATE_SOURCE_TURN',timeout=15)
                    if fail:
                        self.assertEqual(self.query('SELECT status FROM agent_turns WHERE turn_id=?',
                            (rows[0]['journal_turn_id'],))[0]['status'],'crashed')
                    # Drain ordinary source task/DB finalizers before querying.
                    await runtime.stop()
                    self.assertEqual(self.query('SELECT COUNT(*) AS n FROM turn_requests')[0]['n'],0)
                    events = self.query('SELECT * FROM turn_journal_events')
                    self.assertTrue(any(e['tool_name']=='ask' for e in events))
                    text = repr(events)+repr(self.query('SELECT * FROM agent_turns'))
                    for archived in self.query('SELECT payload FROM turn_archives'):
                        trace = json.loads(zlib.decompress(bytes(archived['payload'])))
                        self.assertEqual(trace['messages'],[]); self.assertEqual(trace['execution_decisions'],[])
                        self.assertGreater(trace['usage']['input_tokens'],0)
                        text += repr(trace)
                    for value in (QUESTION,ANSWER):
                        self.assertNotIn(value,text)
                        self.assertNotIn(value,repr(fake.received))
                    self.assertTrue(all(e['input_json'] in {'','{}'} for e in events if e['tool_name']))
                    calls = self.query('SELECT * FROM model_usage_calls')
                    self.assertTrue(calls); self.assertTrue(all(c['actor_key'] is not None and c['purpose']=='turn' for c in calls))
                    self.assertTrue(all(c['turn_id']==rows[0]['journal_turn_id'] for c in calls))
                    self.assertIn(ANSWER,json.dumps(seen[1]['messages']))
                    return seen
                finally:await runtime.stop()

    async def test_private_batch_note_arguments_followup_result_trace_and_request_are_not_logged(self):
        seen = await self.run_private(); self.assertEqual(len(seen),3)
        self.assertIn(QUESTION,json.dumps(seen[1]['messages']))
        self.assertIn(ANSWER,json.dumps(seen[2]['messages']))

    async def test_provider_failure_after_private_tool_still_redacts_and_finishes_real_usage(self):
        seen = await self.run_private(fail=True); self.assertEqual(len(seen),2)

    async def test_non_private_turn_keeps_normal_request_and_trace_body_logging(self):
        app = self.application()
        with patch('src.plugins.ai_chat.agent_loop._runtime',return_value=(app.model_catalog,app.llm_gateway)):
            async with verified_ready_platform() as fake:
                runtime = await self.open_runtime(fake,app)
                try:
                    await self.emit(fake,native='TEST_NORMAL_LOGGING',content='TEST normal request')
                    await self.complete('TEST_NORMAL_LOGGING',timeout=15); await runtime.stop()
                    requests = self.query('SELECT * FROM turn_requests'); self.assertEqual(len(requests),1)
                    self.assertIn('TEST normal request',requests[0]['messages_json'])
                    archives = self.query('SELECT payload FROM turn_archives'); self.assertEqual(len(archives),1)
                    self.assertIn('TEST final',repr(json.loads(zlib.decompress(bytes(archives[0]['payload'])))))
                finally:await runtime.stop()
