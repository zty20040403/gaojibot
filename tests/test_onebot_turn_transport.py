"""The new transport port uses real OneBot dispatch with only fake platform I/O."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, Mock

import nonebot
nonebot.init()

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment, PrivateMessageEvent

from src.plugins.ai_chat.adapters.onebot_turn import OneBotTurnPlatform
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import SendRequest


def incoming(*, private=False, quote=None):
    message = Message([MessageSegment.text("TEST_QUESTION")])
    if quote is not None:
        message.insert(0, MessageSegment.reply(quote))
    fields = dict(
        time=1700000000, self_id=999, post_type="message", user_id=123, message_id=456,
        message=message, original_message=message, raw_message=str(message), font=0,
        sender={"user_id": 123, "nickname": "TEST_SENDER", "role": "member"},
    )
    if private:
        return PrivateMessageEvent(**fields, message_type="private", sub_type="friend")
    return GroupMessageEvent(**fields, group_id=789, message_type="group", sub_type="normal")


def transport(*, private=False, quote=None, response=None):
    bot = Bot(Mock(), "999")
    bot.call_api = AsyncMock(return_value=response if response is not None else {"message_id": 900001})
    # A history alias is deliberately different from the actual transport target.
    port = OneBotTurnPlatform(
        bot, incoming(private=private, quote=quote),
        canonical_scope=ConversationScope("onebot-v11", "group", "900999"),
    )
    return port, bot


def request(port, *, reply=None, endpoint=None, actor=None, epoch=0):
    return SendRequest(
        endpoint or port.endpoint, actor or port.inbound.actor,
        MessageBody((TextNode(0, "TEST_REPLY"),)), "TEST_DELIVERY", epoch,
        reply_to_native_message_id=reply,
    )


class OneBotTurnTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_unverified_reply_targets_and_incoming_quote_cause_no_io(self):
        port, bot = transport(quote=900099)
        self.assertEqual(port.inbound.reply_to_native_message_id, "900099")
        for target in ("900099", "900888", "TEST_FOREIGN_ID", "0456"):
            with self.subTest(target=target):
                result = await port.send(request(port, reply=target))
                self.assertEqual((result.status, result.reason), ("failed", "unverified_reply_target"))
        bot.call_api.assert_not_awaited()

    async def test_unverified_recall_and_human_incoming_message_cause_no_io(self):
        port, bot = transport()
        for target in ("900099", "TEST_FOREIGN_ID", port.inbound.native_message_id):
            with self.subTest(target=target):
                self.assertFalse(await port.recall(port.endpoint, target))
        bot.call_api.assert_not_awaited()

    async def test_current_inbound_can_be_quoted_in_real_group_and_private_dispatch(self):
        for private in (False, True):
            with self.subTest(private=private):
                port, bot = transport(private=private)
                result = await port.send(request(port, reply="456"))
                self.assertEqual(result.status, "committed")
                call = bot.call_api.await_args
                self.assertEqual(call.args, ("send_msg",))
                self.assertEqual(call.kwargs["message_type"], "private" if private else "group")
                self.assertEqual(call.kwargs["user_id"], 123)
                if not private:
                    self.assertEqual(call.kwargs["group_id"], 789)
                self.assertEqual([(item.type, item.data) for item in call.kwargs["message"]], [
                    ("reply", {"id": "456"}), ("text", {"text": "TEST_REPLY"}),
                ])
                self.assertEqual(bot.call_api.await_count, 1)

    async def test_committed_receipt_allows_quote_and_exactly_one_recall(self):
        port, bot = transport()
        bot.call_api.side_effect = [{"message_id": 900001}, {"message_id": 900002}, None]
        first = await port.send(request(port))
        second = await port.send(request(port, reply=first.native_message_id))
        self.assertEqual(second.status, "committed")
        self.assertTrue(await port.recall(port.endpoint, first.native_message_id))
        self.assertEqual(bot.call_api.await_args.args, ("delete_msg",))
        self.assertEqual(bot.call_api.await_args.kwargs, {"message_id": 900001})
        self.assertFalse(await port.recall(port.endpoint, first.native_message_id))
        blocked = await port.send(request(port, reply=first.native_message_id))
        self.assertEqual(blocked.reason, "unverified_reply_target")
        self.assertEqual(bot.call_api.await_count, 3)

    async def test_same_endpoint_in_another_turn_does_not_inherit_receipt_permissions(self):
        first, first_bot = transport()
        receipt = await first.send(request(first))
        second, second_bot = transport()
        self.assertFalse(await second.recall(second.endpoint, receipt.native_message_id))
        result = await second.send(request(second, reply=receipt.native_message_id))
        self.assertEqual(result.reason, "unverified_reply_target")
        self.assertEqual(first_bot.call_api.await_count, 1)
        second_bot.call_api.assert_not_awaited()

    async def test_wrong_endpoint_account_or_actor_cannot_reuse_permission(self):
        port, bot = transport()
        receipt = await port.send(request(port))
        bot.call_api.reset_mock()
        other_account = replace(port.account, app_id="998")
        endpoints = (
            replace(port.endpoint, native_conversation_id="790"),
            replace(port.endpoint, account=other_account),
            replace(port.endpoint, kind="private", native_conversation_id="123"),
        )
        for endpoint in endpoints:
            actor = replace(port.inbound.actor, account=endpoint.account)
            with self.subTest(endpoint=endpoint.key):
                with self.assertRaises(ValueError):
                    await port.send(request(port, endpoint=endpoint, actor=actor))
                with self.assertRaises(ValueError):
                    await port.recall(endpoint, receipt.native_message_id)
        with self.assertRaises(ValueError):
            await port.send(request(port, actor=replace(port.inbound.actor, native_user_id="124")))
        bot.call_api.assert_not_awaited()
        self.assertTrue(await port.recall(port.endpoint, receipt.native_message_id))

    async def test_stale_route_is_rejected_before_platform_io(self):
        port, bot = transport()
        result = await port.send(request(port, epoch=1))
        self.assertEqual((result.status, result.reason), ("failed", "stale_route"))
        bot.call_api.assert_not_awaited()

    async def test_malformed_receipts_do_not_grant_quote_or_recall_permissions(self):
        invalid = (None, {}, {"message_id": True}, {"message_id": False},
                   {"message_id": 900001.0}, {"message_id": "TEST_OPAQUE_ID"},
                   {"message_id": ""}, {"message_id": " 900001 "}, {"message_id": "+900001"},
                   {"message_id": "９００００１"})
        for response in invalid:
            with self.subTest(response=response):
                port, bot = transport()
                bot.call_api.return_value = response
                result = await port.send(request(port))
                self.assertEqual(result.status, "ambiguous")
                candidate = str(response.get("message_id")) if isinstance(response, dict) else "900001"
                self.assertFalse(await port.recall(port.endpoint, candidate))
                blocked = await port.send(request(port, reply=candidate or "900001"))
                self.assertEqual(blocked.reason, "unverified_reply_target")
                self.assertEqual(bot.call_api.await_count, 1)

    async def test_integer_receipts_are_canonicalized_only_for_onebot(self):
        for value, expected in ((900001, "900001"), ("900001", "900001"),
                                (-900001, "-900001"), ("-900001", "-900001"), ("0900001", "900001")):
            with self.subTest(value=value):
                port, bot = transport(response={"message_id": value})
                receipt = await port.send(request(port))
                self.assertEqual((receipt.status, receipt.native_message_id), ("committed", expected))
                self.assertTrue(await port.recall(port.endpoint, expected))
                self.assertEqual(bot.call_api.await_args.kwargs, {"message_id": int(expected)})

    async def test_send_exception_has_no_retry_and_grants_no_native_target(self):
        port, bot = transport()
        bot.call_api.side_effect = TimeoutError("TEST_SEND_UNKNOWN")
        result = await port.send(request(port))
        self.assertEqual((result.status, result.reason), ("ambiguous", "onebot_send_outcome_unknown"))
        self.assertFalse(await port.recall(port.endpoint, "900001"))
        blocked = await port.send(request(port, reply="900001"))
        self.assertEqual(blocked.reason, "unverified_reply_target")
        self.assertEqual(bot.call_api.await_count, 1)

    async def test_concurrent_recall_consumes_permission_before_io(self):
        port, bot = transport()
        receipt = await port.send(request(port))
        entered, release = asyncio.Event(), asyncio.Event()

        async def delete(api, **params):
            self.assertEqual(api, "delete_msg")
            entered.set()
            await release.wait()

        bot.call_api.side_effect = delete
        first = asyncio.create_task(port.recall(port.endpoint, receipt.native_message_id))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(await asyncio.wait_for(port.recall(port.endpoint, receipt.native_message_id), 1))
        finally:
            release.set()
            first_result = await first
        self.assertTrue(first_result)
        self.assertEqual(bot.call_api.await_count, 2)

    async def test_failed_or_cancelled_recall_cannot_be_replayed(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                port, bot = transport()
                receipt = await port.send(request(port))
                entered = asyncio.Event()

                async def delete(api, **params):
                    entered.set()
                    if cancelled:
                        await asyncio.Event().wait()
                    raise TimeoutError("TEST_RECALL_UNKNOWN")

                bot.call_api.side_effect = delete
                first = asyncio.create_task(port.recall(port.endpoint, receipt.native_message_id))
                await asyncio.wait_for(entered.wait(), 1)
                if cancelled:
                    first.cancel()
                with self.assertRaises(asyncio.CancelledError if cancelled else TimeoutError):
                    await first
                self.assertFalse(await port.recall(port.endpoint, receipt.native_message_id))
                self.assertEqual(bot.call_api.await_count, 2)


if __name__ == "__main__":
    unittest.main()
