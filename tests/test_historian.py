from __future__ import annotations

from tests.postgres_fixture import postgres_for_test

import asyncio
import tempfile
import unittest
from pathlib import Path

import nonebot

nonebot.init()

from src.plugins.ai_chat.context_store import ContextStore
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.historian import (
    DreamOperation,
    DreamService,
    HistorianResult,
    HistorianService,
    MaintenanceState,
    MemoryProposal,
)
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.long_term_memory import LongTermMemoryStore
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.storage.jobs import DurableJobStore


class HistorianTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.ledger = MessageLedger(postgres_for_test(self))
        self.context = ContextStore(
            postgres_for_test(self),
            input_budget_tokens=1000,
            high_watermark_tokens=180,
            low_watermark_tokens=90,
            compartment_target_tokens=70,
            raw_tail_min_messages=3,
        )
        self.memories = LongTermMemoryStore(root / "memory.json")
        self.addCleanup(self.context.close)
        self.addCleanup(self.ledger.close)
        self.scope = ConversationScope("onebot-v11", "group", "100")
        for index in range(1, 19):
            self.ledger.record_message(
                self.scope,
                native_message_id=str(index),
                sender_native_user_id="7",
                sender_display="Alice",
                body=MessageBody((TextNode(0, f"project fact {index} " + "x" * 180),)),
                occurred_at=100 + index,
            )

    def test_historian_publishes_and_adds_evidence_bound_memory(self) -> None:
        async def generate(candidate):
            return HistorianResult(
                "详细摘要",
                "中等摘要",
                "短摘要",
                (MemoryProposal("项目统一使用 Python", candidate.messages[0].canonical_message_id),),
            )

        run = asyncio.run(
            HistorianService(
                self.ledger,
                self.context,
                self.memories,
                generate,
            ).run_once()
        )
        self.assertEqual(run.published, 1)
        self.assertEqual(run.memories_added, 1)
        self.assertEqual(self.memories.all_entries()[0].scope_key, "group:100")

    def test_invalid_evidence_does_not_advance_cursor(self) -> None:
        async def generate(_candidate):
            return HistorianResult(
                "详细摘要",
                "中等摘要",
                "短摘要",
                (MemoryProposal("invalid", 999999),),
            )

        run = asyncio.run(
            HistorianService(
                self.ledger,
                self.context,
                self.memories,
                generate,
            ).run_once()
        )
        self.assertEqual(run.published, 0)
        self.assertTrue(run.failures)
        self.assertIsNotNone(self.context.capture_candidate(self.ledger, self.scope))

    def test_quiet_scope_schedules_exact_restart_safe_capture(self) -> None:
        jobs = DurableJobStore(postgres_for_test(self), default_max_attempts=4)
        self.addCleanup(jobs.close)

        async def generate(candidate):
            evidence = tuple(item.canonical_message_id for item in candidate.messages)
            return HistorianResult(
                "详细摘要",
                "关键结论",
                "短摘要",
                summary_p4="检索锚点",
                topic="项目架构",
                importance=0.82,
                confidence=0.91,
                participants=("Alice",),
                evidence_ids=evidence,
            )

        service = HistorianService(
            self.ledger,
            self.context,
            self.memories,
            generate,
        )
        self.assertEqual(
            service.schedule_due(jobs, idle_seconds=600, now=500),
            0,
        )
        self.assertEqual(
            service.schedule_due(jobs, idle_seconds=600, now=1000),
            1,
        )
        self.assertEqual(
            service.schedule_due(jobs, idle_seconds=600, now=1000),
            0,
        )
        job = jobs.claim_due("test-worker", now=1000)[0]
        result = asyncio.run(service.handle_job(job))
        self.assertEqual(result["generation_mode"], "historian")
        episode = self.context.active_compartments()[0]
        self.assertEqual(episode.topic, "项目架构")
        self.assertEqual(episode.summary_p4, "检索锚点")
        self.assertEqual(episode.evidence_ids, tuple(job.payload["source_message_ids"]))

    def test_final_historian_attempt_uses_deterministic_fallback(self) -> None:
        jobs = DurableJobStore(postgres_for_test(self), default_max_attempts=2)
        self.addCleanup(jobs.close)

        async def fail(_candidate):
            raise RuntimeError("model unavailable")

        service = HistorianService(
            self.ledger,
            self.context,
            self.memories,
            fail,
        )
        self.assertEqual(
            service.schedule_due(
                jobs,
                idle_seconds=600,
                max_attempts=2,
                now=1000,
            ),
            1,
        )
        first = jobs.claim_due("test-worker", now=1000)[0]
        with self.assertRaises(RuntimeError):
            asyncio.run(service.handle_job(first))
        jobs.mark_failed(
            first.job_id,
            "test-worker",
            "model unavailable",
            retryable=True,
            retry_delay_seconds=0,
            now=1000,
        )
        final = jobs.claim_due("test-worker", now=1000)[0]
        result = asyncio.run(service.handle_job(final))
        self.assertEqual(result["generation_mode"], "fallback")
        self.assertEqual(
            self.context.active_compartments()[0].generation_mode,
            "fallback",
        )

    def test_dream_uses_versioned_updates(self) -> None:
        entry, _ = self.memories.add(
            "group:100", "group", "旧说法", creator_user_id=1
        )

        async def consolidate(_scope, entries, _evidence):
            return [
                DreamOperation(
                    "update",
                    entries[0].id,
                    entries[0].version,
                    "新说法",
                    "newer evidence",
                )
            ]

        result = asyncio.run(
            DreamService(
                self.memories,
                consolidate,
                min_entries=2,
            ).run_once()
        )
        self.assertEqual(result["changed"], 0)
        self.memories.add("group:100", "group", "第二条", creator_user_id=1)
        result = asyncio.run(
            DreamService(
                self.memories,
                consolidate,
                min_entries=2,
            ).run_once()
        )
        self.assertEqual(result["changed"], 1)
        self.assertEqual(
            next(item for item in self.memories.all_entries() if item.id == entry.id).content,
            "新说法",
        )

    def test_maintenance_state_is_persistent(self) -> None:
        state = MaintenanceState(postgres_for_test(self))
        self.addCleanup(state.close)
        self.assertFalse(state.completed("dream", "2026-08-09"))
        state.mark_completed("dream", "2026-08-09")
        self.assertTrue(state.completed("dream", "2026-08-09"))


if __name__ == "__main__":
    unittest.main()
