"""Actual gateway, shared model/tool loop, durable jobs and M3 fake transport."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tests import test_qq_official_runtime as runtime_fixture
from tests.test_qq_official_runtime_sender import verified_ready_platform
from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.conversation_window import ConversationStore
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.model_preferences import ModelPreferenceStore
from src.plugins.ai_chat.platform_types import EndpointKey
from src.plugins.ai_chat.quota import UsageStore
from src.plugins.ai_chat.skills import SkillRegistry
from src.plugins.ai_chat.subagents import SubAgentCoordinator, SubAgentStore
from src.plugins.ai_chat.tasks import RunningTaskRegistry
from src.plugins.ai_chat.turn_journal import TurnJournal


def model_response(content, tool_calls, kwargs):
    message=SimpleNamespace(content=content,tool_calls=tool_calls)
    if kwargs.get('stream'):
        async def stream():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=message,finish_reason='stop')])
        return stream()
    return SimpleNamespace(choices=[SimpleNamespace(message=message)],usage=None)


class OfficialTurnEndToEndTests(unittest.IsolatedAsyncioTestCase):
    setUp = runtime_fixture.OfficialRuntimeTests.setUp
    runtime = runtime_fixture.OfficialRuntimeTests.runtime
    query = runtime_fixture.OfficialRuntimeTests.query
    wait_db = runtime_fixture.OfficialRuntimeTests.wait_db

    def application(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        ledger, journal = MessageLedger(self.database), TurnJournal(self.database)
        projection, usage = ConversationStore(self.database), UsageStore(self.database)
        for store in (ledger, journal, projection, usage):
            self.addCleanup(store.close)
        calls = DatabaseCalls()
        self.addAsyncCleanup(calls.close)
        return SimpleNamespace(
            settings=replace(settings, search_enabled=False), db_call=calls,
            logger=self.logger, model_preferences=ModelPreferenceStore(None),
            reasoning_preferences=ModelPreferenceStore(None), running_tasks=RunningTaskRegistry(),
            model_catalog=ModelCatalog.from_json(json.dumps({"default": "test", "profiles": {
                "test": {"provider": "openai", "model": "TEST_MODEL", "api_key_required": False},
                "other": {"provider": "openai", "model": "TEST_OTHER_MODEL", "api_key_required": False},
            }}), default_profile="test", environ={}),
            message_ledger=ledger, turn_journal=journal, conversations=projection, usage_store=usage,
            skill_registry=SkillRegistry(Path(temporary.name) / "skills"),
        )

    async def open_runtime(self, fake, app):
        fake.app_id = "TEST_COMPOSED_" + self._testMethodName
        runtime = self.runtime(loaded=runtime_fixture.loaded_config(fake.app_id, base=fake.base_url),
            application=app, ledger=app.message_ledger, journal=app.turn_journal)
        self.assertTrue(await runtime.start())
        await asyncio.wait_for(fake.wait_ready(), 5)
        await self.wait_db("SELECT bot_native_id FROM qq_official_accounts WHERE bot_identity_source='ready'")
        self.assertTrue(runtime.status()["turn_execution_connected"])
        return runtime

    async def emit(self, fake, *, native, content, group=False):
        if not group:
            fake.proactive_allowed[('user', 'TEST_RUNTIME_OWNER')] = True
        data = {"id": native, "content": content,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "author": {"member_openid" if group else "user_openid": "TEST_MEMBER" if group else "TEST_RUNTIME_OWNER"},
                "message_scene": {"ext": {"msg_idx": "TEST_INDEX_" + native}}}
        if group:
            data.update(group_openid="TEST_GROUP", mentions=[{
                "member_openid": "TEST_BOT_MENTION", "is_you": True, "bot": True, "nickname": "TEST bot"}])
        await fake.emit("GROUP_AT_MESSAGE_CREATE" if group else "C2C_MESSAGE_CREATE", data)

    async def complete(self, native):
        return await self.wait_db("""SELECT x.execution_id,x.state,x.journal_turn_id,x.plan_id,d.status
            FROM qq_official_inbound_events i JOIN qq_official_turn_executions x ON x.inbox_id=i.inbox_id
            JOIN deliveries d ON d.plan_id=x.plan_id
            WHERE i.native_message_id=? AND d.status='committed'""", (native,))

    async def test_actual_group_at_and_c2c_share_model_loop_and_commit_real_wire_receipts(self):
        app = self.application()
        model_calls = []

        async def completion(**kwargs):
            model_calls.append(kwargs)
            names = {tool["function"]["name"] for tool in kwargs["tools"]}
            self.assertFalse(names & {"get_group_member_list", "get_group_file_list", "send_group_forward_msg",
                                     "set_msg_emoji_like", "ocr_image", "get_record", "sandbox_exec"})
            self.assertNotIn("TEST_APP_SECRET", json.dumps(kwargs, default=str))
            return model_response("TEST shared final", [], kwargs)

        with patch("src.plugins.ai_chat.agent_loop._create_completion", new=completion):
            async with verified_ready_platform() as fake:
                runtime = await self.open_runtime(fake, app)
                await self.emit(fake, native="TEST_C2C_TURN", content="TEST question")
                first = (await self.complete("TEST_C2C_TURN"))[0]
                self.assertIsNotNone(first["journal_turn_id"])
                # Establish the synthetic group before installing its trusted
                # test binding. An ordinary incoming message cannot bind it.
                await self.emit(fake, native="TEST_UNBOUND", content="TEST ignored", group=True)
                await self.wait_db("SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id='TEST_UNBOUND'")
                endpoint = EndpointKey(runtime.loaded.account, "group", "TEST_GROUP")

                def bind_fixture():
                    with runtime.store.transaction() as cursor:
                        runtime.store.epoch_guard(cursor, runtime._token)
                        cursor.execute("UPDATE qq_official_endpoints SET binding_state='active' WHERE endpoint_key=?",
                                       (endpoint.key,))
                await runtime._calls(bind_fixture)
                await self.emit(fake, native="TEST_GROUP_TURN", content="<@!TEST_BOT_MENTION> TEST group question", group=True)
                await self.complete("TEST_GROUP_TURN")
                self.assertEqual(len(model_calls), 2)
                self.assertEqual(len(fake.received), 2)
                for received, expected in zip(fake.received, ("TEST_C2C_TURN", "TEST_GROUP_TURN")):
                    self.assertEqual(received['payload'].get('msg_id', expected), expected)
                    self.assertEqual(received['payload'].get('msg_seq',1),1)
                    self.assertEqual(received["payload"]["msg_type"], 2)
                    self.assertEqual(received["payload"]["markdown"]["content"], "TEST shared final")
                self.assertEqual(len(self.query("SELECT * FROM delivery_receipts WHERE applied_at IS NOT NULL")), 2)
                self.assertEqual(len(self.query("SELECT * FROM turn_send_links")), 2)
                self.assertEqual(app.running_tasks.list_all(), [])
                await runtime.stop()

    async def test_command_uses_existing_router_and_never_calls_model(self):
        app = self.application()
        with patch("src.plugins.ai_chat.agent_loop._create_completion", side_effect=AssertionError("command called model")):
            async with verified_ready_platform() as fake:
                runtime = await self.open_runtime(fake, app)
                for ordinal, content in enumerate(("/help", "/model other", "/effort high", "/usage", "/task", "/shell id")):
                    native = f"TEST_COMMAND_{ordinal}"
                    await self.emit(fake, native=native, content=content)
                    await self.complete(native)
                self.assertEqual(self.query("SELECT * FROM agent_turns"), [])
                self.assertEqual(len(fake.received), 6)
                self.assertIn("暂不支持", fake.received[-1]["payload"]["markdown"]["content"])
                values = dict(app.model_preferences.items())
                self.assertEqual(list(values.values()), ["other"])
                self.assertEqual(list(dict(app.reasoning_preferences.items()).values()), ["high"])
                await runtime.stop()

    async def test_foreground_tool_submits_real_background_task_and_routes_result_to_same_endpoint(self):
        app = self.application()
        app.settings = replace(app.settings, subagents_enabled=True, subagent_entry_enabled=True)
        app.subagent_store = SubAgentStore(self.database)
        self.addCleanup(app.subagent_store.close)
        app.subagent_coordinator = SubAgentCoordinator(app.subagent_store, app.model_catalog, logger=self.logger)
        front_calls = []
        worker_calls = []
        result_calls = []
        decision = {"mode": "delegate", "reason": "独立文字分析", "answer": "",
            "task_type": "execution", "delivery_required": False,
            "objective": "TEST 分析文字", "deliverables": ["文字分析"], "constraints": [],
            "acceptance": ["给出文字分析"], "outcome_checks": [{"criterion_index": 0, "kind": "evidence"}],
            "steps": [{"id": "analysis", "agent": "analyst", "objective": "TEST 分析文字",
                       "deliverable": "文字分析", "depends_on": []}]}

        async def completion(**kwargs):
            definitions = {item["function"]["name"]: item for item in kwargs.get("tools",[])}
            if 'delegate_agent' not in definitions:
                result_calls.append(kwargs)
                return model_response('TEST analysis foreground',[],kwargs)
            front_calls.append(kwargs)
            self.assertIn("delegate_agent", definitions)
            self.assertNotIn("resume_subagent", definitions)
            self.assertEqual(definitions["delegate_agent"]["function"]["parameters"]["properties"]["role"]["enum"],
                             ["researcher", "analyst"])
            tool_calls = []
            if len(front_calls) == 1:
                tool_calls = [SimpleNamespace(id="TEST_DELEGATION", type="function", function=SimpleNamespace(
                    name="delegate_agent", arguments=json.dumps({"role": "analyst", "objective": "TEST 分析文字"})))]
            else:
                responses = [message["content"] for message in kwargs["messages"] if message["role"] == "tool"]
                self.assertEqual(len(responses), 1)
                self.assertIn('"ok": true', responses[0])
            return model_response("" if tool_calls else "TEST queued", tool_calls, kwargs)

        async def worker(*args, **kwargs):
            worker_calls.append(kwargs)
            tools = {item["function"]["name"] for item in args[2]}
            self.assertFalse(tools & {"sandbox_exec", "delegate_agent", "run_subagents", "get_group_file_list"})
            # No tool evidence is fabricated: the real verifier must preserve
            # that limitation in the terminal result instead of inventing success.
            return '{"status":"success","summary":"TEST analysis","artifacts":[]}'

        with patch("src.plugins.ai_chat.agent_loop._create_completion", new=completion), \
             patch("src.plugins.ai_chat.subagents.ask_deepseek_json", new=AsyncMock(return_value=decision)), \
             patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", new=worker), \
             patch("src.plugins.ai_chat.subagents.ask_deepseek", new=AsyncMock(return_value="TEST analysis draft")):
            async with verified_ready_platform() as fake:
                runtime = await self.open_runtime(fake, app)
                self.assertTrue(runtime.status()["subagent_execution_connected"])
                await self.emit(fake, native="TEST_DELEGATE_SOURCE", content="TEST delegate analysis")
                await self.complete("TEST_DELEGATE_SOURCE")
                await self.wait_db("SELECT delivery_id FROM deliveries WHERE status='committed' AND purpose='background_result'")
                tasks = self.query("SELECT task_id FROM subagent_tasks")
                self.assertEqual(len(tasks), 1)
                task = app.subagent_store.get(tasks[0]["task_id"])
                self.assertIn(task.status, {"completed", "partial"})
                self.assertIsNone(task.requester_user_id)
                self.assertEqual(task.requester_actor.native_user_id, "TEST_RUNTIME_OWNER")
                self.assertEqual(task.endpoint.native_conversation_id, "TEST_RUNTIME_OWNER")
                self.assertEqual(len(front_calls), 2)
                self.assertEqual(len(worker_calls), 1)
                self.assertEqual(len(result_calls),1)
                self.assertTrue(all('msg_id' not in entry['payload'] for entry in fake.received))
                self.assertTrue(all('msg_seq' not in entry['payload'] for entry in fake.received))
                self.assertTrue(any("TEST queued" in entry["payload"]["markdown"]["content"] for entry in fake.received),
                                fake.received)
                self.assertTrue(task.result["answer"])
                self.assertTrue(any(task.result["answer"] in entry["payload"]["markdown"]["content"]
                                    for entry in fake.received), fake.received)
                await self.emit(fake, native="TEST_TASK_STATUS", content=f"/task {task.handle}")
                await self.complete("TEST_TASK_STATUS")
                self.assertIn(task.handle, fake.received[-1]["payload"]["markdown"]["content"])
                self.assertEqual(len(front_calls), 2)
                await runtime.stop()

    async def test_consumer_factory_failure_leaves_no_gateway_or_worker_task(self):
        app = self.application()
        async with verified_ready_platform() as fake:
            fake.app_id = "TEST_COMPOSED_FACTORY_FAILURE"
            runtime = self.runtime(loaded=runtime_fixture.loaded_config(fake.app_id, base=fake.base_url),
                application=app, ledger=app.message_ledger, journal=app.turn_journal,
                turn_consumer_factory=lambda *_: (_ for _ in ()).throw(RuntimeError("TEST factory")))
            await runtime.start()
            async with asyncio.timeout(5):
                while runtime.state != "paused":
                    await asyncio.sleep(0.01)
                await runtime._idle.wait()
            self.assertIsNone(runtime._gateway)
            self.assertIsNone(runtime.sender)
            self.assertFalse(runtime.status()["turn_execution_connected"])
            self.assertEqual(fake.identify_count, 0)
            self.assertEqual(fake.received, [])
            await runtime.stop()


if __name__ == "__main__":
    unittest.main()
