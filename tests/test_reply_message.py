from __future__ import annotations

import unittest

import nonebot

nonebot.init()

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment




from tests.handler_fixture import HandlerTestCaseMixin



def _group_event() -> GroupMessageEvent:
    return GroupMessageEvent(
        time=1,
        self_id=999,
        post_type="message",
        sub_type="normal",
        user_id=123,
        message_type="group",
        message_id=456,
        message=Message("hello"),
        original_message=Message("hello"),
        raw_message="hello",
        font=0,
        sender={
            "user_id": 123,
            "nickname": "Alice",
            "card": "",
            "role": "member",
        },
        group_id=789,
    )


class ReplyMessageTests(HandlerTestCaseMixin, unittest.TestCase):
    def test_group_reply_mentions_replied_user(self) -> None:
        message = self.handlers.replies._reply_message(_group_event(), "你好")

        self.assertEqual(message[0].type, "reply")
        self.assertEqual(message[1].type, "at")
        self.assertEqual(message[1].data["qq"], "123")
        self.assertEqual(message.extract_plain_text().lstrip(), "你好")

    def test_voice_reply_stays_unquoted(self) -> None:
        message = self.handlers.replies._reply_message(_group_event(), MessageSegment.record(b"audio"))

        self.assertEqual(len(message), 1)
        self.assertEqual(message[0].type, "record")

    def test_retry_message_keeps_reply_and_at(self) -> None:
        original = self.handlers.replies._reply_message(_group_event(), "你好")
        retry = self.handlers.replies._make_retry_message(original)

        self.assertIsInstance(retry, Message)
        self.assertEqual(retry[0].type, "reply")
        self.assertEqual(retry[1].type, "at")
        self.assertEqual(retry[1].data["qq"], "123")
        self.assertEqual(retry.extract_plain_text().lstrip(), "你好")

    def test_reply_does_not_duplicate_an_explicit_at_for_sender(self) -> None:
        content = Message(
            [MessageSegment.at(123), MessageSegment.text(" 你来看一下")]
        )

        message = self.handlers.replies._reply_message(_group_event(), content)

        self.assertEqual(
            [segment.type for segment in message],
            ["reply", "at", "text"],
        )


if __name__ == "__main__":
    unittest.main()
