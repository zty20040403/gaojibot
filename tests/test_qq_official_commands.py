from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock

from src.plugins.ai_chat.application.commands import CommandPolicy, CommandServices
from src.plugins.ai_chat.conversation_keys import conversation_key
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.model_preferences import ModelPreferenceStore
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey, InboundMessage
from src.plugins.ai_chat.tasks import RunningTaskRegistry

from src.plugins.ai_chat.adapters.qq_official.commands import route_command
def inbound(account=None, *, private=False, user="TEST_USER", group="TEST_GROUP"):
    account = account or AccountKey("qq-official", "TEST_APP", "test")
    endpoint = EndpointKey(account, "private" if private else "group", user if private else group)
    actor = ActorKey(account, "c2c_user" if private else "group_member", "" if private else group, user)
    return InboundMessage(endpoint, actor, "TEST_EVENT", "TEST_NATIVE", "test", "member",
        MessageBody((TextNode(0, "test"),)), True, None, 100, None)


class RouterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.models = ModelPreferenceStore(Path(self.temp.name) / "models.json")
        self.efforts = ModelPreferenceStore(Path(self.temp.name) / "efforts.json")
        self.registry = RunningTaskRegistry()
        self.begin = AsyncMock()
        self.admission = lambda _: True
        self.policy = CommandPolicy(models=("test-model",), efforts=("low", "high"),
            default_model="test-model", default_effort="low", allow_model_read=True,
            allow_model_change=True, allow_effort_read=True, allow_effort_change=True)
        self.services = CommandServices(self.registry, self.models, self.efforts, lambda _: self.policy)
        self.workers = []

    async def asyncTearDown(self):
        for task in self.workers:
            task.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)

    async def route(self, text, message=None, services=None):
        return await route_command(text, message or inbound(), services=services or self.services,
            admission=self.admission, before_effect=self.begin)

    async def test_help_and_unknown_are_explicit_shared_results(self):
        self.assertIn("/model", (await self.route("/help")).result.text)
        result = await self.route("/not-supported TEST_PRIVATE_ARGUMENT")
        self.assertEqual((result.kind, result.result.status), ("reply", "unsupported"))
        self.assertNotIn("TEST_PRIVATE_ARGUMENT", result.result.text + repr(result))
        self.assertEqual((await self.route("plain text")).kind, "not_command")

    async def test_binding_never_reexecutes_or_enters_generic_effects(self):
        callback = AsyncMock(side_effect=AssertionError("second reducer must not execute"))
        services = replace(self.services, binding_handler=callback)
        for text in ("/bind create", "/bind submit BC-FAKE 123456", "/bind confirm BN-FAKE 1", "/whoami"):
            self.assertEqual((await self.route(text, services=services)).kind, "control")
        callback.assert_not_called()
        self.begin.assert_not_called()

    async def test_unbound_unknown_slash_and_normal_commands_do_not_reply(self):
        self.admission = lambda _: False
        for text in ("/help", "/model test-model", "/not-supported"):
            self.assertEqual((await self.route(text)).kind, "suppressed")
        self.assertEqual((await self.route("/whoami")).kind, "control")
        self.begin.assert_not_called()

    async def test_command_must_durably_begin_before_setting_change(self):
        self.begin.side_effect = RuntimeError("TEST_EXECUTION_NOT_BEGUN")
        with self.assertRaises(RuntimeError):
            await self.route("/model test-model")
        self.assertEqual(self.models.items(), [])
        self.begin.side_effect = None
        self.assertEqual((await self.route("/model test-model")).result.status, "ok")
        self.assertEqual((await self.route("/effort high")).result.status, "ok")
        self.assertEqual(self.models.get_explicit(conversation_key(inbound())), "test-model")

    async def test_model_permissions_do_not_come_from_platform_owner(self):
        services = replace(self.services, host_policy=None)
        result = await self.route("/model test-model", replace(inbound(), platform_role="owner"), services)
        self.assertEqual(result.result.status, "denied")
        self.assertEqual((await self.route("/model group test-model")).result.status, "denied")
        self.assertEqual(self.models.items(), [])

    async def start_task(self, message):
        ready = asyncio.get_running_loop().create_future()
        async def run():
            info = self.registry.register_inbound(conversation_id=conversation_key(message), inbound=message, summary="TEST_TASK")
            ready.set_result(info)
            try:
                await asyncio.Event().wait()
            finally:
                self.registry.finish(info.task_id)
        worker = asyncio.create_task(run())
        self.workers.append(worker)
        return await ready, worker

    async def test_task_cancel_uses_entire_actor_endpoint_and_bad_id_has_no_fallback(self):
        owner = inbound()
        info, worker = await self.start_task(owner)
        outsiders = (inbound(user="TEST_OTHER"), inbound(private=True),
            inbound(group="TEST_OTHER_GROUP"), inbound(AccountKey("qq-official", "TEST_OTHER_APP", "test")),
            inbound(AccountKey("qq-official", "TEST_APP", "sandbox")))
        for other in outsiders:
            self.assertEqual((await self.route("/cancel " + info.task_id, other)).result.status, "not_found")
        self.assertEqual((await self.route("/cancel DOES_NOT_EXIST", owner)).result.status, "not_found")
        self.assertFalse(worker.done())
        self.assertEqual((await self.route("/task " + info.task_id, owner)).result.status, "ok")
        self.assertEqual((await self.route("/cancel " + info.task_id, owner)).result.status, "ok")
        await asyncio.sleep(0)
        self.assertTrue(worker.done())

    async def test_usage_without_scoped_host_port_is_explicitly_unavailable(self):
        result = await self.route("/usage")
        self.assertEqual((result.kind, result.result.status), ("reply", "unavailable"))
