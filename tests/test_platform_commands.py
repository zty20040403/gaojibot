"""Commands use actual platform identity and explicit host policy, with no SDK."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock

from src.plugins.ai_chat.application.commands import (
    CommandPolicy, CommandResult, CommandServices, ParsedCommand, dispatch_command, parse_command,
)
from src.plugins.ai_chat.conversation_keys import conversation_key
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.message_ir import MessageBody
from src.plugins.ai_chat.model_preferences import ModelPreferenceStore
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey, InboundMessage
from src.plugins.ai_chat.tasks import RunningTaskRegistry


def message(*, app="app-A", environment="test", group="group-A", user="opaque-member", scope=None):
    account = AccountKey("qq-official", app, environment)
    endpoint = EndpointKey(account, "private" if group is None else "group", user if group is None else group)
    actor = ActorKey(account, "c2c_user" if group is None else "group_member", "" if group is None else group, user)
    return InboundMessage(endpoint, actor, "event", "opaque-message", "User", "member", MessageBody(),
        True, None, 100, None, canonical_scope=scope)


class CommandParsingTests(unittest.TestCase):
    def test_known_aliases_and_opaque_binding_secret(self):
        for text, name in (("/模型 profile", "model"), ("!reasoning 高", "effort"),
                           ("/ps", "task"), ("/取消任务 t45", "cancel"), ("/帮助", "help")):
            self.assertEqual(parse_command(text).name, name)
        parsed = parse_command("/bind SECRET-WITH-NONNUMERIC-IDENTITY")
        self.assertEqual(parsed.argument, "SECRET-WITH-NONNUMERIC-IDENTITY")
        self.assertNotIn("SECRET", repr(parsed))
        self.assertIsNone(parse_command("ordinary text"))
        self.assertIsNone(parse_command("/unknown secret"))
        self.assertFalse(parse_command("/bind " + "x" * 4097).valid)


class PlatformCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.models = ModelPreferenceStore(root / "models.json")
        self.efforts = ModelPreferenceStore(root / "efforts.json")
        self.registry = RunningTaskRegistry()
        self.workers = []
        self.policy = CommandPolicy(models=("approved-A", "approved-B"), efforts=("low", "high"),
            default_model="approved-A", default_effort="low", allow_model_read=True,
            allow_model_change=True, allow_effort_read=True, allow_effort_change=True)
        self.services = CommandServices(self.registry, self.models, self.efforts, lambda _: self.policy)

    async def asyncTearDown(self):
        for task in self.workers:
            task.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)

    async def run_command(self, text, inbound=None, services=None):
        return await dispatch_command(parse_command(text), inbound or message(), services=services or self.services)

    async def start(self, inbound, summary):
        ready = asyncio.get_running_loop().create_future()
        async def worker():
            info = self.registry.register_inbound(conversation_id=conversation_key(inbound), inbound=inbound, summary=summary)
            ready.set_result(info)
            try:
                await asyncio.Event().wait()
            finally:
                self.registry.finish(info.task_id)
        task = asyncio.create_task(worker())
        self.workers.append(task)
        return await ready, task

    async def test_missing_host_policy_and_platform_owner_cannot_change_models(self):
        inbound = replace(message(), platform_role="owner")
        services = replace(self.services, host_policy=None)
        for text in ("/model", "/model approved-B", "/model group approved-B", "/effort high"):
            self.assertEqual((await self.run_command(text, inbound, services)).status, "denied")
        self.assertEqual(self.models.items(), [])
        self.assertEqual(self.efforts.items(), [])
        self.assertEqual((await self.run_command("/model group approved-B", inbound)).status, "denied")
        self.assertEqual(self.models.items(), [])

    async def test_personal_setting_isolation_covers_all_identity_dimensions(self):
        # Deliberately alias all histories: personal identity remains distinct.
        scope = ConversationScope("onebot-v11", "group", "123")
        inputs = [message(scope=scope), message(app="B", scope=scope), message(environment="sandbox", scope=scope),
                  message(group="B", scope=scope), message(group=None, scope=scope), message(user="other", scope=scope)]
        self.assertEqual((await self.run_command("/model approved-B", inputs[0])).status, "ok")
        self.assertEqual((await self.run_command("/effort 高", inputs[0])).status, "ok")
        for other in inputs[1:]:
            self.assertIsNone(self.models.get_explicit(conversation_key(other)))
            self.assertIsNone(self.efforts.get_explicit(conversation_key(other)))
        self.assertEqual(self.models.get_explicit(conversation_key(inputs[0])), "approved-B")
        self.assertEqual(self.efforts.get_explicit(conversation_key(inputs[0])), "high")
        self.assertEqual((await self.run_command("/model arbitrary-server-model", inputs[0])).status, "denied")
        self.assertEqual((await self.run_command("/effort max", inputs[0])).status, "denied")
        await self.run_command("/model reset", inputs[0])
        await self.run_command("/effort default", inputs[0])
        self.assertIsNone(self.models.get_explicit(conversation_key(inputs[0])))
        self.assertIsNone(self.efforts.get_explicit(conversation_key(inputs[0])))

    async def test_explicit_group_policy_changes_actual_endpoint_only(self):
        alias = ConversationScope("onebot-v11", "group", "123")
        inbound = replace(message(scope=alias), platform_role="member")
        services = replace(self.services, host_policy=lambda _: replace(self.policy, allow_group_default=True))
        self.assertEqual((await self.run_command("/model group approved-B", inbound, services)).status, "ok")
        self.assertEqual((await self.run_command("/effort group high", inbound, services)).status, "ok")
        self.assertEqual(self.models.get_scope_default(inbound.endpoint.scope), "approved-B")
        self.assertEqual(self.efforts.get_scope_member_default(inbound.endpoint.scope), "high")
        self.assertIsNone(self.models.get_scope_default(alias))
        self.assertIsNone(self.efforts.get_scope_member_default(alias))
        self.assertEqual((await self.run_command("/model group approved-A", message(group=None), services)).status, "denied")
        await self.run_command("/model group reset", inbound, services)
        self.assertIsNone(self.models.get_scope_default(inbound.endpoint.scope))

    async def test_revoked_saved_model_not_disclosed_by_list(self):
        inbound = message()
        self.models.set(conversation_key(inbound), "hidden-server-profile")
        result = await self.run_command("/model", inbound)
        self.assertNotIn("hidden-server-profile", result.text)
        self.assertIn("approved-A", result.text)
        self.assertEqual((await self.run_command("/model", services=replace(self.services, host_policy=lambda _: CommandPolicy()))).status, "denied")

    async def test_task_visibility_and_cancel_require_exact_endpoint_actor(self):
        alias = ConversationScope("onebot-v11", "group", "123")
        owner = replace(message(scope=alias), platform_role="owner")
        outsiders = [message(app="B", scope=alias), message(environment="sandbox", scope=alias),
                     message(group="B", scope=alias), message(group=None, scope=alias), message(user="other", scope=alias)]
        own, worker = await self.start(owner, "own task")
        foreign = [await self.start(item, f"foreign secret {index}") for index, item in enumerate(outsiders)]
        result = await self.run_command("/task status", owner)
        self.assertIn(own.task_id, result.text)
        self.assertNotIn("foreign secret", result.text)
        for info, other_worker in foreign:
            self.assertEqual((await self.run_command(f"/task {info.task_id}", owner)).status, "not_found")
            self.assertEqual((await self.run_command(f"/cancel {info.task_id}", owner)).status, "not_found")
            self.assertEqual(other_worker.cancelling(), 0)
        for bad in ("t999", "task#1", "opaque-wrong-id"):
            self.assertEqual((await self.run_command(f"/cancel {bad}", owner)).status, "not_found")
            self.assertEqual(worker.cancelling(), 0)
        self.assertEqual((await self.run_command("/task do a new background job", owner)).status, "unsupported")
        self.assertEqual((await self.run_command(f"/task cancel {own.task_id}", owner)).status, "ok")
        self.assertEqual(worker.cancelling(), 1)

    async def test_cancel_without_id_only_cancels_requesters_latest(self):
        first, first_worker = await self.start(message(), "first")
        second, second_worker = await self.start(message(), "second")
        _, foreign_worker = await self.start(message(user="other"), "foreign")
        result = await self.run_command("/cancel")
        self.assertIn(second.task_id, result.text)
        self.assertNotIn(first.task_id, result.text)
        self.assertEqual(first_worker.cancelling(), 0)
        self.assertEqual(second_worker.cancelling(), 1)
        self.assertEqual(foreign_worker.cancelling(), 0)

    async def test_binding_usage_unknown_and_legacy_escape_are_explicit(self):
        inbound = message()
        secret = "UNPRINTED_BINDING_SECRET"
        result = await self.run_command("/bind " + secret, inbound)
        self.assertEqual(result.status, "unavailable")
        self.assertNotIn(secret, result.text)
        self.assertEqual((await self.run_command("/usage")).status, "unavailable")
        result = await self.run_command("/unknown")
        self.assertEqual(result.status, "unknown")
        self.assertFalse(result.handled)
        callback = AsyncMock()
        result = await dispatch_command(ParsedCommand("model"), inbound, legacy_callback=callback)
        self.assertEqual(result.status, "denied")
        callback.assert_not_called()
        usage = Mock(return_value=CommandResult("ok", "usage for actual endpoint"))
        result = await self.run_command("/usage", inbound, replace(self.services, usage=usage))
        self.assertEqual(result.text, "usage for actual endpoint")
        usage.assert_called_once_with(inbound, ParsedCommand("usage"))
        binding = Mock(side_effect=RuntimeError(secret))
        result = await self.run_command("/bind " + secret, inbound, replace(self.services, binding_handler=binding))
        self.assertEqual(result.status, "unavailable")
        self.assertNotIn(secret, result.text)
