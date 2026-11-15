"""Private target tool/model composition on real PG, not production acceptance."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

import httpx

from src.bot_storage.async_calls import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official import turn_source
from src.plugins.ai_chat.adapters.qq_official.billing import scoped
from src.plugins.ai_chat.background_tools import BackgroundPlatform
from src.plugins.ai_chat.adapters.qq_official.conversation_control import change
from src.plugins.ai_chat.adapters.qq_official.link_ingress import LinkIngress
from src.plugins.ai_chat.adapters.qq_official.question_authority import QuestionAuthority
from src.plugins.ai_chat.adapters.qq_official.question_model import QuestionModel
from src.plugins.ai_chat.adapters.qq_official.question_worker import QuestionWorker,QuestionTarget
from src.plugins.ai_chat.adapters.qq_official.questions import Questions
from src.plugins.ai_chat.adapters.qq_official.turn_platform import ExecutionBinding
from src.plugins.ai_chat.agent_loop import request_sink
from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.conversation_window import ConversationStore
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.llm_gateway import LLMGateway
from src.plugins.ai_chat.message_ir import MessageBody,TextNode
from src.plugins.ai_chat.message_vectors import REVISION,embedding_identity
from src.plugins.ai_chat.model_billing import ModelBilling
from src.plugins.ai_chat.model_catalog import ModelCatalog,ModelProfile
from src.plugins.ai_chat.platform_types import ActorKey,ProgramToolSource,QuestionToolSource
from src.plugins.ai_chat.quota import UsageStore
from src.plugins.ai_chat.semantic_recall import EmbeddingClient,PgVectorBackend,SemanticDocument,SemanticRecallService
from src.plugins.ai_chat.tools import neutral_registry_for
from src.plugins.ai_chat.tools.state import ToolContext
from tests.conversation_question_fixture import QuestionFixture
from tests.test_agent_kernel import _tool_response,_final
from tests.test_model_budget import completion


class QuestionModelTests(QuestionFixture,unittest.IsolatedAsyncioTestCase):
    link_approver = 'TEST_LINK_APPROVER'

    def setUp(self):
        super().setUp(); clock = patch('time.time',side_effect=lambda:self.now); clock.start(); self.addCleanup(clock.stop)
        self.calls = DatabaseCalls(); self.addAsyncCleanup(self.calls.close)
        self.ledger,self.store,self.usage = MessageLedger(self.database),ConversationStore(self.database),UsageStore(self.database)
        for store in (self.ledger,self.store,self.usage):self.addCleanup(store.close)
        authority = QuestionAuthority(LinkIngress(self.auth,admission=self.admission),admission=self.admission)
        self.questions = Questions(authority,self.jobs); self.binding = ExecutionBinding(turn_source,self.token,self.handle,self.job,self.admission)
        profile = ModelProfile('TEST_TARGET_MODEL','openai-chat','openai-chat','TEST_TARGET_MODEL','https://model.example.invalid',
            api_key='TEST_KEY',max_input_tokens=32768,max_output_tokens=1024)
        self.preferences = SimpleNamespace(get_scope_default=Mock(return_value=profile.name),get_scope_enabled_override=Mock(return_value=True))
        self.app = SimpleNamespace(settings=replace(settings,semantic_enabled=False),model_billing=ModelBilling(self.database,clock=lambda:self.now),
            db_call=self.calls,message_ledger=self.ledger,conversations=self.store,usage_store=self.usage,
            model_catalog=ModelCatalog({profile.name:profile},default_profile=profile.name),model_preferences=self.preferences)
        self.runtime = SimpleNamespace(application=self.app,jobs=self.jobs,_calls=self.calls,logger=Mock(),store=SimpleNamespace(transaction=self.tx))
        self.model = QuestionModel(self.runtime,self.token,self.questions); self.worker = QuestionWorker(self.runtime,self.token,self.questions,self.model)
        self.provider = SimpleNamespace(create_completion=AsyncMock()); self.gateway = LLMGateway({'openai-chat':self.provider},catalog=self.app.model_catalog)
        for endpoint,text in ((self.endpoint,'TEST_SOURCE_SECRET'),(self.target,'TEST_TARGET_DINNER')):
            actor = ActorKey(self.account,'group_member',endpoint.native_conversation_id,'TEST_READER')
            message = self.ledger.record_message(endpoint.scope,native_message_id=text,sender_native_user_id=actor.native_user_id,
                sender_display='TEST_SPEAKER',actor=actor,source_endpoint=endpoint,body=MessageBody((TextNode(0,text),)),occurred_at=self.now-10)
            if endpoint==self.target:self.target_message = message
            self.store.write_memory(endpoint.scope.key,text+'_MEMORY')

    def submit(self):
        with self.tx() as cursor:return self.questions.submit(cursor,self.token,self.binding,self.edge['link_id'],'TEST_PRIVATE_QUESTION')[0]

    def row(self,row):return self.query('SELECT * FROM qq_official_conversation_questions WHERE question_id=?',(row['question_id'],))[0]

    async def run_worker(self):
        with patch('src.plugins.ai_chat.agent_loop._runtime',return_value=(self.app.model_catalog,self.gateway)):
            return await self.worker.worker.run_once()

    def claimed_target(self):
        self.submit(); job = self.jobs.claim_due(self.worker.worker.worker_id,kinds=self.worker.worker.registered_kinds,now=self.now)[0]
        running,data = self.worker.claim(job)
        async def current():await self.calls(self.worker.current,job)
        return QuestionTarget(self.target,running['target_scope_key'],running['question_id'],data['question'],data['source_label'],current)

    def response(self,name=None,arguments=None):
        reply = _tool_response(name,arguments,'TEST_CALL_'+name) if name else _final('TEST_PLAIN_NOT_PUBLIC')
        reply.usage = completion(20,10,5).usage
        return reply

    async def test_actual_tool_loop_reads_only_target_and_prepares_private_answer_not_plain_or_public_output(self):
        self.provider.create_completion.side_effect = [self.response('history',{'query':'TEST_TARGET_DINNER'}),
            self.response('ask',{'action':'answer','answer':'TEST_PRIVATE_SUMMARY'}),self.response()]
        row = self.submit(); leaked = Mock(); sink = request_sink.set(leaked)
        try:self.assertEqual(await self.run_worker(),1)
        finally:request_sink.reset(sink)
        leaked.assert_not_called(); self.assertEqual((self.row(row)['state'],self.row(row)['answer']),('answered','TEST_PRIVATE_SUMMARY'))
        for call in self.provider.create_completion.call_args_list:
            text = repr(call.kwargs['messages']); self.assertIn('TEST_TARGET_DINNER',text); self.assertNotIn('TEST_SOURCE_SECRET',text)
            self.assertEqual({tool['function']['name'] for tool in call.kwargs['tools']},{'history','ask'})
            self.assertEqual(call.args[0].model,'TEST_TARGET_MODEL')
        self.assertTrue(all(call.args[0]==self.target.scope for call in self.preferences.get_scope_default.call_args_list))
        self.assertEqual(self.query('SELECT COUNT(*) AS n FROM deliveries')[0]['n'],0)
        bills = self.query('SELECT actor_key,scope_key,purpose FROM model_usage_calls')
        self.assertEqual(len(bills),3); self.assertTrue(all(row==dict(actor_key=None,scope_key=self.target.scope.key,purpose='conversation_question') for row in bills))
        self.assertEqual(self.query('SELECT input_tokens,output_tokens,cached_tokens FROM usage_events'),[dict(input_tokens=20,output_tokens=10,cached_tokens=5)]*3)

    async def test_tool_whitelist_rejects_owner_vm_recursive_target_and_conflicting_answer(self):
        async def llm(prompt,history,tools,execute,**options):
            self.assertEqual({item['function']['name'] for item in tools},{'history','ask'})
            for name,args in [('ops_act',{}),('agent',{}),('send_file',{}),('memory_write',{}),('use_skill',{}),
                ('ask',{'action':'request','target':'TEST_FOREIGN'}),('ask',{'action':'refuse','question_id':'TEST_FOREIGN'}),
                ('history',{'query':'TEST','scope_key':self.endpoint.scope.key})]:
                with self.subTest(name=name),self.assertRaises(ValueError):await execute(name,args)
            self.assertEqual(options['memory_context'],'TEST_TARGET_DINNER_MEMORY')
            result = await execute('ask',{'action':'refuse'}); self.assertEqual(json.loads(result)['state'],'prepared')
            self.assertEqual(await execute('ask',{'action':'refuse'}),result)
            with self.assertRaisesRegex(ValueError,'question_response_conflict'):await execute('ask',{'action':'answer','answer':'TEST_REPLACE'})
            return 'TEST_PLAIN'
        self.model.llm = llm; row = self.submit(); await self.run_worker(); self.assertEqual(self.row(row)['state'],'refused')

    async def test_plain_model_text_cannot_answer_or_implicitly_send_question(self):
        self.provider.create_completion.return_value = self.response(); row = self.submit(); await self.run_worker()
        self.assertEqual((self.row(row)['state'],self.row(row)['answer']),('unknown',''))
        self.assertEqual(self.query('SELECT COUNT(*) AS n FROM deliveries')[0]['n'],0)

    async def test_same_account_caller_cannot_replace_frozen_target_history_scope(self):
        self.worker.executor = lambda target:self.model(replace(target,scope_key=self.endpoint.scope.key))
        row = self.submit(); await self.run_worker(); self.assertEqual(self.row(row)['state'],'unknown')
        self.provider.create_completion.assert_not_awaited(); self.assertEqual(self.query('SELECT * FROM usage_events'),[])

    async def test_legacy_event_and_human_source_cannot_be_combined_with_question_metadata(self):
        source = QuestionToolSource(self.target,'CQ-'+'a'*32,self.target.scope)
        context = ToolContext(SimpleNamespace(context=self.app),question_source=source,platform=BackgroundPlatform(self.target),capabilities=frozenset({'context','self','web'}))
        self.assertEqual({tool['function']['name'] for tool in neutral_registry_for(context).definitions(context)},{'history'})
        with self.assertRaises(ValueError):ToolContext(SimpleNamespace(context=self.app),question_source=source,event=object())
        with self.assertRaises(TypeError):ToolContext(SimpleNamespace(context=self.app),question_source=source,inbound=object())
        with self.assertRaises(ValueError):ToolContext(SimpleNamespace(context=self.app),question_source=source,program_source=ProgramToolSource(self.target,'TEST_PROGRAM','TEST_WAKE'))
        with self.assertRaises(ValueError):replace(source,history_scope=replace(self.endpoint.scope,account=replace(self.account,app_id='TEST_OTHER_APP')))

    async def test_disabled_target_ai_and_token_quota_refuse_before_provider_dispatch(self):
        self.preferences.get_scope_enabled_override.return_value = False
        row = self.submit(); await self.run_worker(); self.assertEqual(self.row(row)['state'],'unknown'); self.provider.create_completion.assert_not_awaited()
        self.preferences.get_scope_enabled_override.return_value = True; self.usage.daily_input_token_limit = 1
        with self.tx() as cursor:row = self.questions.submit(cursor,self.token,self.binding,self.edge['link_id'],'TEST_NEW_QUESTION')[0]
        await self.run_worker(); self.assertEqual(self.row(row)['state'],'unknown'); self.provider.create_completion.assert_not_awaited()
        self.assertEqual(self.query('SELECT * FROM usage_events'),[])

    async def test_mixed_source_billing_or_missing_host_scope_never_opens_model(self):
        target = self.claimed_target()
        with self.assertRaisesRegex(PermissionError,'question_host_model_scope_required'):await self.model(target)
        with scoped(self.app,self.endpoint,'turn',self.actor,current=target.current),self.assertRaises(PermissionError):await self.model(target)
        self.provider.create_completion.assert_not_awaited()

    async def test_stop_after_known_provider_usage_keeps_actual_quota_but_drops_prepared_answer(self):
        self.provider.create_completion.return_value = self.response()
        async def llm(prompt,history,tools,execute,**options):
            await self.gateway.create_completion(options['profile'],messages=[])
            await execute('ask',{'action':'answer','answer':'TEST_STALE_CANDIDATE'})
            with self.tx() as cursor:
                change(cursor,self.target,'stop',now=self.now,actor=self.actor.key,authorized=True)
                change(cursor,self.target,'start',now=self.now+1,actor=self.actor.key,authorized=True)
            return 'TEST_FINAL'
        self.model.llm = llm; row = self.submit(); await self.run_worker()
        self.assertEqual((self.row(row)['state'],self.row(row)['answer']),('cancelled',''))
        self.assertEqual(self.query('SELECT source,input_tokens,output_tokens FROM usage_events'),
            [dict(source='conversation_question',input_tokens=20,output_tokens=10)])
        self.assertIsNotNone(self.query('SELECT finished_at FROM model_usage_calls')[0]['finished_at'])

    async def test_history_tool_rechecks_before_releasing_result_after_target_stop(self):
        original,observed = self.ledger.history,[]
        def revoke(scope,**kwargs):
            result = original(scope,**kwargs)
            if kwargs.get('terms'):
                with self.tx() as cursor:change(cursor,self.target,'stop',now=self.now,actor=self.actor.key,authorized=True)
            return result
        async def llm(prompt,history,tools,execute,**options):
            try:observed.append(await execute('history',{'query':'TEST_TARGET_DINNER'}))
            except PermissionError:observed.append('closed')
            return 'TEST_NO_ANSWER'
        self.model.llm = llm; target = self.claimed_target()
        with patch.object(self.ledger,'history',side_effect=revoke),scoped(self.app,self.target,'conversation_question',current=target.current,conversation_question_id=target.question_id):
            with self.assertRaises(PermissionError):await self.model(target)
        self.assertEqual(observed,['closed'])

    async def test_double_cancel_drains_quota_settlement_without_replaying_prepared_answer(self):
        entered,release = asyncio.Event(),asyncio.Event(); original = self.model.calls
        async def blocked(fn,*args,**kwargs):
            if getattr(fn,'__name__','')=='settle':entered.set(); await release.wait()
            return await original(fn,*args,**kwargs)
        self.model.calls = blocked
        async def llm(prompt,history,tools,execute,**options):
            await self.gateway.create_completion(options['profile'],messages=[])
            await execute('ask',{'action':'answer','answer':'TEST_INTERRUPTED_CANDIDATE'}); return 'TEST_FINAL'
        self.model.llm = llm; self.provider.create_completion.return_value = self.response(); row = self.submit()
        task = asyncio.create_task(self.run_worker())
        try:
            await asyncio.wait_for(entered.wait(),5); task.cancel(); await asyncio.sleep(0); task.cancel(); await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task,5)
        self.assertEqual((self.row(row)['state'],self.row(row)['answer']),('unknown',''))
        self.assertEqual(self.query('SELECT source,input_tokens FROM usage_events'),[dict(source='conversation_question',input_tokens=20)])
        self.assertEqual(await self.run_worker(),0)

    async def test_semantic_history_query_keeps_question_target_and_no_source_person_on_embedding_receipt(self):
        self.app.settings = replace(self.app.settings,semantic_enabled=True)
        embedder = EmbeddingClient(base_url='https://embedding.example.invalid/v1',api_key='',model='TEST_EMBED',dimensions=1024,
            transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'data':[{'index':0,'embedding':[1]+[0]*1023}],'usage':{'prompt_tokens':6}})))
        backend = PgVectorBackend(self.database.dsn,dimensions=1024,schema=self.database.schema); self.app.semantic_recall = SemanticRecallService(embedder,backend)
        revision = self.query(f'SELECT {REVISION} AS version FROM messages_with_captions m WHERE canonical_message_id=?',(self.target_message.canonical_message_id,))[0]['version']
        backend.upsert(SemanticDocument(self.target.scope.key,'message','msg#'+str(self.target_message.canonical_message_id),self.target_message.prompt_text,
            dict(source_revision=revision,embedding_identity=embedding_identity(embedder))),[1]+[0]*1023,model=embedder.model)
        async def llm(prompt,history,tools,execute,**options):
            result = await execute('history',{'query':'聚餐什么时候','limit':1}); self.assertIn('TEST_TARGET_DINNER',result); self.assertNotIn('TEST_SOURCE_SECRET',result)
            await execute('ask',{'action':'refuse'}); return 'TEST_NO_PUBLIC_OUTPUT'
        self.model.llm = llm; row = self.submit(); await self.run_worker(); self.assertEqual(self.row(row)['state'],'refused')
        bills = self.query('SELECT actor_key,scope_key,purpose,profile,input_tokens,source_metadata_json FROM model_usage_calls')
        self.assertEqual(len(bills),1); bill = bills[0]
        self.assertEqual((bill['actor_key'],bill['scope_key'],bill['purpose'],bill['profile'],bill['input_tokens']),(None,self.target.scope.key,'conversation_question','embedding',6))
        self.assertEqual(json.loads(bill['source_metadata_json'])['question']['id'],row['question_id'])
