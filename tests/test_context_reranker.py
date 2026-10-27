from __future__ import annotations

from tests.postgres_fixture import postgres_for_test

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import nonebot

nonebot.init()

from src.plugins.ai_chat.context_pipeline import (
    ContextTokenBudget,
    HybridReranker,
    build_hybrid_recall,
)
from src.plugins.ai_chat.context_pipeline.ranking import RecallCandidate
from src.plugins.ai_chat.context_pipeline.ranking import (
    combine_budgeted_sections,
    fit_token_budget,
)
from src.plugins.ai_chat.context_pipeline.router import rule_recall_route
from src.plugins.ai_chat.context_store import estimate_tokens
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.long_term_memory import LongTermMemoryStore
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.semantic_recall import SemanticHit


class _SemanticRecall:
    def __init__(self, hits):
        self.hits = hits
        self.scopes = []

    async def search(self, scopes, query, limit=10):
        del query, limit
        self.scopes = list(scopes)
        return list(self.hits)


class HybridContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_recall_skips_embedding_and_memories(self) -> None:
        ledger = MessageLedger(postgres_for_test(self))
        self.addCleanup(ledger.close)
        scope = ConversationScope("onebot-v11", "group", "100")
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        memories = LongTermMemoryStore(Path(directory.name) / "memory.json")
        memories.add(
            "group:100:user:9",
            "user",
            "用户喜欢绿色主题",
            creator_user_id=9,
        )
        semantic = _SemanticRecall([])
        decision = rule_recall_route("快速排序怎么写", None, is_group=True)

        result = await build_hybrid_recall(
            ledger=ledger,
            scope=scope,
            plan=None,
            user_text="快速排序怎么写",
            group_memory_scope="group:100",
            user_memory_scope="group:100:user:9",
            memory_store=memories,
            semantic_recall=semantic,
            recall_decision=decision,
            budget=ContextTokenBudget(0, 0, 0, 0, 0),
        )

        self.assertEqual(result.candidates, ())
        self.assertEqual(semantic.scopes, [])

    async def test_recall_keeps_group_and_current_user_scopes_separate(self) -> None:
        ledger = MessageLedger(postgres_for_test(self))
        self.addCleanup(ledger.close)
        scope = ConversationScope("onebot-v11", "group", "100")
        visible = ledger.record_message(
            scope,
            native_message_id="1",
            sender_native_user_id="7",
            sender_display="Alice",
            body=MessageBody((TextNode(0, "h610 数据库延迟很低"),)),
            occurred_at=1000,
        )
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        memories = LongTermMemoryStore(Path(directory.name) / "memory.json")
        own, _ = memories.add(
            "group:100:user:9",
            "user",
            "Kenneth 偏好 h610 做热数据主库",
            creator_user_id=9,
        )
        memories.add(
            "group:100:user:8",
            "user",
            "Bob 的私人密码不能泄漏",
            creator_user_id=8,
        )
        group, _ = memories.add(
            "group:100",
            "group",
            "tank 只负责冷归档",
            creator_user_id=9,
        )
        semantic = _SemanticRecall(
            [
                SemanticHit(
                    scope.key,
                    "message",
                    f"msg#{visible.canonical_message_id}",
                    visible.prompt_text,
                    0.92,
                    {},
                ),
                SemanticHit(
                    "onebot-v11:group:200",
                    "message",
                    "msg#999",
                    "另一个群的秘密",
                    0.99,
                    {},
                ),
                SemanticHit(
                    own.scope_key,
                    "memory",
                    f"memory#{own.id}",
                    own.content,
                    0.91,
                    {},
                ),
                SemanticHit(
                    group.scope_key,
                    "memory",
                    f"memory#{group.id}",
                    group.content,
                    0.88,
                    {},
                ),
            ]
        )
        result = await build_hybrid_recall(
            ledger=ledger,
            scope=scope,
            plan=None,
            user_text="h610 和 tank 数据库怎么分工",
            group_memory_scope="group:100",
            user_memory_scope="group:100:user:9",
            memory_store=memories,
            semantic_recall=semantic,
            budget=ContextTokenBudget(0, 0, 100, 100, 120),
            now=1100,
        )

        combined = result.group_context + result.memory_context
        self.assertIn("h610 数据库", combined)
        self.assertIn("tank", combined)
        self.assertNotIn("Bob 的私人密码", combined)
        self.assertNotIn("另一个群的秘密", combined)
        self.assertEqual(
            set(semantic.scopes),
            {scope.key, "group:100", "group:100:user:9"},
        )

    def test_reranker_filters_irrelevant_personal_memory(self) -> None:
        ranked = HybridReranker().rerank(
            "PostgreSQL 主库延迟",
            [
                RecallCandidate(
                    "memory#1",
                    "user_memory",
                    "group:100:user:9",
                    "喜欢绿色主题",
                    recency_score=1.0,
                ),
                RecallCandidate(
                    "memory#2",
                    "group_memory",
                    "group:100",
                    "PostgreSQL 主库位于 h610，延迟较低",
                    recency_score=0.8,
                ),
            ],
        )

        self.assertEqual([item.candidate.handle for item in ranked], ["memory#2"])

    def test_reranker_records_selected_and_dropped_reasons(self) -> None:
        result = HybridReranker().rerank_with_audit(
            "PostgreSQL 主库延迟",
            [
                RecallCandidate(
                    "msg#1",
                    "relation_graph",
                    "onebot-v11:group:100",
                    "PostgreSQL 主库在 h610",
                    relation_score=1.0,
                    semantic_score=0.9,
                ),
                RecallCandidate(
                    "memory#2",
                    "user_memory",
                    "group:100:user:9",
                    "喜欢绿色主题",
                    recency_score=1.0,
                ),
            ],
        )

        by_handle = {item.candidate.handle: item for item in result.decisions}
        self.assertTrue(by_handle["msg#1"].selected)
        self.assertIn("selected", by_handle["msg#1"].decision_codes)
        self.assertFalse(by_handle["memory#2"].selected)
        self.assertIn(
            "below_source_threshold",
            by_handle["memory#2"].decision_codes,
        )

    def test_relative_threshold_keeps_complementary_evidence(self) -> None:
        ranked = HybridReranker().rerank(
            "h610 数据库切换",
            [
                RecallCandidate(
                    "msg#1",
                    "relation_graph",
                    "onebot-v11:group:100",
                    "Alice: h610 是当前主库",
                    relation_score=1.0,
                    semantic_score=0.8,
                    topic_score=0.8,
                ),
                RecallCandidate(
                    "episode#12345678",
                    "historian_episode",
                    "onebot-v11:group:100",
                    "切换前要确认复制延迟和备份恢复点",
                    semantic_score=0.82,
                    topic_score=0.65,
                    importance_score=0.9,
                ),
                RecallCandidate(
                    "memory#3",
                    "user_memory",
                    "group:100:user:9",
                    "喜欢绿色主题",
                    recency_score=1.0,
                ),
            ],
            limit=5,
        )

        handles = [item.candidate.handle for item in ranked]
        self.assertIn("msg#1", handles)
        self.assertIn("episode#12345678", handles)
        self.assertNotIn("memory#3", handles)

    def test_duplicate_message_merges_graph_and_semantic_signals(self) -> None:
        ranked = HybridReranker().rerank(
            "PostgreSQL 主库",
            [
                RecallCandidate(
                    "msg#1",
                    "relation_graph",
                    "onebot-v11:group:100",
                    "Alice: PostgreSQL 主库在 h610",
                    relation_score=1.0,
                ),
                RecallCandidate(
                    "msg#1",
                    "raw_history",
                    "onebot-v11:group:100",
                    "Alice: PostgreSQL 主库在 h610",
                    semantic_score=0.95,
                ),
            ],
        )

        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].candidate.source, "relation_graph")
        self.assertEqual(ranked[0].candidate.relation_score, 1.0)
        self.assertEqual(ranked[0].candidate.semantic_score, 0.95)

    def test_token_partitions_enforce_source_and_total_limits(self) -> None:
        focus = fit_token_budget("焦点" * 200, 30)
        timeline = fit_token_budget("群时间线" * 200, 40)
        combined = combine_budgeted_sections(
            [("focus", focus, 30), ("timeline", timeline, 40)],
            total_budget=55,
        )

        self.assertLessEqual(estimate_tokens(focus), 30)
        self.assertLessEqual(estimate_tokens(timeline), 40)
        self.assertLessEqual(estimate_tokens(combined), 55)


if __name__ == "__main__":
    unittest.main()
