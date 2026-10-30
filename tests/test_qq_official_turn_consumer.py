import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.plugins.ai_chat.adapters.qq_official import turn_source as execution
from tests import qq_official_turn_fixture as fixture
from src.plugins.ai_chat.adapters.qq_official.turns import OfficialTurnConsumer
from src.plugins.ai_chat.adapters.qq_official.turn_platform import TransportState
from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
from src.plugins.ai_chat.application.commands import CommandServices
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.message_ir import MessageBody, TextNode, body_to_json
from src.plugins.ai_chat.tasks import RunningTaskRegistry


from src.plugins.ai_chat.application import chat_orchestrator as orchestrator_module
from src.plugins.ai_chat import turn_journal as journal_module


class ConsumerTests(unittest.IsolatedAsyncioTestCase):
    async def test_streamed_section_is_queued_before_model_finishes_without_final_duplicate(self):
        from src.plugins.ai_chat.agent_loop import _create_streaming_completion
        sections = ["### 一\n表格\n\n- 列表", "\n\n### 二\n剩余回答"]

        async def chunks():
            for piece in sections:
                yield SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=piece, tool_calls=[]), finish_reason=None)])

        async def model(_inbound, _platform, _text, **kwargs):
            with patch("src.plugins.ai_chat.agent_loop._invoke_completion", AsyncMock(return_value=chunks())):
                result, emitted = await _create_streaming_completion(kwargs['final_stream_sink'], self.profile)
            self.assertEqual(emitted, sections[0] + "\n\n")
            rows = self.f.query("SELECT body_json,turn_id FROM deliveries ORDER BY delivery_id")
            self.assertEqual(len(rows), 1)
            self.assertIn("列表", rows[0]['body_json'])
            self.assertIsNotNone(rows[0]['turn_id'])
            self.assertEqual(self.f.query("SELECT state FROM qq_official_turn_executions")[0]['state'], 'executing')
            return result.choices[0].message.content[len(emitted):]

        self.agent.side_effect = model
        self.assertEqual(await self.consumer(self.f.job), {'status': 'handed_off'})
        rows = self.f.query("SELECT body_json FROM deliveries ORDER BY delivery_id")
        self.assertEqual(len(rows), 2)
        self.assertNotIn("列表", rows[1]['body_json'])
        self.assertIn("剩余回答", rows[1]['body_json'])

    def setUp(self):
        self.f = fixture.ExecutionFixture("runTest")
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
        self.assertIn("/model", self.f.query("SELECT manifest_json FROM delivery_plans")[0]["manifest_json"])

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
