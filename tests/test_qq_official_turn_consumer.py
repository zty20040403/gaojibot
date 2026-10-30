import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.plugins.ai_chat.adapters.qq_official import execution
from tests import test_qq_official_execution as fixture
from src.plugins.ai_chat.adapters.qq_official.turn_consumer import OfficialTurnConsumer
from src.plugins.ai_chat.adapters.qq_official.turn_platform import TransportState
from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
from src.plugins.ai_chat.application.commands import CommandServices
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.message_ir import MessageBody, TextNode, body_to_json
from src.plugins.ai_chat.tasks import RunningTaskRegistry
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred


from src.plugins.ai_chat.application import chat_orchestrator as orchestrator_module
from src.plugins.ai_chat import turn_journal as journal_module


class ConsumerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = fixture.ExecutionTests("test_result_capture_is_exactly_idempotent_and_rejects_changed_body")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.deliveries = DeliveryStore(self.f.database, recover_interrupted=False)
        self.journal = journal_module.TurnJournal(self.f.database)
        self.addCleanup(self.deliveries.close)
        self.addCleanup(self.journal.close)
        self.f.window(source="TEST_INPUT")
        with self.f.tx() as cursor:
            observe_ready(cursor, self.f.token, {"op": 0, "t": "READY", "d": {
                "session_id": "TEST_SESSION", "user": {"id": "TEST_BOT", "bot": True}}}, self.f.now)
        self.outbox = OfficialSendStore(self.deliveries, admission=self.f.admission,
            pepper=b"TEST_CONSUMER_PEPPER" * 3, clock=lambda: self.f.now)
        self.transport = TransportState(self.f.account, True, True)
        self.agent = AsyncMock(return_value="TEST final")
        self.profile = SimpleNamespace(provider_identity="TEST", model="TEST_MODEL", name="TEST_PROFILE")

    async def asyncSetUp(self):
        self.calls = DatabaseCalls()
        ports = orchestrator_module.ChatPorts(
            conversation_id=lambda inbound: inbound.endpoint.key, endpoint_enabled=lambda _: True,
            group_default_profile=lambda _: None, reply_target_turn=lambda _: None,
            record_trigger=Mock(), current_turn_context=lambda *_: "", drain_feedback=lambda _: [],
            ask_agent=self.agent, is_silence_reply=lambda answer: answer == "TEST_SILENCE",
            journal_reply_text=str)
        self.tasks = RunningTaskRegistry()
        self.orchestrator = orchestrator_module.ChatOrchestrator(ports=ports, running_tasks=self.tasks,
            model_preferences=SimpleNamespace(get_explicit=lambda _: None),
            model_catalog=SimpleNamespace(resolve_preference=lambda _: self.profile),
            logger=Mock(), prompt_version="TEST", db_call=self.calls)
        self.consumer = OfficialTurnConsumer(api=execution, token=self.f.token,
            store=SimpleNamespace(admitted=self.f.admission), outbox=self.outbox,
            journal=self.journal, database_calls=self.calls, orchestrator=self.orchestrator,
            command_services=CommandServices(running_tasks=self.tasks),
            transport_state=lambda: self.transport, clock=lambda: self.f.now)

    async def asyncTearDown(self):
        await self.calls.close()

    async def test_shared_orchestrator_creates_one_durable_result_and_m3_plan(self):
        self.assertEqual(await self.consumer(self.f.job), {"status": "handed_off"})
        self.agent.assert_awaited_once()
        self.assertEqual(self.f.query("SELECT state FROM qq_official_turn_executions")[0]["state"], "handed_off")
        self.assertEqual(self.f.query("SELECT status,final_text FROM agent_turns")[0]["final_text"], "TEST final")
        self.assertEqual(len(self.f.query("SELECT * FROM delivery_plans")), 1)
        self.assertEqual(self.f.query("SELECT * FROM delivery_receipts"), [])
        await self.consumer(self.f.job)
        self.agent.assert_awaited_once()
        self.assertEqual(len(self.f.query("SELECT * FROM delivery_plans")), 1)

    async def test_failed_handoff_retries_frozen_result_without_model(self):
        with patch.object(self.outbox, "enqueue", side_effect=RuntimeError("TEST_DB")):
            with self.assertRaisesRegex(JobDeferred, "handoff"):
                await self.consumer(self.f.job)
        self.assertEqual(self.f.query("SELECT state FROM qq_official_turn_executions")[0]["state"], "result_ready")
        await self.consumer(self.f.job)
        self.agent.assert_awaited_once()

    async def test_model_failure_never_reenters_model_after_recovery(self):
        self.agent.side_effect = RuntimeError("TEST_PRIVATE_MODEL_FAILURE")
        with self.assertRaisesRegex(JobDeferred, "official_turn_interrupted"):
            await self.consumer(self.f.job)
        result = await self.consumer(self.f.job)
        self.assertEqual(result["status"], "needs_attention")
        self.agent.assert_awaited_once()
        self.assertEqual(self.f.query("SELECT * FROM delivery_plans"), [])

    async def test_command_is_real_shared_router_without_model_journal(self):
        with self.f.tx() as cursor:
            cursor.execute("UPDATE messages SET body_json=? WHERE canonical_message_id=?",
                (body_to_json(MessageBody((TextNode(0, "/help"),))), self.f.message.canonical_message_id))
            cursor.execute("UPDATE qq_official_inbound_events SET turn_text='/help',turn_text_version=1 WHERE inbox_id=?",
                           (self.f.inbox,))
        result = await self.consumer(self.f.job)
        self.assertEqual(result["status"], "handed_off")
        self.agent.assert_not_awaited()
        self.assertEqual(self.f.query("SELECT * FROM agent_turns"), [])
        self.assertIn("/model", self.f.query("SELECT result_body_json FROM qq_official_turn_executions")[0]["result_body_json"])

    async def test_cancelled_model_is_not_swallowed_or_restarted(self):
        self.agent.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.consumer(self.f.job)
        self.assertEqual((await self.consumer(self.f.job))["status"], "needs_attention")
        self.agent.assert_awaited_once()

    async def test_silence_has_no_fabricated_send(self):
        self.agent.return_value = "TEST_SILENCE"
        self.assertEqual(await self.consumer(self.f.job), {"status": "silent"})
        self.assertEqual(self.f.query("SELECT * FROM delivery_plans"), [])
        self.assertEqual(self.f.query("SELECT state,plan_id FROM qq_official_turn_executions")[0]["state"], "handed_off")


if __name__ == "__main__":
    unittest.main()
