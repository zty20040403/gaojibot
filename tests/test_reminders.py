from __future__ import annotations

from tests.postgres_fixture import postgres_for_test

import unittest

import nonebot

nonebot.init()

from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.reminders import ReminderStore


class ReminderStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ReminderStore(postgres_for_test(self), max_per_scope=2)
        self.scope = ConversationScope(
            "onebot-v11",
            "group",
            "99",
            actor_native_user_id="1",
            bot_native_user_id="1000",
        )

    def tearDown(self) -> None:
        self.store.close()

    def test_claim_send_and_scope_list(self) -> None:
        reminder = self.store.create(
            self.scope,
            creator_native_user_id="1",
            creator_principal_id=2,
            message="submit homework",
            scheduled_for=200,
            now=100,
        )
        self.assertEqual(self.store.claim_due(now=199), [])
        claimed = self.store.claim_due(now=200)
        self.assertEqual([item.handle for item in claimed], [reminder.handle])
        self.assertEqual(claimed[0].attempts, 1)
        self.assertTrue(self.store.mark_sent(reminder.reminder_id, sent_at=201))
        self.assertEqual(self.store.list_pending(self.scope), [])

    def test_failed_delivery_is_retried_after_delay(self) -> None:
        reminder = self.store.create(
            self.scope,
            creator_native_user_id="1",
            creator_principal_id=None,
            message="retry me",
            scheduled_for=200,
            now=100,
        )
        self.store.claim_due(now=200)
        self.assertTrue(
            self.store.mark_failed(reminder.reminder_id, "offline", now=200)
        )
        self.assertEqual(self.store.claim_due(now=259), [])
        self.assertEqual(len(self.store.claim_due(now=260)), 1)

    def test_cancel_is_scope_bound(self) -> None:
        reminder = self.store.create(
            self.scope,
            creator_native_user_id="1",
            creator_principal_id=None,
            message="cancel me",
            scheduled_for=200,
            now=100,
        )
        other = ConversationScope("onebot-v11", "group", "100")
        self.assertFalse(self.store.cancel(other, reminder.reminder_id))
        self.assertTrue(self.store.cancel(self.scope, reminder.reminder_id))


if __name__ == "__main__":
    unittest.main()
