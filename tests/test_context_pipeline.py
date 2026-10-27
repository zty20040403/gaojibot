from __future__ import annotations

from tests.postgres_fixture import postgres_for_test

import unittest

import nonebot

nonebot.init()

from src.plugins.ai_chat.context_pipeline import ReferenceResolver, TopicGraphStore
from src.plugins.ai_chat.context_pipeline.graph import MessageReferenceGraph
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MentionNode, MessageBody, TextNode


class ReferenceResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = MessageLedger(postgres_for_test(self))
        self.addCleanup(self.ledger.close)
        self.resolver = ReferenceResolver()
        self.group_a = ConversationScope("onebot-v11", "group", "100")
        self.group_b = ConversationScope("onebot-v11", "group", "200")

    def record(
        self,
        scope: ConversationScope,
        native_id: int,
        user_id: int,
        sender: str,
        text: str,
        *,
        reply_to: int | None = None,
        direction: str = "inbound",
    ):
        return self.ledger.record_message(
            scope,
            native_message_id=str(native_id),
            sender_native_user_id=str(user_id),
            sender_display=sender,
            body=MessageBody((TextNode(0, text),)),
            occurred_at=1_000 + native_id,
            direction=direction,
            reply_to_native_message_id=(
                str(reply_to) if reply_to is not None else None
            ),
        )

    def test_opinion_follow_up_prefers_latest_human_statement(self) -> None:
        question = self.record(
            self.group_a,
            1,
            7,
            "Alice",
            "PostgreSQL 和 SQLite 哪个适合机器人？",
        )
        comment = self.record(
            self.group_a,
            2,
            8,
            "Bob",
            "我觉得 PostgreSQL 更稳",
        )
        current = self.record(self.group_a, 3, 9, "Kenneth", "你觉得呢")

        plan = self.resolver.resolve(
            self.ledger,
            self.group_a,
            current_message_id=current.canonical_message_id,
            current_text="你觉得呢",
            current_native_user_id=9,
            now=current.occurred_at,
        )

        self.assertEqual(plan.focus_message_id, comment.canonical_message_id)
        self.assertIn(question.canonical_message_id, plan.related_message_ids)
        self.assertIn("adjacent_human_anchor", plan.reason_codes)
        self.assertIn("PostgreSQL", plan.rendered_context)

    def test_bot_mention_does_not_override_unmentioned_latest_statement(self) -> None:
        old_question = self.record(
            self.group_a,
            1,
            7,
            "Alice",
            "h610 的数据库是不是坏了？",
        )
        bot_reply = self.record(
            self.group_a,
            2,
            99,
            "Bot",
            "数据库目前正常。",
            reply_to=1,
            direction="outbound",
        )
        latest = self.record(
            self.group_a,
            3,
            9,
            "Kenneth",
            "我觉得 h310 比 h610 屌",
        )
        bot_principal = self.ledger.principal_id_for_native("onebot-v11", "99")
        current = self.ledger.record_message(
            self.group_a,
            native_message_id="4",
            sender_native_user_id="9",
            sender_display="Kenneth",
            body=MessageBody(
                (
                    MentionNode(0, "99", "Bot", bot_principal),
                    TextNode(1, "你觉得呢"),
                )
            ),
            occurred_at=1_004,
        )

        plan = self.resolver.resolve(
            self.ledger,
            self.group_a,
            current_message_id=current.canonical_message_id,
            current_text="你觉得呢",
            current_native_user_id=9,
            now=current.occurred_at,
        )

        self.assertEqual(plan.focus_message_id, latest.canonical_message_id)
        self.assertNotEqual(plan.focus_message_id, old_question.canonical_message_id)
        self.assertNotIn(bot_reply.canonical_message_id, plan.related_message_ids)
        self.assertEqual(plan.confidence, 0.99)
        self.assertIn("我觉得 h310 比 h610 屌", plan.rendered_context)

        graph = MessageReferenceGraph([old_question, bot_reply, latest, current])
        self.assertFalse(
            any(
                edge.relation == "mention"
                and bot_reply.canonical_message_id
                in {edge.source_message_id, edge.target_message_id}
                for edge in graph.edges
            )
        )

    def test_empty_mention_follow_up_prefers_recent_group_question(self) -> None:
        question = self.record(
            self.group_a,
            1,
            7,
            "Alice",
            "这个服务为什么突然变慢了？",
        )
        current = self.record(self.group_a, 2, 9, "Kenneth", "")

        plan = self.resolver.resolve(
            self.ledger,
            self.group_a,
            current_message_id=current.canonical_message_id,
            current_text="你觉得呢",
            current_native_user_id=9,
            now=current.occurred_at,
        )

        self.assertEqual(plan.focus_message_id, question.canonical_message_id)
        self.assertIn("adjacent_human_anchor", plan.reason_codes)
        self.assertIn("突然变慢", plan.rendered_context)

    def test_empty_mention_prefers_latest_human_message_over_older_question(self) -> None:
        older_question = self.record(
            self.group_a,
            1,
            7,
            "Alice",
            "今晚几点开会？",
        )
        shared_card = self.record(
            self.group_a,
            2,
            8,
            "Bob",
            "[卡片:小红书帖子 | https://xhslink.com/example]",
        )
        current = self.record(self.group_a, 3, 9, "Kenneth", "")

        plan = self.resolver.resolve(
            self.ledger,
            self.group_a,
            current_message_id=current.canonical_message_id,
            current_text="你觉得呢",
            current_native_user_id=9,
            now=current.occurred_at,
            prefer_latest=True,
        )

        self.assertEqual(plan.focus_message_id, shared_card.canonical_message_id)
        self.assertNotEqual(plan.focus_message_id, older_question.canonical_message_id)
        self.assertIn("empty_mention_latest", plan.reason_codes)
        self.assertIn("xhslink.com", plan.rendered_context)

    def test_explicit_reply_has_absolute_priority(self) -> None:
        target = self.record(self.group_a, 1, 7, "Alice", "晚上吃面吗")
        self.record(self.group_a, 2, 8, "Bob", "服务器怎么配？")
        current = self.record(
            self.group_a,
            3,
            9,
            "Kenneth",
            "你觉得呢",
            reply_to=1,
        )

        plan = self.resolver.resolve(
            self.ledger,
            self.group_a,
            current_message_id=current.canonical_message_id,
            current_text="你觉得呢",
            current_native_user_id=9,
            now=current.occurred_at,
        )

        self.assertEqual(plan.focus_message_id, target.canonical_message_id)
        self.assertEqual(plan.confidence, 1.0)
        self.assertIn("explicit_reply", plan.reason_codes)

    def test_other_group_is_never_a_candidate(self) -> None:
        secret = self.record(
            self.group_b,
            1,
            7,
            "Alice",
            "另一个群的秘密问题是什么？",
        )
        local = self.record(self.group_a, 2, 8, "Bob", "本群今晚吃什么？")
        current = self.record(self.group_a, 3, 9, "Kenneth", "你觉得呢")

        plan = self.resolver.resolve(
            self.ledger,
            self.group_a,
            current_message_id=current.canonical_message_id,
            current_text="你觉得呢",
            current_native_user_id=9,
            now=current.occurred_at,
        )

        candidate_ids = {item.message_id for item in plan.candidates}
        self.assertEqual(plan.focus_message_id, local.canonical_message_id)
        self.assertNotIn(secret.canonical_message_id, candidate_ids)
        self.assertNotIn("秘密问题", plan.rendered_context)

    def test_standalone_question_does_not_force_a_focus(self) -> None:
        self.record(self.group_a, 1, 7, "Alice", "你们吃什么？")
        current = self.record(
            self.group_a,
            2,
            9,
            "Kenneth",
            "NixOS 怎么更新？",
        )

        plan = self.resolver.resolve(
            self.ledger,
            self.group_a,
            current_message_id=current.canonical_message_id,
            current_text="NixOS 怎么更新？",
            current_native_user_id=9,
            now=current.occurred_at,
        )

        self.assertIsNone(plan.focus_message_id)
        self.assertEqual(plan.reason_codes, ("standalone_message",))
        self.assertEqual(plan.rendered_context, "")

    def test_question_with_its_own_subject_is_not_a_follow_up(self) -> None:
        self.record(self.group_a, 1, 7, "Alice", "今晚吃什么？")
        current = self.record(
            self.group_a,
            2,
            9,
            "Kenneth",
            "你觉得 PostgreSQL 怎么样？",
        )

        plan = self.resolver.resolve(
            self.ledger,
            self.group_a,
            current_message_id=current.canonical_message_id,
            current_text="你觉得 PostgreSQL 怎么样？",
            current_native_user_id=9,
            now=current.occurred_at,
        )

        self.assertIsNone(plan.focus_message_id)
        self.assertEqual(plan.reason_codes, ("standalone_message",))

    def test_topic_graph_records_reply_mention_sender_time_and_semantic_edges(self) -> None:
        alice = self.record(
            self.group_a,
            1,
            7,
            "Alice",
            "控制台要不要用 SSE 实时更新？",
        )
        principal = self.ledger.principal_id_for_native("onebot-v11", "7")
        mentioned = self.ledger.record_message(
            self.group_a,
            native_message_id="2",
            sender_native_user_id="8",
            sender_display="Bob",
            body=MessageBody(
                (
                    TextNode(0, "这个 SSE 方案可以"),
                    MentionNode(1, "7", "Alice", principal),
                )
            ),
            occurred_at=1002,
            reply_to_native_message_id="1",
        )
        follow = self.record(
            self.group_a,
            3,
            8,
            "Bob",
            "这样下拉框不会被刷新关闭",
        )
        graph = MessageReferenceGraph(
            [alice, mentioned, follow],
            semantic_scores={(follow.canonical_message_id, alice.canonical_message_id): 0.91},
        )
        relations = {edge.relation for edge in graph.edges}
        self.assertTrue(
            {
                "reply",
                "mention",
                "same_sender_continuation",
                "temporal_proximity",
                "question_answer",
                "semantic_similarity",
            }.issubset(relations)
        )
        store = TopicGraphStore(postgres_for_test(self))
        self.addCleanup(store.close)
        self.assertGreater(store.upsert(self.group_a, graph.edges), 0)


if __name__ == "__main__":
    unittest.main()
