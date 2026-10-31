"""A real official source, durable worker and outbox, with local model/VM doubles."""
import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official import turn_source
from src.plugins.ai_chat.adapters.qq_official.background_agents import OfficialBackgroundAgents
from src.plugins.ai_chat.adapters.qq_official.background_delivery import present
from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.conversation_window import ConversationStore
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelProfile
from src.plugins.ai_chat.quota import UsageStore
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker
from tests.qq_official_turn_fixture import ExecutionFixture


class OfficialBackgroundTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_to_background_to_foreground_outbox(self):
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
            write_file=AsyncMock(), read_file=AsyncMock(return_value=b'TEST_REPORT'))
        app = SimpleNamespace(logger=logging.getLogger(__name__), official_runtime=runtime, db_call=calls,
            model_catalog=ModelCatalog({'TEST_MODEL': profile}, default_profile='TEST_MODEL'),
            sandbox_manager=manager, settings=replace(settings, search_enabled=False),
            conversations=conversations, message_ledger=f.ledger, usage_store=usage,
            semantic_recall=None, skill_registry=SimpleNamespace(get=lambda _: None))
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


        async def model(_text, _history, tools, execute, *, feedback_provider, **kwargs):
            self.assertIn('shell', {item['function']['name'] for item in tools})
            self.assertNotIn('send_file', {item['function']['name'] for item in tools})
            self.assertEqual(await feedback_provider(), ['TEST extra detail'])
            await execute('write_file', {'path': '/work/result.txt', 'content': 'TEST file'})
            return 'TEST finished /work/result.txt'

        service.runner.llm = model
        job = f.jobs.claim_due('TEST_BACKGROUND_WORKER', kinds=(service.kind,), now=f.now)[0]
        worker = DurableJobWorker(f.jobs, logger=app.logger, database_calls=calls, worker_id=job.lease_owner)
        worker.register(service.kind, service.execute)
        await worker._execute(job)
        row = service.store.get(number)
        self.assertEqual(row['state'], 'completed')
        self.assertEqual(f.jobs.get(number).status, 'succeeded')
        self.assertEqual(manager.write_file.await_args.args[2], f'/work/.gaoji/jobs/job-{number}.md')
        with patch('src.plugins.ai_chat.adapters.qq_official.background_delivery.ask_deepseek_with_tools',
                   new=AsyncMock(return_value='TEST foreground result')):
            await present(service, row)
        self.assertIsNotNone(service.store.get(number)['delivered_at'])
        self.assertEqual(f.query("SELECT purpose FROM delivery_plans WHERE account_key=?", (f.account.key,))[0]['purpose'], 'background_result')
