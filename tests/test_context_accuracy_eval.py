from __future__ import annotations

from tests.postgres_fixture import postgres_for_test

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

import nonebot

nonebot.init()

from src.plugins.ai_chat.context_pipeline import (
    ContextTokenBudget,
    ReferenceResolver,
    build_hybrid_recall,
)
from src.plugins.ai_chat.context_store import ContextStore
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, MentionNode, TextNode
from src.plugins.ai_chat.semantic_recall import SemanticHit


FIXTURE = Path(__file__).parent / "fixtures" / "context_accuracy_cases.json"


class _NoMemories:
    @staticmethod
    def list_entries(_scopes):
        return []


class _StubSemanticRecall:
    def __init__(self, hits: list[SemanticHit]) -> None:
        self.hits = hits
        self.embedder = SimpleNamespace(model="BAAI/bge-m3")

    async def search(self, _scope_keys, _query, *, limit):
        return self.hits[:limit]


class ContextAccuracyEvaluationTests(unittest.TestCase):
    def test_projection_and_recall_wiring_contracts(self) -> None:
        # This tests host assembly, not answer accuracy or real embedding retrieval.
        seeds = json.loads(FIXTURE.read_text(encoding="utf-8"))
        expanded = [
            (seed, prompt)
            for seed in seeds
            for prompt in seed.get("prompts", [seed["current"]["text"]])
        ]
        self.assertGreaterEqual(len(expanded), 100)

        timeline_total = 0
        timeline_covered = 0
        recall_total = 0
        recall_hits = 0
        leakage_count = 0

        for seed, prompt in expanded:
            with self.subTest(case=seed["name"], prompt=prompt):
                result = self._evaluate(seed, prompt)
                timeline_total += 1
                timeline_covered += int(result["timeline_covered"])
                recall_total += int(result["recall_evaluated"])
                recall_hits += int(result["recall_hit"])
                leakage_count += int(result["leaked"])

        timeline_coverage = timeline_covered / max(timeline_total, 1)
        recall_at_five = recall_hits / max(recall_total, 1)
        self.assertGreaterEqual(timeline_coverage, 0.99)
        self.assertGreaterEqual(recall_at_five, 0.90)
        self.assertEqual(leakage_count, 0)

    def _evaluate(self, seed: dict[str, object], prompt: str) -> dict[str, bool]:
        ledger = MessageLedger(postgres_for_test(self, reset=True))
        self.addCleanup(ledger.close)
        scope = ConversationScope("onebot-v11", "group", "100")
        other_scope = ConversationScope("onebot-v11", "group", "200")
        other_ids: list[int] = []
        for item in seed.get("other_group_messages", []):
            other_ids.append(
                self._record(ledger, other_scope, item).canonical_message_id
            )
        native_to_canonical = {
            item["id"]: self._record(ledger, scope, item).canonical_message_id
            for item in seed["messages"]
        }
        current_item = dict(seed["current"])
        current_item["text"] = prompt
        current = self._record(ledger, scope, current_item)
        plan = ReferenceResolver().resolve(
            ledger,
            scope,
            current_message_id=current.canonical_message_id,
            current_text=prompt,
            current_native_user_id=current_item["user"],
            now=current.occurred_at,
        )
        context = ContextStore(
            postgres_for_test(self),
            input_budget_tokens=6000,
            historian_managed=True,
        )
        self.addCleanup(context.close)
        rendered = context.build_projection(
            ledger,
            scope,
            exclude_native_message_id=current.native_message_id,
            materialize=False,
        ).text
        leaked = any(
            marker in rendered
            for marker in seed.get("leak_markers", ["另一个群的机密"])
        )

        recall_evaluated = seed.get("evaluation") == "recall"
        recall_hit = False
        if recall_evaluated:
            expected_recall = native_to_canonical[int(seed["expected_recall"])]
            semantic_order = [
                native_to_canonical[int(item)] for item in seed["semantic_order"]
            ]
            hits = [
                SemanticHit(
                    scope.key,
                    "message",
                    f"msg#{message_id}",
                    "fixture",
                    max(0.97 - index * 0.03, 0.55),
                    {},
                )
                for index, message_id in enumerate(semantic_order)
            ]
            hits.insert(
                0,
                SemanticHit(
                    other_scope.key,
                    "message",
                    f"msg#{other_ids[0] if other_ids else 999999}",
                    "cross-scope decoy",
                    1.0,
                    {},
                ),
            )
            recalled = asyncio.run(
                build_hybrid_recall(
                    ledger=ledger,
                    scope=scope,
                    plan=plan,
                    user_text=prompt,
                    group_memory_scope="group:100",
                    user_memory_scope="group:100:user:9",
                    memory_store=_NoMemories(),
                    semantic_recall=_StubSemanticRecall(hits),
                    budget=ContextTokenBudget(500, 500, 200, 200, 800),
                    now=current.occurred_at,
                )
            )
            top_five = [item.candidate.handle for item in recalled.candidates[:5]]
            recall_hit = f"msg#{expected_recall}" in top_five
            leaked = leaked or any(
                item.candidate.scope_key == other_scope.key
                for item in recalled.candidates
            )
        timeline_covered = all(
            str(required) in rendered for required in seed.get("required", [])
        )
        return {
            "timeline_covered": timeline_covered,
            "recall_evaluated": bool(recall_evaluated),
            "recall_hit": recall_hit,
            "leaked": leaked,
        }

    @staticmethod
    def _record(
        ledger: MessageLedger,
        scope: ConversationScope,
        item: dict[str, object],
    ):
        nodes = [TextNode(0, str(item["text"]))]
        for index, mention in enumerate(item.get("mentions", []), start=1):
            nodes.append(
                MentionNode(
                    index,
                    str(mention["user"]),
                    str(mention["display"]),
                )
            )
        return ledger.record_message(
            scope,
            native_message_id=str(item["id"]),
            sender_native_user_id=str(item["user"]),
            sender_display=str(item["sender"]),
            body=MessageBody(tuple(nodes)),
            occurred_at=int(item.get("at") or 1_000 + int(item["id"])),
            direction=str(item.get("direction") or "inbound"),
            reply_to_native_message_id=(
                str(item["reply_to"]) if item.get("reply_to") is not None else None
            ),
        )


if __name__ == "__main__":
    unittest.main()
