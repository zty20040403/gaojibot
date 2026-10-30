"""Direct turns report failures and close interrupted journals without replay."""
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

    async def test_stop_cancels_model_and_new_owner_does_not_execute_it_again(self):
        app = self.application()
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def blocked(**kwargs):
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
                restarted = await self.open_runtime(fake, app)
                await self.wait_db("SELECT execution_id FROM qq_official_turn_executions WHERE reason_code='restart_notified'")
                await self.wait_db("SELECT delivery_id FROM deliveries WHERE status='committed'")
                model.assert_awaited_once()
                self.assertEqual(len(self.query("SELECT * FROM delivery_plans")), 1)
                self.assertEqual(self.query("SELECT * FROM usage_events"), [])
                self.assertEqual(self.query("SELECT status FROM agent_turns"), [{"status": "crashed"}])
                self.assertIn('刚才重启了', json.dumps(fake.received, ensure_ascii=False))
                await restarted.stop()
                again = await self.open_runtime(fake, app)
                self.assertEqual(len(self.query("SELECT * FROM delivery_plans")), 1)
                await again.stop()
