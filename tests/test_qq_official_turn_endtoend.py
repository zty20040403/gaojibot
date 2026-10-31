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
from unittest.mock import patch

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

    async def test_unfinished_messages_share_one_reply_with_complete_input(self):
        from src.plugins.ai_chat.adapters.qq_official.message_batch import MessageBatcher
        app=self.application()
        calls=[]
        async def completion(**kwargs):
            calls.append(kwargs)
            return model_response('TEST complete answer',[],kwargs)
        with patch('src.plugins.ai_chat.agent_loop._create_completion',new=completion), \
                patch('src.plugins.ai_chat.adapters.qq_official.message_batch.MessageBatcher',
                      side_effect=lambda dispatch:MessageBatcher(dispatch,idle=.3,maximum=2)):
            async with verified_ready_platform() as fake:
                runtime=await self.open_runtime(fake,app)
                await self.emit(fake,native='TEST_BATCH_UNBOUND',content='TEST setup',group=True)
                await self.wait_db("SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id='TEST_BATCH_UNBOUND'")
                endpoint=EndpointKey(runtime.loaded.account,'group','TEST_GROUP')
                with runtime.store.transaction() as cursor:
                    cursor.execute("UPDATE qq_official_endpoints SET binding_state='active' WHERE endpoint_key=?",(endpoint.key,))
                for index,text in enumerate(('那个','明天的会','改到几点了')):
                    await self.emit(fake,native=f'TEST_BATCH_{index}',content=text,group=True)
                await self.complete('TEST_BATCH_0')
                self.assertEqual(len(calls),1)
                messages=json.dumps(calls[0]['messages'],ensure_ascii=False)
                for text in ('那个','明天的会','改到几点了'):
                    self.assertIn(text,messages)
                self.assertEqual(len(fake.received),1)
                self.assertEqual(len(self.query('SELECT * FROM qq_official_turn_executions')),1)
                await runtime.stop()

    async def test_proactive_debounces_and_limits_conversation_to_eight_per_hour(self):
        from src.plugins.ai_chat.adapters.qq_official.native_background_factory import background_factory, ProactiveConfiguration
        app=self.application()
        with patch('src.plugins.ai_chat.application.shared_proactive.ask_deepseek_json',
                   return_value={'reply':'TEST spontaneous reply'}) as model, \
                patch('src.plugins.ai_chat.adapters.qq_official.proactive_ingress.DEBOUNCE_SECONDS',.15):
            async with verified_ready_platform() as fake:
                runtime=self.runtime(loaded=runtime_fixture.loaded_config(fake.app_id,base=fake.base_url),
                    application=app,ledger=app.message_ledger,journal=app.turn_journal,
                    background_factory=background_factory(proactive_configuration=ProactiveConfiguration(enabled=True),proactive_profile='test'))
                self.assertTrue(await runtime.start())
                await asyncio.wait_for(fake.wait_ready(),5)
                await self.wait_db("SELECT bot_native_id FROM qq_official_accounts WHERE bot_identity_source='ready'")
                await self.emit(fake,native='TEST_PROACTIVE_SETUP',content='TEST setup',group=True)
                await self.wait_db("SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id='TEST_PROACTIVE_SETUP'")
                endpoint=EndpointKey(runtime.loaded.account,'group','TEST_GROUP')
                with runtime.store.transaction() as cursor:
                    cursor.execute("UPDATE qq_official_endpoints SET binding_state='active' WHERE endpoint_key=?",(endpoint.key,))
                fake.proactive_allowed[('group','TEST_GROUP')]=True
                await runtime.sender.refresh_bound_groups()
                async def emit(index):
                    await fake.emit('GROUP_MESSAGE_CREATE',{
                        'id':f'TEST_PASSIVE_{index}','content':f'TEST conversation topic {index}',
                        'timestamp':datetime.now(timezone.utc).isoformat(),
                        'group_openid':'TEST_GROUP','author':{'member_openid':'TEST_MEMBER'}})
                for index in ('burst1','burst2','0'):
                    await emit(index)
                for index in range(8):
                    if index:await emit(index)
                    await self.wait_db("SELECT count(*) AS count FROM qq_official_proactive_observations WHERE state='queued'",
                        predicate=lambda rows:rows[0]['count']==index+1)
                await emit(8)
                await self.wait_db("SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id='TEST_PASSIVE_8'")
                await asyncio.sleep(.3)
                self.assertEqual(model.call_count,8)
                self.assertEqual(len(self.query("SELECT plan_id FROM qq_official_proactive_observations WHERE state='queued'")),8)
                await runtime.stop()

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
