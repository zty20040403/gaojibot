"""Real ingress and worker recovery never repeat an uncertain model execution."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import psycopg

from src.plugins.ai_chat.adapters.qq_official.turn_platform import OfficialTurnPlatform
from tests import test_qq_official_turn_endtoend as turn_fixture
from tests.test_qq_official_runtime_sender import verified_ready_platform


def completion(text="TEST durable answer"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=[]))],
        usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    )


class OfficialTurnRecoveryTests(unittest.IsolatedAsyncioTestCase):
    setUp = turn_fixture.OfficialTurnEndToEndTests.setUp
    runtime = turn_fixture.OfficialTurnEndToEndTests.runtime
    query = turn_fixture.OfficialTurnEndToEndTests.query
    wait_db = turn_fixture.OfficialTurnEndToEndTests.wait_db
    application = turn_fixture.OfficialTurnEndToEndTests.application
    open_runtime = turn_fixture.OfficialTurnEndToEndTests.open_runtime
    emit = turn_fixture.OfficialTurnEndToEndTests.emit
    complete = turn_fixture.OfficialTurnEndToEndTests.complete

    async def settled_attention(self, native):
        return await self.wait_db("""SELECT x.*,j.status AS job_status FROM qq_official_turn_executions x
            JOIN qq_official_inbound_events i ON i.inbox_id=x.inbox_id
            JOIN durable_jobs j ON j.job_id=x.job_id
            WHERE i.native_message_id=? AND x.state='needs_attention' AND j.status='succeeded'""", (native,))

    async def expedite(self, native):
        await asyncio.to_thread(self.query, """UPDATE durable_jobs SET next_attempt_at=0
            WHERE status='pending' AND job_id=(SELECT dispatch_job_id FROM qq_official_inbound_events
                WHERE native_message_id=?) RETURNING job_id""", (native,))

    async def test_model_internal_error_freezes_safe_reply_without_raw_exception(self):
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

    async def test_result_ready_recovers_only_plan_after_handoff_failure_and_restart(self):
        app = self.application()
        model = AsyncMock(return_value=completion())
        with patch("src.plugins.ai_chat.agent_loop._create_completion", new=model):
            async with verified_ready_platform() as fake:
                runtime = await self.open_runtime(fake, app)
                with patch.object(OfficialTurnPlatform, "send", side_effect=RuntimeError("TEST_HANDOFF_SECRET")):
                    await self.emit(fake, native="TEST_RESULT_READY", content="TEST question")
                    row = (await self.wait_db("""SELECT x.execution_id,x.journal_turn_id,x.result_digest,j.status
                        FROM qq_official_turn_executions x JOIN durable_jobs j ON j.job_id=x.job_id
                        WHERE x.state='result_ready' AND j.status='pending'"""))[0]
                    self.assertEqual(fake.received, [])
                    self.assertEqual(len(self.query("SELECT * FROM usage_events")), 1)
                    await runtime.stop()
                await self.expedite("TEST_RESULT_READY")
                restarted = await self.open_runtime(fake, app)
                finished = (await self.complete("TEST_RESULT_READY"))[0]
                self.assertEqual(finished["execution_id"], row["execution_id"])
                self.assertEqual(finished["journal_turn_id"], row["journal_turn_id"])
                model.assert_awaited_once()
                self.assertEqual(len(self.query("SELECT * FROM agent_turns")), 1)
                self.assertEqual(len(self.query("SELECT * FROM usage_events")), 1)
                self.assertEqual(len(self.query("SELECT * FROM delivery_plans")), 1)
                self.assertEqual(len(fake.received), 1)
                self.assertEqual(fake.received[0]["payload"]["markdown"]["content"], "TEST durable answer")
                self.assertEqual(self.query("SELECT result_digest FROM qq_official_turn_executions")[0]["result_digest"], row["result_digest"])
                await restarted.stop()

    async def test_result_transaction_failure_rolls_back_usage_and_never_reexecutes_model(self):
        app = self.application()
        model = AsyncMock(return_value=completion())
        with patch("src.plugins.ai_chat.agent_loop._create_completion", new=model):
            with patch.object(app.turn_journal, "finish_turn", side_effect=ValueError("TEST_JOURNAL_SECRET")):
                async with verified_ready_platform() as fake:
                    runtime = await self.open_runtime(fake, app)
                    await self.emit(fake, native="TEST_RESULT_ROLLBACK", content="TEST question")
                    await self.wait_db("SELECT execution_id FROM qq_official_turn_executions WHERE state='needs_attention'")
                    await self.expedite("TEST_RESULT_ROLLBACK")
                    row = (await self.settled_attention("TEST_RESULT_ROLLBACK"))[0]
                    self.assertIsNone(row["result_body_json"])
                    self.assertIsNone(row["plan_id"])
                    self.assertEqual(self.query("SELECT * FROM usage_events"), [])
                    self.assertEqual(self.query("SELECT * FROM delivery_plans"), [])
                    self.assertEqual(self.query("SELECT status FROM agent_turns"), [{"status": "running"}])
                    self.assertEqual(fake.received, [])
                    self.assertNotIn("TEST_JOURNAL_SECRET", json.dumps(self.query("SELECT last_error,result_json FROM durable_jobs"), default=str))
                    model.assert_awaited_once()
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
                await self.expedite("TEST_STOP_RUNNING")
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

    async def test_account_backend_loss_cancels_model_and_takeover_never_replays(self):
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
                await self.emit(fake, native="TEST_ACCOUNT_LOST", content="TEST question")
                await asyncio.wait_for(started.wait(), 5)
                token = runtime._token

                def terminate():
                    with psycopg.connect(self.database.dsn, autocommit=True) as connection:
                        return connection.execute("SELECT pg_terminate_backend(%s,1000)", (token.backend_pid,)).fetchone()[0]

                self.assertTrue(await asyncio.to_thread(terminate))
                async with asyncio.timeout(5):
                    while runtime.state != "paused":
                        await asyncio.sleep(0.01)
                    await runtime._idle.wait()
                self.assertTrue(cancelled.is_set())
                self.assertEqual(app.running_tasks.list_all(), [])
                self.assertEqual(fake.received, [])
                await runtime.stop()
                await self.expedite("TEST_ACCOUNT_LOST")
                restarted = await self.open_runtime(fake, app)
                self.assertGreater(restarted._token.epoch, token.epoch)
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
