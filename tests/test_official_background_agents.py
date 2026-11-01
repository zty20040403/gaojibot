"""A real official source, durable worker and outbox, with local model/VM doubles."""
import json
import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import unittest

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official import turn_source
from src.plugins.ai_chat.adapters.qq_official.background_agents import OfficialBackgroundAgents
from src.plugins.ai_chat.adapters.qq_official.background_delivery import present
from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
from src.plugins.ai_chat.codex_engine import CodexEngine
from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.conversation_window import ConversationStore
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelProfile
from src.plugins.ai_chat.quota import UsageStore
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker
from tests.qq_official_turn_fixture import ExecutionFixture
from tests.test_codex_engine import FakeAppServer


class OfficialBackgroundTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_to_background_to_foreground_outbox(self):
        await self.exercise_background()

    async def test_rejected_steer_is_saved_as_failure_and_sent_to_foreground(self):
        await self.exercise_background(steer_error='TEST steering rejected')

    async def exercise_background(self, *, steer_error=None):
        f = ExecutionFixture('runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        calls = DatabaseCalls()
        self.addAsyncCleanup(calls.close)
        handle = f.claim().handle
        f.begin(handle)
        conversations, usage = ConversationStore(f.database), UsageStore(f.database)
        self.addCleanup(conversations.close)
        self.addCleanup(usage.close)
        deliveries = DeliveryStore(f.database, recover_interrupted=False)
        self.addCleanup(deliveries.close)
        profile = ModelProfile(name='TEST_MODEL', provider='test', protocol='openai-chat',
            model='TEST_MODEL', base_url='http://127.0.0.1', api_key_required=False)
        store = SimpleNamespace(transaction=f.tx, admitted=f.admission, clock=lambda: f.now)
        runtime = SimpleNamespace(store=store, jobs=f.jobs, _calls=calls, sender=None, file_service=None, media_service=None,
            outbox=OfficialSendStore(deliveries, admission=f.admission, pepper=b'TEST_BACKGROUND_PEPPER' * 3, clock=lambda: f.now))
        manager = SimpleNamespace(ensure_default=AsyncMock(return_value={'sandbox_id': 's123abc'}),
            write_file=AsyncMock(), read_file=AsyncMock(return_value=b'TEST_REPORT'),
            start_owned=AsyncMock(), _guest_exec=AsyncMock(return_value=(0, b'', b'')))
        app = SimpleNamespace(logger=logging.getLogger(__name__), official_runtime=runtime, db_call=calls,
            model_catalog=ModelCatalog({'TEST_MODEL': profile}, default_profile='TEST_MODEL'),
            sandbox_manager=manager, settings=replace(settings, search_enabled=False),
            conversations=conversations, message_ledger=f.ledger, usage_store=usage,
            semantic_recall=None, skill_registry=SimpleNamespace(get=lambda _: None),
            model_preferences=SimpleNamespace(get_explicit=lambda _: None))
        service = OfficialBackgroundAgents(runtime, f.token, app)
        runtime.agent_service = service
        source = dict(account_key=f.account.key, endpoint_key=f.endpoint.key, actor_key=f.actor.key,
            scope_key=f.endpoint.scope.key, source_execution_id=handle.execution_id,
            canonical_message_id=f.message.canonical_message_id, native_message_id='TEST_INPUT',
            endpoint_generation=1, route_epoch=1)
        inbound = service.restore(source)
        from src.plugins.ai_chat.adapters.qq_official.turn_platform import ExecutionBinding
        life = ExecutionBinding(turn_source, f.token, handle, f.job, f.admission)
        turn = TurnState(ToolContext(SimpleNamespace(context=app), inbound=inbound,
            platform=SimpleNamespace(account=f.account, execution=life)), model_profile=profile)
        submitted = await service.control(turn, 'agent', {'task': 'TEST research'})
        number = int(submitted['job'][4:])
        await service.control(turn, 'agent_steer', {'job': submitted['job'], 'message': 'TEST extra detail'})
        from datetime import datetime, timezone
        schedule = await service.schedules.control(turn, 'schedule_set', {'kind': 'reminder',
            'text': 'TEST reminder', 'at': datetime.fromtimestamp(f.now + 3600, timezone.utc).isoformat()})
        self.assertTrue((await service.schedules.control(turn, 'schedule_cancel', {'id': schedule['id']}))['cancelled'])


        async def engine(row, session, *, feedback, progress, trace):
            names = {item['function']['name'] for item in session.tools}
            self.assertIn('ask_main', names)
            self.assertNotIn('send_file', names)
            self.assertEqual(await feedback(), ['TEST extra detail'])
            await progress('shell')
            return 'TEST finished /work/result.txt'

        service.runner.engine = engine
        if steer_error is not None:
            server = FakeAppServer(steer_error=steer_error, complete_on_rejection=True)
            gateway = SimpleNamespace(start=AsyncMock(), open=lambda: ('TEST_TOKEN', server),
                revoke=Mock(), port=18640, failure=lambda token: '')
            service.runner.engine = CodexEngine(gateway, service.model_settings, start_vm=service.start_vm,
                                               guest_exec=service.guest_exec, clock=lambda: f.now)
        job = f.jobs.claim_due('TEST_BACKGROUND_WORKER', kinds=(service.kind,), now=f.now)[0]
        worker = DurableJobWorker(f.jobs, logger=app.logger, database_calls=calls, worker_id=job.lease_owner)
        worker.register(service.kind, service.execute)
        if steer_error is None:
            await worker._execute(job)
        else:
            with self.assertLogs(app.logger, level='ERROR'):
                await worker._execute(job)
        row = service.store.get(number)
        self.assertEqual(row['state'], 'completed' if steer_error is None else 'failed')
        if steer_error is None:
            self.assertEqual(row['last_tool'], 'shell')
        else:
            self.assertIn('追加要求未被 Codex 接受', row['error'])
            self.assertIn(steer_error, row['error'])
            self.assertEqual(row['report'], row['error'])
            self.assertEqual(json.loads(row['steering_json']), ['TEST extra detail'])
            self.assertIsNotNone(row['finished_at'])
            self.assertIsNone(row['delivered_at'])
            self.assertEqual([item['job_id'] for item in service.store.pending_results(f.account.key)], [number])
            self.assertEqual(len(server.sent('turn/steer')), 1)
            gateway.revoke.assert_called_once_with('TEST_TOKEN')
        self.assertEqual(f.jobs.get(number).status, 'succeeded')
        script = manager._guest_exec.await_args.args[2][1]
        self.assertTrue(script.endswith(f'base64 -d > /work/.gaoji/jobs/job-{number}.md'))
        manager.write_file.assert_not_awaited()
        answer = 'TEST foreground result' if steer_error is None else 'TEST 追加要求未送达，请重新发起任务。'
        with patch('src.plugins.ai_chat.adapters.qq_official.background_delivery.ask_deepseek_with_tools',
                   new=AsyncMock(return_value=answer)) as foreground:
            await present(service, row)
        if steer_error is not None:
            prompt = foreground.await_args.args[0]
            self.assertIn(f'[job#{number} failed]', prompt)
            self.assertIn(row['error'], prompt)
            self.assertIn('失败须说明具体原因，不能声称已经成功', prompt)
        self.assertEqual(service.store.get(number)['frontend_answer'], answer)
        self.assertIsNotNone(service.store.get(number)['delivered_at'])
        self.assertEqual(f.query("SELECT purpose FROM delivery_plans WHERE account_key=?", (f.account.key,))[0]['purpose'], 'background_result')
