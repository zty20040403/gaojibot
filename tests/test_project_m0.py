"""M0 validation of existing scheduled turns; no project product code yet."""

from __future__ import annotations

from dataclasses import replace
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import EmoteNode, MessageBody, TextNode
from src.plugins.ai_chat.quota import UsageStore
from src.plugins.ai_chat.routine_worker import RoutineDispatcher, routine_event
from src.plugins.ai_chat.routines import RoutineStore
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.turn_journal import TurnJournal
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker
from tests.handler_fixture import HandlerTestCaseMixin
from tests.postgres_fixture import postgres_for_test
from tests.test_llm_gateway import profile


def completion(*, tool=False):
    calls = [SimpleNamespace(id='scheduled-face', type='function', function=SimpleNamespace(
        name='send_qq_face', arguments='{"expression":"微笑"}'))] if tool else []
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content='' if tool else 'Scheduled verification complete.', tool_calls=calls))], usage=None)


class ProjectSyntheticTurnTests(HandlerTestCaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        db = postgres_for_test(self)
        app = self.app_context
        app.database = db
        app.message_ledger = MessageLedger(db)
        app.job_store = DurableJobStore(db)
        app.delivery_store = DeliveryStore(db, recover_interrupted=False)
        app.usage_store = UsageStore(db)
        app.turn_journal = TurnJournal(db)
        app.routine_store = RoutineStore(db, jobs=app.job_store,
            outbox=app.delivery_store, usage=app.usage_store)
        app.job_worker = DurableJobWorker(app.job_store, logger=app.logger)
        app.settings = replace(app.settings, subagent_entry_enabled=False,
            tool_intent_enabled=False, search_enabled=False, voice_enabled=False,
            ocr_enabled=False, routine_timeout_seconds=30)
        self.scope = ConversationScope('onebot-v11', 'group', '101', '201', '301')
        self.profile = profile('m0-synthetic')
        self.bot = SimpleNamespace(self_id='301',
            call_api=AsyncMock(return_value={'online': True, 'good': True}),
            get_group_member_list=AsyncMock(return_value=[{'user_id': 201}]))
        self.dispatcher = RoutineDispatcher(self.handlers)

    async def test_real_turn_tool_outbox_recovery_does_not_insert_synthetic_user_message(self):
        app = self.app_context
        seed = app.message_ledger.record_message(self.scope, native_message_id='77',
            sender_native_user_id='201', sender_display='Fixture',
            body=MessageBody((TextNode(0, 'A real earlier message'),)),
            occurred_at=int(time.time()) - 300)
        original = app.message_ledger.recent_in_scope(self.scope)
        routine = app.routine_store.set(self.scope, name='m0 validation',
            prompt='Send a smile and confirm the scheduled check.', cron='* * * * *',
            now=int(time.time()) - 65)
        run = app.routine_store.materialize_due()[0]
        job = app.job_store.claim_due('m0-fixture')[0]
        synthetic = routine_event(run)
        self.assertLess(synthetic.message_id, 0)
        with (patch('src.plugins.ai_chat.routine_worker.get_bot', return_value=self.bot),
              patch.object(self.handlers, 'group_enabled', return_value=True),
              patch.object(self.handlers.chat, '_preferred_model_profile', return_value=self.profile),
              patch('src.plugins.ai_chat.agent_loop._create_completion',
                    new=AsyncMock(side_effect=[completion(tool=True), completion()])) as provider):
            result = await self.dispatcher.execute(job)
            self.assertEqual(result['state'], 'completed')
            self.assertEqual(provider.await_count, 2)
            tool_results = [item for item in provider.await_args.kwargs['messages']
                            if item['role'] == 'tool']
            self.assertEqual(len(tool_results), 1)
            self.assertEqual(tool_results[0]['tool_call_id'], 'scheduled-face')
            self.assertTrue(tool_results[0]['content'].startswith('<untrusted kind="tool_result"'))
            self.assertTrue(app.job_store.defer(job, delay_seconds=0))
            replacement = app.job_store.claim_due('m0-takeover')[0]
            recovered = await self.dispatcher.execute(replacement)
            self.assertEqual(recovered['delivery_id'], result['delivery_id'])
            self.assertEqual(provider.await_count, 2)

        self.assertEqual(app.message_ledger.recent_in_scope(self.scope), original)
        self.assertEqual(len(original), 1)
        self.assertEqual(original[0].canonical_message_id, seed.canonical_message_id)
        self.assertIsNone(app.message_ledger.canonical_id_for_native(self.scope, str(synthetic.message_id)))
        saved = app.routine_store.get_run(run.run_id)
        turn = app.turn_journal.get_turn_by_id(saved.turn_id)
        self.assertEqual(turn.status, 'succeeded')
        self.assertIsNone(turn.trigger_canonical_message_id)
        self.assertEqual(turn.scope_key, self.scope.key)
        self.assertEqual(saved.routine_id, routine.routine_id)
        deliveries = app.delivery_store.recent()
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].target_scope.key, self.scope.key)
        self.assertEqual(deliveries[0].reply_to_native_message_id, '')
        self.assertTrue(any(isinstance(node, TextNode) and
                            'Scheduled verification complete.' in node.text
                            for node in deliveries[0].body.nodes))
        self.assertTrue(any(isinstance(node, EmoteNode) and node.native_id == '14'
                            for node in deliveries[0].body.nodes))


if __name__ == '__main__':
    unittest.main()
