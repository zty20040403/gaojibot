"""Recovery respects shutdown and preserves the original journal."""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from tests import test_qq_official_turn_endtoend as turn_fixture
from tests.test_qq_official_runtime_sender import verified_ready_platform


class OfficialTurnRecoveryTests(unittest.IsolatedAsyncioTestCase):
    setUp = turn_fixture.OfficialTurnEndToEndTests.setUp
    runtime = turn_fixture.OfficialTurnEndToEndTests.runtime
    query = turn_fixture.OfficialTurnEndToEndTests.query
    wait_db = turn_fixture.OfficialTurnEndToEndTests.wait_db
    application = turn_fixture.OfficialTurnEndToEndTests.application
    open_runtime = turn_fixture.OfficialTurnEndToEndTests.open_runtime
    emit = turn_fixture.OfficialTurnEndToEndTests.emit
    complete = turn_fixture.OfficialTurnEndToEndTests.complete

    async def test_model_internal_error_sends_safe_reply_without_raw_exception(self):
        app = self.application()
        secret = "TEST_PRIVATE_MODEL_EXCEPTION_SENTINEL"
        with patch("src.plugins.ai_chat.agent_loop._create_completion", side_effect=ValueError(secret)) as model:
            async with verified_ready_platform() as fake:
                runtime = await self.open_runtime(fake, app)
                await self.emit(fake, native="TEST_ERROR", content="TEST question")
                await self.complete("TEST_ERROR")
                model.assert_awaited_once()
                journal = self.query("SELECT status,final_text FROM agent_turns")
                self.assertEqual(len(journal), 1)
                self.assertEqual(journal[0]["status"], "crashed")
                evidence = self.query("SELECT result_body_json,reason_code FROM qq_official_turn_executions")
                evidence += self.query("SELECT result_json,last_error FROM durable_jobs")
                serialized = json.dumps([journal, evidence, fake.received, str(self.logger.mock_calls)], default=str)
                self.assertNotIn(secret, serialized)
                self.assertEqual(len(fake.received), 1)
                self.assertEqual(app.running_tasks.list_all(), [])
                await runtime.stop()

    async def test_nonresumable_snapshot_is_not_dispatched_after_restart(self):
        app, started = self.application(), asyncio.Event()

        async def blocked(**kwargs):
            started.set()
            await asyncio.Event().wait()

        with patch('src.plugins.ai_chat.agent_loop.durable_messages', return_value=([], False)), \
                patch('src.plugins.ai_chat.agent_loop._create_completion', side_effect=blocked) as model:
            async with verified_ready_platform() as fake:
                runtime = await self.open_runtime(fake, app)
                await self.emit(fake, native='TEST_NONRESUMABLE', content='TEST question')
                await asyncio.wait_for(started.wait(), 5)
                self.assertEqual(self.query('SELECT checkpoint_json FROM qq_official_turn_executions'),
                                 [{'checkpoint_json': None}])
                await runtime.stop()
                restarted = await self.open_runtime(fake, app)
                try:
                    await self.wait_db("SELECT delivery_id FROM deliveries WHERE status='committed'")
                    self.assertIn('刚才重启了', json.dumps(fake.received, ensure_ascii=False))
                    model.assert_awaited_once()
                finally:
                    await restarted.stop()

    async def test_shutdown_leaves_running_journal_for_next_ready_owner(self):
        app = self.application()
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def blocked(**kwargs):
            if started.is_set():
                return turn_fixture.model_response('TEST recovered', [], kwargs)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch("src.plugins.ai_chat.agent_loop._create_completion", side_effect=blocked) as model:
            async with verified_ready_platform() as fake:
                runtime = await self.open_runtime(fake, app)
                await self.emit(fake, native="TEST_STOP_RUNNING", content="TEST question")
                await asyncio.wait_for(started.wait(), 5)
                await asyncio.wait_for(runtime.stop(), 5)
                self.assertTrue(cancelled.is_set())
                self.assertEqual(app.running_tasks.list_all(), [])
                self.assertEqual(fake.received, [])
                original = self.query('SELECT turn_id,status FROM agent_turns')
                self.assertEqual([r['status'] for r in original], ['running'])
                restarted = await self.open_runtime(fake, app)
                await self.complete('TEST_STOP_RUNNING')
                self.assertEqual(model.await_count, 2)
                self.assertEqual(len(self.query("SELECT * FROM delivery_plans")), 1)
                self.assertEqual(self.query("SELECT * FROM usage_events"), [])
                self.assertEqual(self.query('SELECT turn_id,status FROM agent_turns'),
                                 [{'turn_id': original[0]['turn_id'], 'status': 'succeeded'}])
                self.assertEqual(len(fake.received), 1)
                self.assertIn('TEST recovered', json.dumps(fake.received))
                await restarted.stop()
                again = await self.open_runtime(fake, app)
                self.assertEqual(len(self.query("SELECT * FROM delivery_plans")), 1)
                await again.stop()
