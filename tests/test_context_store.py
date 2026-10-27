from __future__ import annotations

from tests.postgres_fixture import postgres_for_test

import unittest

import nonebot

nonebot.init()

from src.plugins.ai_chat.context_store import ContextStore
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode


class ContextStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = MessageLedger(postgres_for_test(self))
        self.context = ContextStore(
            postgres_for_test(self),
            input_budget_tokens=1000,
            high_watermark_tokens=180,
            low_watermark_tokens=90,
            compartment_target_tokens=70,
            raw_tail_min_messages=3,
            max_compartments=10,
        )
        self.addCleanup(self.context.close)
        self.addCleanup(self.ledger.close)
        self.group_a = ConversationScope("onebot-v11", "group", "100")
        self.group_b = ConversationScope("onebot-v11", "group", "200")

    def seed(self, count: int = 18) -> None:
        for index in range(1, count + 1):
            self.ledger.record_message(
                self.group_a,
                native_message_id=str(index),
                sender_native_user_id=str(1000 + index % 2),
                sender_display=f"User {index % 2}",
                body=MessageBody(
                    (TextNode(0, f"project discussion {index} " + "x" * 200),)
                ),
                occurred_at=100 + index,
            )

    def test_materializes_exact_coverage_and_keeps_raw_tail(self) -> None:
        self.seed()

        projection = self.context.build_projection(self.ledger, self.group_a)
        valid, detail = self.context.verify_scope(self.ledger, self.group_a)

        self.assertTrue(valid, detail)
        self.assertGreater(projection.materialized_count, 0)
        self.assertTrue(projection.compartment_handles)
        self.assertTrue(projection.raw_message_ids)
        self.assertEqual(
            len(projection.compartment_handles),
            len(set(projection.compartment_handles)),
        )
        self.assertIn("conversation compartments", projection.text)
        self.assertIn("protected live tail", projection.text)

    def test_expand_rechecks_scope_and_source_hash(self) -> None:
        self.seed()
        projection = self.context.build_projection(self.ledger, self.group_a)
        handle = projection.compartment_handles[0]

        expanded = self.context.expand(self.ledger, self.group_a, handle)
        self.assertIn("exact evidence", expanded)
        self.assertIsNone(self.context.expand(self.ledger, self.group_b, handle))

        first_source = int(expanded.split("msg#", 1)[1].split("..", 1)[0])
        with self.ledger._transaction() as cursor:
            cursor.execute(
                """
                UPDATE messages SET body_json = ?
                WHERE canonical_message_id = ?
                """,
                (
                    '{"v":1,"nodes":[{"type":"text","index":0,'
                    '"text":"tampered source"}]}',
                    first_source,
                ),
            )
        self.assertIsNone(self.context.expand(self.ledger, self.group_a, handle))

    def test_clear_hides_compartments_and_old_raw_messages(self) -> None:
        self.seed()
        before = self.context.build_projection(self.ledger, self.group_a)
        handle = before.compartment_handles[0]

        self.ledger.hide_history(self.group_a)
        floor = self.ledger.visible_message_floor(self.group_a)
        hidden = self.context.hide_history(self.group_a, floor)
        after = self.context.build_projection(self.ledger, self.group_a)

        self.assertGreater(hidden, 0)
        self.assertEqual(after.text, "")
        self.assertIsNone(self.context.expand(self.ledger, self.group_a, handle))

    def test_current_message_can_be_excluded_without_losing_coverage(self) -> None:
        self.seed()
        projection = self.context.build_projection(
            self.ledger,
            self.group_a,
            exclude_native_message_id="18",
            protected_message_ids=(18,),
        )

        self.assertNotIn("discussion 18", projection.text)
        valid, detail = self.context.verify_scope(self.ledger, self.group_a)
        self.assertTrue(valid, detail)

    def test_raw_tail_does_not_skip_an_oversized_middle_message(self) -> None:
        managed = ContextStore(
            postgres_for_test(self),
            input_budget_tokens=1000,
            historian_managed=True,
        )
        self.addCleanup(managed.close)
        for native_id, text in (
            ("old", "old joke that must not jump across the gap"),
            ("large", "x" * 5000),
            ("new", "current live tail"),
        ):
            self.ledger.record_message(
                self.group_a,
                native_message_id=native_id,
                sender_native_user_id="1000",
                sender_display="User",
                body=MessageBody((TextNode(0, text),)),
                occurred_at=200,
            )

        projection = managed.build_projection(self.ledger, self.group_a)

        self.assertIn("current live tail", projection.text)
        self.assertNotIn("old joke", projection.text)
        self.assertEqual(len(projection.raw_message_ids), 1)

    def test_per_turn_budget_limits_the_chronological_suffix(self) -> None:
        managed = ContextStore(
            postgres_for_test(self),
            input_budget_tokens=4000,
            historian_managed=True,
        )
        self.addCleanup(managed.close)
        for index in range(12):
            self.ledger.record_message(
                self.group_a,
                native_message_id=f"budget-{index}",
                sender_native_user_id="1000",
                sender_display="User",
                body=MessageBody(
                    (TextNode(0, f"timeline {index} " + "x" * 500),)
                ),
                occurred_at=300 + index,
            )

        projection = managed.build_projection(
            self.ledger,
            self.group_a,
            token_budget=1000,
        )

        self.assertLess(projection.raw_message_ids[0], 12)
        self.assertIn("timeline 11", projection.text)
        self.assertNotIn("timeline 0 ", projection.text)
        self.assertTrue(projection.degraded)

    def test_historian_publication_uses_cursor_cas(self) -> None:
        self.seed()
        candidate = self.context.capture_candidate(self.ledger, self.group_a)
        self.assertIsNotNone(candidate)
        episode = self.context.publish_generated(
            candidate,  # type: ignore[arg-type]
            ("详细摘要", "中等摘要", "短摘要"),
        )
        self.assertEqual(episode.summary_p1, "详细摘要")
        self.assertEqual(
            episode.source_hash,
            candidate.source_hash,  # type: ignore[union-attr]
        )
        with self.assertRaises(RuntimeError):
            self.context.publish_generated(
                candidate,  # type: ignore[arg-type]
                ("再次摘要", "再次中等摘要", "再次短摘要"),
            )

    def test_historian_managed_projection_never_advances_cursor(self) -> None:
        managed = ContextStore(
            postgres_for_test(self),
            input_budget_tokens=1000,
            high_watermark_tokens=180,
            low_watermark_tokens=90,
            compartment_target_tokens=70,
            raw_tail_min_messages=3,
            historian_managed=True,
        )
        self.addCleanup(managed.close)
        self.seed()
        projection = managed.build_projection(self.ledger, self.group_a)
        self.assertEqual(projection.materialized_count, 0)
        self.assertFalse(projection.compartment_handles)
        self.assertIsNotNone(
            managed.capture_candidate(self.ledger, self.group_a, settled=True)
        )


if __name__ == "__main__":
    unittest.main()
