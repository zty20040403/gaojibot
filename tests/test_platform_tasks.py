"""Task control is authorized by the actual account/endpoint/actor, not history."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import unittest

import nonebot

nonebot.init()

from src.plugins.ai_chat.platform_types import (
    AccountKey, ActorKey, EndpointKey, InboundMessage,
)
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.tasks import LegacyTaskIdentity, RunningTaskRegistry


def inbound(*, app="TEST_APP_A", environment="production", group="TEST_GROUP_A",
            actor="TEST_MEMBER", message="TEST_MESSAGE", platform="qq-official",
            canonical_scope=None):
    account = AccountKey(platform, app, environment)
    endpoint = EndpointKey(account, "group" if group is not None else "private", group or actor)
    identity_kind = ("group_member" if group is not None else "c2c_user") if platform == "qq-official" else "user"
    identity_namespace = group if platform == "qq-official" and group is not None else ""
    return InboundMessage(
        endpoint=endpoint,
        actor=ActorKey(account, identity_kind, identity_namespace, actor),
        event_id="TEST_EVENT", native_message_id=message,
        sender_display="Test user", platform_role="member", body=MessageBody((TextNode(0, "test"),)),
        mentions_bot=True, reply_to_native_message_id=None, occurred_at=1000,
        reply_credential=None, canonical_scope=canonical_scope,
    )


class PlatformTaskRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = RunningTaskRegistry()
        self.workers = []

    async def asyncTearDown(self):
        for task in self.workers:
            task.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)

    async def start(self, message, *, conversation="shared-history", summary="test task", legacy_identity=None):
        ready = asyncio.get_running_loop().create_future()
        async def worker():
            info = self.registry.register_inbound(
                conversation_id=conversation, inbound=message, summary=summary,
                legacy_identity=legacy_identity,
            )
            ready.set_result(info)
            try:
                await asyncio.Event().wait()
            finally:
                self.registry.finish(info.task_id)
        task = asyncio.create_task(worker())
        self.workers.append(task)
        # Surface validation exceptions instead of hanging a failed helper task.
        done, _ = await asyncio.wait((ready, task), return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
        return ready.result(), task

    async def start_legacy(self, *, conversation="shared-history", user=2, group=9, message=101):
        ready = asyncio.get_running_loop().create_future()
        async def worker():
            info = self.registry.register_current(
                conversation_id=conversation, user_id=user, group_id=group,
                message_id=message, summary="legacy task",
            )
            ready.set_result(info)
            try:
                await asyncio.Event().wait()
            finally:
                self.registry.finish(info.task_id)
        task = asyncio.create_task(worker())
        self.workers.append(task)
        return await ready, task

    def isolation_cases(self):
        return [
            inbound(),
            inbound(app="TEST_APP_B"),
            inbound(environment="sandbox"),
            inbound(group="TEST_GROUP_B"),
            inbound(group=None),
            inbound(actor="TEST_OTHER_MEMBER"),
        ]

    async def test_non_numeric_identity_never_populates_legacy_numeric_fields(self):
        message = inbound(message="TEST_NONNUMERIC_MESSAGE")
        info, _ = await self.start(message, summary="  test\n result  ")
        self.assertEqual((info.user_id, info.group_id, info.message_id), (None, None, None))
        self.assertEqual((info.endpoint, info.actor, info.native_message_id),
                         (message.endpoint, message.actor, "TEST_NONNUMERIC_MESSAGE"))
        self.assertEqual(info.summary, "test result")
        self.assertFalse(info.legacy_visible)
        self.assertEqual(self.registry.list_all(), [info])

    async def test_same_literal_ids_remain_isolated_by_account_environment_group_and_kind(self):
        records = [await self.start(message) for message in self.isolation_cases()]
        for message, (info, _) in zip(self.isolation_cases(), records):
            with self.subTest(endpoint=message.endpoint.key, actor=message.actor.key):
                self.assertEqual(self.registry.list_for_endpoint(message.endpoint, message.actor), [info])
        group_endpoint = self.isolation_cases()[0].endpoint
        self.assertEqual({item.task_id for item in self.registry.list_for_endpoint(group_endpoint)},
                         {records[0][0].task_id, records[-1][0].task_id})
        self.assertEqual(self.registry.list_for("shared-history"), [])

    async def test_cancel_never_crosses_endpoint_or_actor_despite_same_history(self):
        messages = self.isolation_cases()
        records = [await self.start(message) for message in messages]
        victim, victim_task = records[0]
        for other in messages[1:]:
            with self.subTest(endpoint=other.endpoint.key, actor=other.actor.key):
                self.assertIsNone(self.registry.cancel_for_endpoint(other.endpoint, other.actor, victim.task_id))
                self.assertEqual(victim_task.cancelling(), 0)
        # A valid actor from another namespace is not valid at this endpoint.
        self.assertIsNone(self.registry.cancel_for_endpoint(messages[0].endpoint, messages[3].actor, victim.task_id))
        self.assertEqual(self.registry.cancel_for_endpoint(messages[0].endpoint, messages[0].actor, victim.task_id), victim)
        with self.assertRaises(asyncio.CancelledError):
            await victim_task
        self.assertTrue(all(task.cancelling() == 0 for _, task in records[1:]))

    async def test_same_target_literal_in_group_and_private_is_not_the_same_endpoint(self):
        group = inbound(group="TEST_SAME_ID", actor="TEST_SAME_ID")
        private = inbound(group=None, actor="TEST_SAME_ID")
        group_info, group_task = await self.start(group)
        private_info, private_task = await self.start(private)
        self.assertEqual(group.endpoint.native_conversation_id, private.endpoint.native_conversation_id)
        self.assertEqual(self.registry.list_for_endpoint(group.endpoint, group.actor), [group_info])
        self.assertEqual(self.registry.list_for_endpoint(private.endpoint, private.actor), [private_info])
        self.assertIsNone(self.registry.cancel_for_endpoint(group.endpoint, private.actor, group_info.task_id))
        self.assertIsNone(self.registry.cancel_for_endpoint(private.endpoint, private.actor, group_info.task_id))
        self.assertIsNone(self.registry.push_feedback_for_endpoint(
            "wrong domain", endpoint=group.endpoint, actor=private.actor, task_id=group_info.task_id))
        self.assertEqual((group_task.cancelling(), private_task.cancelling()), (0, 0))

    async def test_mutating_scoped_operations_require_an_explicit_actor(self):
        message = inbound()
        info, task = await self.start(message)
        with self.assertRaises(TypeError):
            self.registry.cancel_for_endpoint(message.endpoint, None, info.task_id)
        with self.assertRaises(TypeError):
            self.registry.push_feedback_for_endpoint("no actor", endpoint=message.endpoint,
                                                     actor=None, task_id=info.task_id)
        with self.assertRaises(TypeError):
            self.registry.drain_feedback_for_endpoint(message.endpoint, None, info.task_id)
        self.assertEqual(task.cancelling(), 0)

    async def test_cancel_latest_only_targets_requesting_actor(self):
        message = inbound()
        first, first_task = await self.start(message)
        second, second_task = await self.start(replace(message, native_message_id="TEST_SECOND"))
        other, other_task = await self.start(inbound(actor="TEST_OTHER_MEMBER"))
        self.assertIsNone(self.registry.cancel_for_endpoint(message.endpoint, message.actor, "TEST_MISSING_TASK"))
        self.assertEqual(self.registry.cancel_for_endpoint(message.endpoint, message.actor), second)
        with self.assertRaises(asyncio.CancelledError):
            await second_task
        self.assertEqual(first_task.cancelling(), 0)
        self.assertEqual(other_task.cancelling(), 0)
        self.assertEqual(self.registry.list_for_endpoint(message.endpoint, message.actor), [first])
        self.assertIn(other, self.registry.list_for_endpoint(message.endpoint))

    async def test_feedback_and_drain_are_scoped_to_endpoint_and_actor(self):
        messages = self.isolation_cases()
        records = [await self.start(message) for message in messages]
        victim = records[0][0]
        self.assertEqual(self.registry.push_feedback_for_endpoint(
            "  use\n revised plan ", endpoint=messages[0].endpoint, actor=messages[0].actor,
            task_id=victim.task_id), victim)
        for other in messages[1:]:
            self.assertIsNone(self.registry.push_feedback_for_endpoint(
                "unauthorized", endpoint=other.endpoint, actor=other.actor, task_id=victim.task_id))
            self.assertEqual(self.registry.drain_feedback_for_endpoint(other.endpoint, other.actor, victim.task_id), [])
        self.assertEqual(self.registry.drain_feedback_for_endpoint(messages[0].endpoint, messages[0].actor, victim.task_id),
                         ["use revised plan"])
        self.assertEqual(self.registry.drain_feedback_for_endpoint(messages[0].endpoint, messages[0].actor, victim.task_id), [])

    async def test_feedback_explicit_unknown_or_conflicting_selector_never_falls_back(self):
        message = inbound()
        first, _ = await self.start(message)
        second, _ = await self.start(replace(message, native_message_id="TEST_SECOND"))
        for selector in ({"task_id": "TEST_UNKNOWN_TASK"}, {"reply_message_id": "TEST_UNKNOWN_MESSAGE"},
                         {"task_id": second.task_id, "reply_message_id": message.native_message_id}):
            with self.subTest(selector=selector):
                self.assertIsNone(self.registry.push_feedback_for_endpoint(
                    "do not inject", endpoint=message.endpoint, actor=message.actor, **selector))
        self.assertEqual(self.registry.drain_feedback(first.task_id), [])
        self.assertEqual(self.registry.drain_feedback(second.task_id), [])
        selected = self.registry.push_feedback_for_endpoint(
            "replied first task", endpoint=message.endpoint, actor=message.actor,
            reply_message_id=message.native_message_id)
        self.assertEqual(selected, first)
        self.assertEqual(self.registry.drain_feedback(first.task_id), ["replied first task"])
        with self.assertRaises(TypeError):
            self.registry.push_feedback_for_endpoint("bad integer", endpoint=message.endpoint,
                                                     actor=message.actor, reply_message_id=101)

    async def test_canonical_history_mapping_cannot_authorize_legacy_control(self):
        scope = ConversationScope("onebot-v11", "group", "9")
        message = inbound(group="9", actor="2", message="101", canonical_scope=scope)
        official, official_task = await self.start(message)
        legacy, _ = await self.start_legacy()
        self.assertEqual(self.registry.list_for("shared-history"), [legacy])
        self.assertEqual(self.registry.list_for_group(9), [legacy])
        self.assertIsNone(self.registry.cancel("shared-history", official.task_id))
        self.assertIsNone(self.registry.cancel_for_group(9, official.task_id))
        self.assertEqual(official_task.cancelling(), 0)
        # Keep the old feedback fallback semantics, but it may only select legacy.
        selected = self.registry.push_feedback("legacy note", group_id=9, task_id=official.task_id)
        self.assertEqual(selected, legacy)
        self.assertEqual(self.registry.drain_feedback(official.task_id), [])
        self.assertEqual(self.registry.list_for_endpoint(message.endpoint, message.actor), [official])

    async def test_onebot_explicit_legacy_metadata_preserves_existing_queries_and_fields(self):
        for group in ("9", None):
            with self.subTest(group=group):
                message = inbound(platform="onebot-v11", group=group, actor="2", message="-101")
                metadata = LegacyTaskIdentity(2, 9 if group else None, -101)
                conversation = f"TEST_LEGACY_{group}"
                info, task = await self.start(message, conversation=conversation, legacy_identity=metadata)
                self.assertEqual((info.user_id, info.group_id, info.message_id),
                                 (metadata.user_id, metadata.group_id, metadata.message_id))
                self.assertTrue(info.legacy_visible)
                self.assertEqual(self.registry.list_for(conversation), [info])
                self.assertEqual(self.registry.list_for_endpoint(message.endpoint, message.actor), [info])
                self.assertEqual(self.registry.cancel(conversation, info.task_id), info)
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def test_legacy_metadata_cannot_fake_qq_numbers_or_mismatch_onebot_input(self):
        official = inbound(group="9", actor="2", message="101")
        with self.assertRaisesRegex(ValueError, "only valid for OneBot"):
            self.registry.register_inbound(conversation_id="same", inbound=official, summary="bad", legacy_identity=LegacyTaskIdentity(2, 9, 101))
        onebot = inbound(platform="onebot-v11", group="9", actor="2", message="101")
        invalid = [LegacyTaskIdentity(3, 9, 101), LegacyTaskIdentity(2, 8, 101),
                   LegacyTaskIdentity(2, None, 101), LegacyTaskIdentity(2, 9, 102)]
        for metadata in invalid:
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                self.registry.register_inbound(conversation_id="same", inbound=onebot, summary="bad", legacy_identity=metadata)
        private = inbound(platform="onebot-v11", group=None, actor="2", message="101")
        with self.assertRaises(ValueError):
            self.registry.register_inbound(conversation_id="same", inbound=private, summary="bad", legacy_identity=LegacyTaskIdentity(2, 9, 101))
        with self.assertRaises(ValueError):
            LegacyTaskIdentity(True, None, 101)
        self.assertEqual(self.registry.list_all(), [])

    async def test_structured_onebot_without_explicit_legacy_metadata_stays_structured(self):
        message = inbound(platform="onebot-v11", group="9", actor="2", message="101")
        info, _ = await self.start(message)
        self.assertEqual((info.user_id, info.group_id, info.message_id), (None, None, None))
        self.assertEqual(self.registry.list_for_group(9), [])
        self.assertEqual(self.registry.list_for("shared-history"), [])
        self.assertEqual(self.registry.list_for_endpoint(message.endpoint, message.actor), [info])

    async def test_finished_tasks_are_removed_from_scoped_views_and_feedback(self):
        message = inbound()
        info, task = await self.start(message)
        self.registry.push_feedback_for_endpoint("pending", endpoint=message.endpoint,
                                                 actor=message.actor, task_id=info.task_id)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.registry.list_for_endpoint(message.endpoint, message.actor), [])
        self.assertEqual(self.registry.drain_feedback_for_endpoint(message.endpoint, message.actor, info.task_id), [])

    async def test_global_shutdown_still_cleans_both_legacy_and_structured_tasks(self):
        await self.start(inbound())
        await self.start_legacy()
        self.assertEqual(await self.registry.cancel_all_and_wait(), 2)
        self.assertEqual(self.registry.list_all(), [])


if __name__ == "__main__":
    unittest.main()
