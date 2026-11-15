"""Actual foreground/target factories, original TxA, tools and billing on PG."""
import asyncio
from dataclasses import replace
from datetime import datetime,timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

from src.plugins.ai_chat.billing_context import active_billing
from src.plugins.ai_chat.llm_gateway import LLMGateway
from src.plugins.ai_chat.platform_types import BackgroundToolSource,ProgramToolSource,QuestionToolSource
from src.plugins.ai_chat.tools import neutral_registry_for
from src.plugins.ai_chat.tools.state import ToolContext
from tests import test_native_billing as native
from tests import test_qq_official_runtime as fixture
from tests.test_agent_kernel import _tool_response,_final
from tests.test_llm_gateway import FakeStream,stream_chunk
from tests.test_model_budget import completion
from tests.test_qq_official_runtime_sender import verified_ready_platform

QUESTION,ANSWER = 'TEST_FRONT_PRIVATE_QUESTION','TEST_FRONT_PRIVATE_ANSWER'


class NativeQuestionAskTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.OfficialRuntimeTests.setUp
    runtime = fixture.OfficialRuntimeTests.runtime
    query = fixture.OfficialRuntimeTests.query
    wait_db = fixture.OfficialRuntimeTests.wait_db
    application = native.NativeBillingTests.application
    complete = native.NativeBillingTests.complete

    async def emit(self,fake,text,*,actor='TEST_RUNTIME_OWNER'):
        self.sequence = getattr(self,'sequence',0)+1; native_id = 'TEST_FRONT_EVENT_'+str(self.sequence)
        await fake.emit('C2C_MESSAGE_CREATE',dict(id=native_id,author=dict(user_openid=actor),content=text,timestamp=datetime.now(timezone.utc).isoformat()))
        await self.wait_db('SELECT * FROM qq_official_inbound_events WHERE native_message_id=? AND status=\'processed\'',(native_id,))
        return native_id

    async def run_case(self,*,linked=True,stopped=False,refused=False):
        app = self.application(); source_calls,target_calls = [],[]; reference = 'a'*32
        observations = []
        from src.plugins.ai_chat.adapters.qq_official.question_runtime import QuestionRuntime
        original_frontend = QuestionRuntime.frontend
        async def observe(service,inbound,platform):
            session = await original_frontend(service,inbound,platform)
            observations.append((inbound,platform,session)); return session
        class Provider:
            async def create_completion(_self,profile,**kwargs):
                billing = active_billing(); self.assertIsNotNone(billing)
                if billing.source.purpose=='conversation_question':
                    target_calls.append(kwargs)
                    self.assertIsNone(billing.source.actor)
                    response = (_tool_response('history',{'query':'TEST_TARGET_DINNER_ONLY'},'TEST_TARGET_READ') if len(target_calls)==1
                        else _tool_response('ask',{'action':'refuse'} if refused else {'action':'answer','answer':ANSWER},'TEST_TARGET_ANSWER')
                        if len(target_calls)==2 else _final('TEST_PLAIN_NOT_AN_ANSWER'))
                    response.usage = completion(20,10,5).usage; return response
                if billing.source.actor.native_user_id=='TEST_TARGET_OWNER':
                    if linked:self.assertNotIn(reference,next(tool['function']['description'] for tool in kwargs['tools'] if tool['function']['name']=='ask'))
                    return FakeStream(stream_chunk('TEST_TARGET_ACK'),stream_chunk(finish='stop'),completion(20,10,5))
                source_calls.append(kwargs)
                if len(source_calls)==1:
                    definition = next(tool['function'] for tool in kwargs['tools'] if tool['function']['name']=='ask')
                    self.assertNotIn('TEST_TARGET_OWNER',definition['description'])
                    if linked and not stopped:self.assertIn(reference,definition['description'])
                    elif stopped:self.assertNotIn(reference,definition['description'])
                    inbound,platform,session = observations[-1]; service = session.service
                    with self.assertRaisesRegex(PermissionError,'question_source_binding_required'):
                        await original_frontend(service,inbound,SimpleNamespace(inbound=inbound,execution=platform.execution))
                    original = platform.execution
                    try:
                        platform.execution = replace(original,token=replace(original.token))
                        with self.assertRaisesRegex(PermissionError,'question_source_binding_required'):await original_frontend(service,inbound,platform)
                    finally:platform.execution = original
                    try:
                        platform.inbound = replace(inbound,native_message_id='TEST_FOREIGN_NATIVE',route_epoch=inbound.route_epoch+1)
                        with self.assertRaisesRegex(PermissionError,'question_source_binding_required'):await original_frontend(service,platform.inbound,platform)
                        from src.plugins.ai_chat.tools.questions import available
                        self.assertFalse(available(ToolContext(SimpleNamespace(context=app),inbound=platform.inbound,platform=platform,question_session=session)))
                    finally:platform.inbound = inbound
                    chunk = stream_chunk('TEST private query planning '+QUESTION)
                    chunk.choices[0].delta.tool_calls = [SimpleNamespace(index=0,id='TEST_SOURCE_ASK',type='function',
                        function=SimpleNamespace(name='ask',arguments=json.dumps(dict(target=reference,question=QUESTION))))]
                    return FakeStream(chunk,stream_chunk(finish='tool_calls'),completion(20,10,5))
                return FakeStream(stream_chunk('TEST_SOURCE_AUTHENTICATED_SUMMARY'),stream_chunk(finish='stop'),completion(20,10,5))
        app.llm_gateway = LLMGateway({'openai-chat':Provider()},catalog=app.model_catalog)
        with patch('src.plugins.ai_chat.agent_loop._runtime',return_value=(app.model_catalog,app.llm_gateway)), \
                patch.object(QuestionRuntime,'frontend',new=observe):
            async with verified_ready_platform() as fake:
                fake.app_id = 'TEST_NATIVE_ASK_'+self._testMethodName
                loaded = replace(fixture.loaded_config(fake.app_id,base=fake.base_url),owner_openids=('TEST_RUNTIME_OWNER','TEST_TARGET_OWNER'))
                runtime = self.runtime(loaded=loaded,application=app,ledger=app.message_ledger,journal=app.turn_journal)
                await runtime.start(); await asyncio.wait_for(fake.wait_ready(),5)
                try:
                    if linked:
                        with patch('src.plugins.ai_chat.adapters.qq_official.linking.secrets.randbelow',return_value=654321):issued = await self.emit(fake,'/link')
                        await self.wait_db('SELECT * FROM deliveries WHERE reply_to_native_message_id=? AND status=\'committed\'',(issued,))
                        confirmed = await self.emit(fake,'/link 654321',actor='TEST_TARGET_OWNER')
                        await self.wait_db('SELECT * FROM deliveries WHERE reply_to_native_message_id=? AND status=\'committed\'',(confirmed,))
                        reference = self.query("SELECT link_id FROM qq_official_conversation_links WHERE state='active'")[0]['link_id']
                    seeded = await self.emit(fake,'TEST_TARGET_DINNER_ONLY',actor='TEST_TARGET_OWNER'); await self.complete(seeded,timeout=15)
                    if stopped:
                        stop = await self.emit(fake,'/stop',actor='TEST_TARGET_OWNER')
                        await self.wait_db('SELECT * FROM deliveries WHERE reply_to_native_message_id=? AND status=\'committed\'',(stop,))
                    source = await self.emit(fake,'TEST_SOURCE_PRIVATE_ONLY'); finished = (await self.complete(source,timeout=20))[0]
                    await runtime.stop()
                    self.assertEqual(len(source_calls),2)
                    self.assertNotIn('TEST_TARGET_DINNER_ONLY',repr(source_calls[0]['messages']))
                    rows = self.query('SELECT * FROM qq_official_conversation_questions')
                    if linked and not stopped:
                        self.assertEqual(len(rows),1); row = rows[0]
                        self.assertEqual(row['state'],'refused' if refused else 'answered'); self.assertIsNotNone(row['consumed_at'])
                        self.assertEqual(row['source_execution_id'],finished['execution_id']); self.assertEqual(len(target_calls),3)
                        for call in target_calls:
                            self.assertIn('TEST_TARGET_DINNER_ONLY',repr(call['messages'])); self.assertNotIn('TEST_SOURCE_PRIVATE_ONLY',repr(call['messages']))
                            self.assertEqual({tool['function']['name'] for tool in call['tools']},{'history','ask'})
                        if not refused:self.assertIn(ANSWER,repr(source_calls[-1]['messages']))
                    else:
                        self.assertEqual(rows,[]); self.assertEqual(target_calls,[])
                        self.assertIn('target_stopped' if stopped else 'not_linked',repr(source_calls[-1]['messages']))
                    self.assertEqual(self.query('SELECT * FROM turn_requests WHERE turn_id=?',(finished['journal_turn_id'],)),[])
                    normal = repr(self.query('SELECT * FROM turn_journal_events WHERE turn_id=?',(finished['journal_turn_id'],)))
                    for archived in self.query('SELECT payload FROM turn_archives WHERE turn_id=?',(finished['journal_turn_id'],)):
                        trace = json.loads(zlib.decompress(bytes(archived['payload']))); self.assertEqual(trace['messages'],[]); normal += repr(trace)
                    for value in (QUESTION,ANSWER):self.assertNotIn(value,normal); self.assertNotIn(value,repr(fake.received))
                    calls = self.query('SELECT * FROM model_usage_calls WHERE turn_id=?',(finished['journal_turn_id'],))
                    self.assertEqual(len(calls),2); self.assertTrue(all(call['purpose']=='turn' and call['actor_key'] for call in calls))
                    targets = self.query("SELECT * FROM model_usage_calls WHERE purpose='conversation_question'")
                    self.assertEqual(len(targets),3 if linked and not stopped else 0); self.assertTrue(all(call['actor_key'] is None for call in targets))
                    self.assertTrue(all(call['finished_at'] for call in calls+targets))
                    self.assertEqual({row['binding_state'] for row in self.query('SELECT binding_state FROM qq_official_endpoints')},{'unbound'})
                    for seen_inbound,_seen_platform,seen_session in observations:
                        if seen_inbound.actor.native_user_id=='TEST_TARGET_OWNER':self.assertEqual(seen_session.references,())
                    inbound,platform,session = observations[-1]; self.assertIs(session.binding,platform.execution)
                    # Sharing the original foreground session as non-human metadata
                    # cannot expose recursive source ask in any other tool surface.
                    for metadata in (ProgramToolSource(inbound.endpoint,'TEST_PROGRAM','TEST_RUN'),
                            QuestionToolSource(inbound.endpoint,'CQ-'+'b'*32,inbound.scope),
                            BackgroundToolSource(inbound.endpoint,inbound.actor,'subagent','TEST_JOB',1)):
                        field = 'program_source' if isinstance(metadata,ProgramToolSource) else 'question_source' if isinstance(metadata,QuestionToolSource) else 'background_source'
                        context = ToolContext(SimpleNamespace(context=app),platform=platform,question_session=session,**{field:metadata})
                        self.assertNotIn('ask',{tool['function']['name'] for tool in neutral_registry_for(context).definitions(context)})
                finally:await runtime.stop()

    async def test_actual_default_private_source_and_target_use_grounded_link_then_consume_one_private_answer(self):await self.run_case()
    async def test_target_explicit_refusal_returns_to_original_source_not_as_public_answer(self):await self.run_case(refused=True)
    async def test_missing_directed_link_is_explicit_and_never_wakes_or_enumerates_target(self):await self.run_case(linked=False)
    async def test_actual_target_stop_refuses_before_question_or_target_model(self):await self.run_case(stopped=True)
