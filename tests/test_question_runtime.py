"""Actual QQ account/TxA/link/queue/model factory on loopback, not live QQ."""
import asyncio
from dataclasses import replace
from datetime import datetime,timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

from src.plugins.ai_chat.adapters.qq_official import turn_source
from src.plugins.ai_chat.adapters.qq_official.conversation_control import change
from src.plugins.ai_chat.adapters.qq_official.question_runtime import QuestionRuntime
from src.plugins.ai_chat.adapters.qq_official.turn_platform import ExecutionBinding
from src.plugins.ai_chat.llm_gateway import LLMGateway
from src.plugins.ai_chat.model_billing import ModelBilling
from src.plugins.ai_chat.platform_types import ActorKey,EndpointKey
from tests import test_native_billing as native
from tests import test_qq_official_runtime as fixture
from tests.test_agent_kernel import _tool_response,_final
from tests.test_model_budget import completion
from tests.test_qq_official_runtime_sender import verified_ready_platform


async def park_worker(_service):await asyncio.Event().wait()


class QuestionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.OfficialRuntimeTests.setUp
    runtime = fixture.OfficialRuntimeTests.runtime
    query = fixture.OfficialRuntimeTests.query
    wait_db = fixture.OfficialRuntimeTests.wait_db
    application = native.NativeBillingTests.application

    async def start_account(self,fake,app):
        fake.app_id = 'TEST_QUESTION_FACTORY_'+self._testMethodName
        self.entered,self.release = asyncio.Event(),asyncio.Event()
        self.source_error = None
        async def source_task(job):
            try:
                token = runtime._token
                with runtime.store.transaction() as cursor:
                    row = cursor.execute('SELECT native_message_id FROM qq_official_inbound_events WHERE inbox_id=?',(job.payload['inbox_id'],)).fetchone()
                    if row['native_message_id']!='TEST_SOURCE_HELD':return
                    now = int(runtime.store.clock())
                    handle = turn_source.claim_execution(cursor,token=token,job=job,now=now,deadline_at=now+3600,admission=runtime.store.admitted).handle
                    journal = runtime.journal.start_turn(handle.endpoint.scope,trigger_canonical_message_id=handle.canonical_message_id,
                        objective='TEST_SOURCE_HOST_TASK',provider='TEST_PROVIDER',model='TEST_SOURCE_MODEL',started_at=now)
                    turn_source.begin_execution(cursor,token=token,handle=handle,job=job,now=now,admission=runtime.store.admitted,journal_turn_id=journal.turn_id)
                    self.binding = ExecutionBinding(turn_source,token,handle,job,runtime.store.admitted)
            except Exception as error:self.source_error = error; self.entered.set(); raise
            self.entered.set(); await self.release.wait()
        loaded = replace(fixture.loaded_config(fake.app_id,base=fake.base_url),owner_openids=('TEST_RUNTIME_OWNER','TEST_TARGET_OWNER'))
        options = dict(application=app,turn_consumer_factory=lambda *_:source_task)
        if app is not None:options.update(ledger=app.message_ledger,journal=app.turn_journal)
        runtime = self.runtime(loaded=loaded,**options); await runtime.start(); await asyncio.wait_for(fake.wait_ready(),5)
        await self.wait_db("SELECT bot_native_id FROM qq_official_accounts WHERE bot_identity_source='ready'")
        self.assertIsNotNone(runtime.question_service)
        return runtime

    async def emit(self,fake,text,*,actor='TEST_RUNTIME_OWNER',native=None):
        counter = getattr(self,'counter',0)+1; self.counter = counter; native = native or 'TEST_QUESTION_RUNTIME_'+str(counter)
        await fake.emit('C2C_MESSAGE_CREATE',dict(id=native,author=dict(user_openid=actor),content=text,timestamp=datetime.now(timezone.utc).isoformat()))
        await self.wait_db("SELECT * FROM qq_official_inbound_events WHERE native_message_id=? AND status='processed'",(native,))
        return native

    async def prepare_source(self,fake,runtime):
        with patch('src.plugins.ai_chat.adapters.qq_official.linking.secrets.randbelow',return_value=654321):source = await self.emit(fake,'/link')
        await self.wait_db("SELECT * FROM deliveries WHERE reply_to_native_message_id=? AND status='committed'",(source,))
        target = await self.emit(fake,'/link 654321',actor='TEST_TARGET_OWNER')
        await self.wait_db("SELECT * FROM deliveries WHERE reply_to_native_message_id=? AND status='committed'",(target,))
        edge = self.query("SELECT * FROM qq_official_conversation_links WHERE state='active'")[0]
        await self.emit(fake,'TEST_TARGET_DINNER_ONLY',actor='TEST_TARGET_OWNER')
        await self.emit(fake,'TEST_SOURCE_PRIVATE_ONLY',native='TEST_SOURCE_HELD'); await asyncio.wait_for(self.entered.wait(),5)
        if self.source_error is not None:raise self.source_error
        return edge

    def response(self,name=None,arguments=None):
        response = _tool_response(name,arguments,'TEST_CALL_'+str(name)) if name else _final('TEST_PLAIN_NOT_PUBLIC')
        response.usage = completion(20,10,5).usage; return response

    def gateway(self,app,responses):
        self.provider = SimpleNamespace(create_completion=AsyncMock(side_effect=responses))
        app.llm_gateway = LLMGateway({'openai-chat':self.provider},catalog=app.model_catalog)
        return patch('src.plugins.ai_chat.agent_loop._runtime',return_value=(app.model_catalog,app.llm_gateway))

    async def test_real_account_factory_after_default_private_link_runs_private_target_tools_and_billing(self):
        app = self.application()
        with self.gateway(app,[self.response('history',{'query':'TEST_TARGET_DINNER_ONLY'}),self.response('ask',{'action':'answer','answer':'TEST_PRIVATE_SUMMARY'}),self.response()]):
            async with verified_ready_platform() as fake:
                runtime = await self.start_account(fake,app)
                try:
                    edge = await self.prepare_source(fake,runtime); service = runtime.question_service
                    self.assertTrue(runtime.status()['question_model_connected']); row,_ = await runtime._calls(service.submit,self.binding,edge['link_id'],'TEST_PRIVATE_QUESTION')
                    answer = (await self.wait_db("SELECT * FROM qq_official_conversation_questions WHERE question_id=? AND state NOT IN ('queued','running')",(row['question_id'],),timeout=15))[0]
                    self.assertEqual(answer['state'],'answered',(answer['reason_code'],self.provider.create_completion.call_count,self.logger.method_calls))
                    self.assertEqual(answer['answer'],'TEST_PRIVATE_SUMMARY'); self.assertEqual(len(fake.received),2)
                    target = EndpointKey(runtime.loaded.account,'private','TEST_TARGET_OWNER')
                    bills = self.query('SELECT actor_key,scope_key,purpose,finished_at FROM model_usage_calls')
                    self.assertEqual(len(bills),3); self.assertTrue(all(bill['actor_key'] is None and bill['scope_key']==target.scope.key and bill['purpose']=='conversation_question' and bill['finished_at'] for bill in bills))
                    for call in self.provider.create_completion.call_args_list:
                        text = repr(call.kwargs['messages']); self.assertIn('TEST_TARGET_DINNER_ONLY',text); self.assertNotIn('TEST_SOURCE_PRIVATE_ONLY',text)
                    self.assertEqual({row['binding_state'] for row in self.query('SELECT binding_state FROM qq_official_endpoints')},{'unbound'})
                finally:await runtime.stop()
                self.assertTrue(service.closing); self.assertIsNone(runtime.question_service); self.assertEqual(self.query('SELECT state FROM qq_official_conversation_questions'),[dict(state='answered')])

    async def test_monitor_closes_stopped_and_expired_pending_questions_without_model_replay(self):
        app = self.application()
        with self.gateway(app,[]),patch.object(QuestionRuntime,'run',new=park_worker):
            async with verified_ready_platform() as fake:
                runtime = await self.start_account(fake,app)
                try:
                    edge = await self.prepare_source(fake,runtime); service = runtime.question_service
                    row,_ = await runtime._calls(service.submit,self.binding,edge['link_id'],'TEST_STOP_PENDING')
                    target = EndpointKey.parse(edge['target_endpoint_key']); actor = ActorKey(runtime.loaded.account,'c2c_user','','TEST_TARGET_OWNER')
                    with runtime.store.transaction() as cursor:
                        change(cursor,target,'stop',now=int(runtime.store.clock()),actor=actor.key,authorized=True)
                        change(cursor,target,'start',now=int(runtime.store.clock()),actor=actor.key,authorized=True)
                    service.next_sweep = 0; await service.maintain()
                    self.assertEqual(self.query('SELECT state FROM qq_official_conversation_questions WHERE question_id=?',(row['question_id'],)),[dict(state='cancelled')])
                    newer,_ = await runtime._calls(service.submit,self.binding,edge['link_id'],'TEST_NEW_EXPIRY')
                    service.questions.authority.authority.clock = lambda:newer['deadline_at']; service.next_sweep = 0; await service.maintain()
                    self.assertEqual(self.query('SELECT state FROM qq_official_conversation_questions WHERE question_id=?',(newer['question_id'],)),[dict(state='expired')])
                    self.provider.create_completion.assert_not_awaited(); self.assertEqual(len(fake.received),2)
                finally:await runtime.stop()
                self.assertEqual(self.query('SELECT state FROM qq_official_conversation_links'),[dict(state='active')])

    async def test_shutdown_retires_pending_question_and_queue_without_revoking_live_link(self):
        app = self.application()
        with self.gateway(app,[]),patch.object(QuestionRuntime,'run',new=park_worker):
            async with verified_ready_platform() as fake:
                runtime = await self.start_account(fake,app); edge = await self.prepare_source(fake,runtime)
                row,_ = await runtime._calls(runtime.question_service.submit,self.binding,edge['link_id'],'TEST_SHUTDOWN_PENDING')
                await runtime.stop()
                self.assertEqual(self.query('SELECT state FROM qq_official_conversation_questions WHERE question_id=?',(row['question_id'],)),[dict(state='cancelled')])
                self.assertEqual(self.query('SELECT status FROM durable_jobs WHERE job_id=?',(row['job_id'],)),[dict(status='cancelled')])
                self.assertEqual(self.query('SELECT state FROM qq_official_conversation_links'),[dict(state='active')]); self.provider.create_completion.assert_not_awaited()

    async def test_restart_without_ai_recovers_untouched_old_epoch_before_any_worker_or_gateway_input(self):
        app = self.application()
        async def simulate_process_loss(service):service.closing = True; service.runtime.question_service = None
        with self.gateway(app,[]),patch.object(QuestionRuntime,'run',new=park_worker):
            async with verified_ready_platform() as fake:
                # Patch before attach: AsyncExitStack captures the bound close callback.
                with patch.object(QuestionRuntime,'close',simulate_process_loss):
                    first = await self.start_account(fake,app); edge = await self.prepare_source(fake,first)
                    row,_ = await first._calls(first.question_service.submit,self.binding,edge['link_id'],'TEST_PROCESS_LOSS_PENDING')
                    await first.stop()  # Explicit crash fixture, not healthy shutdown.
                self.assertEqual(self.query('SELECT state FROM qq_official_conversation_questions'),[dict(state='queued')])
                recovered_before_input = []
                original_attach = QuestionRuntime.attach
                async def observe_attach(service):
                    result = await original_attach(service)
                    recovered_before_input.append(self.query('SELECT state FROM qq_official_conversation_questions'))
                    return result
                with patch.object(QuestionRuntime,'attach',observe_attach):second = await self.start_account(fake,None)
                try:
                    self.assertEqual(recovered_before_input,[[dict(state='cancelled')]])
                    self.assertFalse(second.status()['question_model_connected']); self.assertFalse(second.question_service.available)
                    self.assertEqual(self.query('SELECT state FROM qq_official_conversation_questions'),[dict(state='cancelled')])
                    self.assertEqual(self.query('SELECT status FROM durable_jobs WHERE job_id=?',(row['job_id'],)),[dict(status='cancelled')])
                    self.provider.create_completion.assert_not_awaited(); self.assertEqual(len(fake.received),2)
                finally:await second.stop()

    async def test_shutdown_drains_target_model_known_usage_before_closing_original_question(self):
        app = self.application(); entered,cleaning,release = asyncio.Event(),asyncio.Event(),asyncio.Event()
        requests = 0
        async def pending(_profile,**_kwargs):
            nonlocal requests
            requests += 1
            if requests>1:raise AssertionError('model_request_after_account_shutdown')
            entered.set()
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set(); await release.wait()
                return self.response('ask',{'action':'refuse'})
        with self.gateway(app,pending):
            async with verified_ready_platform() as fake:
                runtime = await self.start_account(fake,app); self.addCleanup(release.set)
                edge = await self.prepare_source(fake,runtime)
                row,_ = await runtime._calls(runtime.question_service.submit,self.binding,edge['link_id'],'TEST_DRAIN_ORIGINAL')
                await asyncio.wait_for(entered.wait(),10); stopping = asyncio.create_task(runtime.stop())
                try:
                    await asyncio.wait_for(cleaning.wait(),5); await asyncio.sleep(.05)
                    self.assertFalse(stopping.done())
                    self.assertEqual(self.query('SELECT state FROM qq_official_conversation_questions WHERE question_id=?',(row['question_id'],)),[dict(state='running')])
                finally:
                    release.set(); await asyncio.wait_for(stopping,10)
                self.assertEqual(requests,1)
                bills = self.query('SELECT input_tokens,finished_at,actor_key FROM model_usage_calls'); self.assertEqual(len(bills),1); bill = bills[0]
                self.assertEqual(bill['input_tokens'],20); self.assertTrue(bill['finished_at']); self.assertIsNone(bill['actor_key'])
                self.assertEqual(self.query('SELECT state FROM qq_official_conversation_questions'),[dict(state='unknown')])
                self.assertEqual(len(fake.received),2); self.assertIsNone(runtime.question_service)

    async def test_unavailable_accounting_refuses_question_but_keeps_link_control(self):
        app = self.application(); app.model_billing = ModelBilling(None)
        async with verified_ready_platform() as fake:
            runtime = await self.start_account(fake,app)
            try:
                edge = await self.prepare_source(fake,runtime); self.assertFalse(runtime.status()['question_model_connected'])
                with self.assertRaisesRegex(RuntimeError,'question_model_unavailable'):
                    await runtime._calls(runtime.question_service.submit,self.binding,edge['link_id'],'TEST_UNAVAILABLE')
                self.assertEqual(self.query('SELECT * FROM qq_official_conversation_questions'),[]); self.assertEqual(len(fake.received),2)
            finally:await runtime.stop()
