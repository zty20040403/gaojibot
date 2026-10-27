from __future__ import annotations

from tests.postgres_fixture import postgres_for_test

import unittest
from unittest.mock import AsyncMock

import nonebot
from nonebot.exception import NetworkError

nonebot.init()

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message

from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MediaNode, MessageBody, TextNode
from src.plugins.ai_chat.onebot_model_output import OneBotModelOutputResolver


def group_event(group_id: int = 100) -> GroupMessageEvent:
    return GroupMessageEvent(
        time=1,
        self_id=999,
        post_type="message",
        sub_type="normal",
        user_id=7,
        message_type="group",
        message_id=10,
        message=Message("hello"),
        original_message=Message("hello"),
        raw_message="hello",
        font=0,
        sender={
            "user_id": 7,
            "nickname": "Alice",
            "card": "",
            "role": "member",
        },
        group_id=group_id,
    )


class OneBotModelOutputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.ledger = MessageLedger(postgres_for_test(self))
        self.addCleanup(self.ledger.close)
        self.scope = ConversationScope("onebot-v11", "group", "100")
        self.bot = AsyncMock()
        self.bot.get_group_member_list.return_value = [
            {"user_id": 7, "nickname": "Alice", "role": "member"},
            {"user_id": 88, "card": "Bob", "role": "admin"},
            {"user_id": 999, "nickname": "Bot", "role": "member"},
        ]

    async def test_resolves_principal_to_real_current_group_at(self) -> None:
        principal_id = self.ledger.ensure_principal_identity(
            "onebot-v11", 88, "Bob"
        )
        resolver = OneBotModelOutputResolver(
            self.bot, group_event(), self.ledger
        )

        message = await resolver.render(f"[mention#{principal_id}] 过来看")

        self.assertEqual([segment.type for segment in message], ["at", "text"])
        self.assertEqual(message[0].data["qq"], "88")
        self.assertNotIn("mention#", str(message))

    async def test_member_who_never_spoke_can_be_rescued_by_name(self) -> None:
        resolver = OneBotModelOutputResolver(
            self.bot, group_event(), self.ledger
        )

        message = await resolver.render("@Bob 过来看")

        self.assertEqual(message[0].type, "at")
        self.assertEqual(message[0].data["qq"], "88")
        self.assertIsNotNone(
            self.ledger.principal_id_for_native("onebot-v11", 88)
        )

    async def test_member_api_failure_degrades_to_display_text(self) -> None:
        stored = self.ledger.record_message(
            self.scope,
            native_message_id="bob-before-outage",
            sender_native_user_id="88",
            sender_display="Bob",
            body=MessageBody((TextNode(0, "hello"),)),
        )
        self.bot.get_group_member_list.side_effect = NetworkError("offline")
        resolver = OneBotModelOutputResolver(
            self.bot, group_event(), self.ledger
        )

        message = await resolver.render(
            f"[mention#{stored.sender_principal_id}] 看一下"
        )

        self.assertTrue(all(segment.type != "at" for segment in message))
        self.assertEqual(message.extract_plain_text(), "@Bob 看一下")

    async def test_absent_member_degrades_to_display_text(self) -> None:
        stored = self.ledger.record_message(
            self.scope,
            native_message_id="old-member-message",
            sender_native_user_id="77",
            sender_display="Old Member",
            body=MessageBody((TextNode(0, "hello"),)),
        )
        resolver = OneBotModelOutputResolver(
            self.bot, group_event(), self.ledger
        )

        message = await resolver.render(
            f"[mention#{stored.sender_principal_id}] 在吗"
        )

        self.assertTrue(all(segment.type != "at" for segment in message))
        self.assertEqual(message.extract_plain_text(), "@Old Member 在吗")

    async def test_cross_group_principal_does_not_leak_display_name(self) -> None:
        other_scope = ConversationScope("onebot-v11", "group", "200")
        stored = self.ledger.record_message(
            other_scope,
            native_message_id="private-to-other-group",
            sender_native_user_id="77",
            sender_display="Other Group Secret Name",
            body=MessageBody((TextNode(0, "hello"),)),
        )
        resolver = OneBotModelOutputResolver(
            self.bot, group_event(), self.ledger
        )

        message = await resolver.render(
            f"[mention#{stored.sender_principal_id}] 在吗"
        )

        self.assertTrue(all(segment.type != "at" for segment in message))
        self.assertEqual(message.extract_plain_text(), "@群成员 在吗")
        self.assertNotIn("Secret", str(message))

    async def test_untrusted_caption_cannot_leak_cross_group_name(self) -> None:
        other_scope = ConversationScope("onebot-v11", "group", "200")
        stored = self.ledger.record_message(
            other_scope,
            native_message_id="caption-from-other-group",
            sender_native_user_id="77",
            sender_display="Other Group Secret Name",
            body=MessageBody((TextNode(0, "hello"),)),
        )
        resolver = OneBotModelOutputResolver(
            self.bot, group_event(), self.ledger
        )

        message = await resolver.render(
            f"[mention#{stored.sender_principal_id}: Other Group Secret Name] 在吗"
        )

        self.assertEqual(message.extract_plain_text(), "@群成员 在吗")
        self.assertNotIn("Secret", str(message))

    async def test_hallucinated_principal_never_becomes_a_qq_number(self) -> None:
        resolver = OneBotModelOutputResolver(
            self.bot, group_event(), self.ledger
        )

        message = await resolver.render("[mention#123456789] 在吗")

        self.assertTrue(all(segment.type != "at" for segment in message))
        self.assertEqual(message.extract_plain_text(), "@群成员 在吗")
        self.assertNotIn("123456789", str(message))

    async def test_face_and_in_scope_media_become_native_segments(self) -> None:
        stored = self.ledger.record_message(
            self.scope,
            native_message_id="20",
            sender_native_user_id="7",
            sender_display="Alice",
            body=MessageBody(
                (
                    MediaNode(
                        1,
                        "image",
                        source="https://example.test/image.png",
                        source_type="image",
                    ),
                )
            ),
        )
        resolver = OneBotModelOutputResolver(
            self.bot, group_event(), self.ledger
        )

        message = await resolver.render(
            f"[face#14] [image#{stored.canonical_message_id}.1]"
        )

        self.assertEqual([segment.type for segment in message], ["face", "text", "image"])
        self.assertEqual(message[0].data["id"], "14")
        self.assertEqual(message[2].data["file"], "https://example.test/image.png")

    async def test_curated_extended_face_becomes_native_segment(self) -> None:
        resolver = OneBotModelOutputResolver(
            self.bot, group_event(), self.ledger
        )

        message = await resolver.render("[face#355: 耶]")

        self.assertEqual(len(message), 1)
        self.assertEqual(message[0].type, "face")
        self.assertEqual(message[0].data["id"], "355")

    async def test_media_handle_cannot_cross_group_scope(self) -> None:
        other_scope = ConversationScope("onebot-v11", "group", "200")
        stored = self.ledger.record_message(
            other_scope,
            native_message_id="30",
            sender_native_user_id="7",
            sender_display="Alice",
            body=MessageBody(
                (
                    MediaNode(
                        0,
                        "image",
                        source="https://example.test/secret.png",
                        source_type="image",
                    ),
                )
            ),
        )
        resolver = OneBotModelOutputResolver(
            self.bot, group_event(), self.ledger
        )

        message = await resolver.render(
            f"[image#{stored.canonical_message_id}.0]"
        )

        self.assertTrue(all(segment.type != "image" for segment in message))
        self.assertEqual(message.extract_plain_text(), "[图片不可用]")


if __name__ == "__main__":
    unittest.main()
