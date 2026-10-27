from __future__ import annotations

from tests.postgres_fixture import postgres_for_test

import unittest

import nonebot

nonebot.init()

from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.pins import PinStore


class PinStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = MessageLedger(postgres_for_test(self))
        self.pins = PinStore(postgres_for_test(self), max_per_scope=2)
        self.scope = ConversationScope(
            "onebot-v11", "group", "99", bot_native_user_id="1000"
        )
        self.other = ConversationScope(
            "onebot-v11", "group", "100", bot_native_user_id="1000"
        )

    def tearDown(self) -> None:
        self.pins.close()
        self.ledger.close()

    def _message(self, text: str, native: str):
        return self.ledger.record_message(
            self.scope,
            native_message_id=native,
            sender_native_user_id="1",
            sender_display="Alice",
            body=MessageBody((TextNode(0, text),)),
        )

    def test_pin_is_scope_bound_and_idempotent(self) -> None:
        message = self._message("important choice", "10")
        first, created = self.pins.pin(
            self.ledger,
            self.scope,
            message.canonical_message_id,
            pinned_by_principal_id=message.sender_principal_id,
        )
        second, created_again = self.pins.pin(
            self.ledger,
            self.scope,
            message.canonical_message_id,
            pinned_by_principal_id=None,
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first, second)
        with self.assertRaises(ValueError):
            self.pins.pin(
                self.ledger,
                self.other,
                message.canonical_message_id,
                pinned_by_principal_id=None,
            )

    def test_pin_survives_clear_and_remains_renderable(self) -> None:
        message = self._message("keep this after clear", "11")
        self.pins.pin(
            self.ledger,
            self.scope,
            message.canonical_message_id,
            pinned_by_principal_id=None,
        )
        self.ledger.hide_history(self.scope)
        self.assertIsNone(
            self.ledger.get_in_scope(self.scope, message.canonical_message_id)
        )
        rendered = self.pins.render(self.ledger, self.scope)
        self.assertIn("keep this after clear", rendered)
        self.assertIn(f"msg#{message.canonical_message_id}", rendered)

    def test_search_and_unpin(self) -> None:
        message = self._message("project uses sqlite", "12")
        self.pins.pin(
            self.ledger,
            self.scope,
            message.canonical_message_id,
            pinned_by_principal_id=None,
        )
        self.assertEqual(len(self.pins.search(self.ledger, self.scope, "SQLITE")), 1)
        self.assertTrue(
            self.pins.unpin(self.scope, message.canonical_message_id)
        )
        self.assertEqual(self.pins.list(self.scope), [])


if __name__ == "__main__":
    unittest.main()
