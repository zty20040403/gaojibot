from __future__ import annotations

import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

import nonebot
import psycopg
from psycopg import sql

os.environ.setdefault("AI_ALLOW_LEGACY_SQLITE", "true")
nonebot.init()

from src.bot_storage.database import PostgresDatabase
from src.bot_storage.legacy_migration import (
    capture_legacy_snapshot,
    migrate_legacy_snapshot,
    verify_legacy_snapshot,
)
from src.bot_storage.schema import HEAD_REVISION
from src.bot_storage.state import open_json_state
from src.plugins.ai_chat.bridges import MirrorStateStore
from src.plugins.ai_chat.context_store import ContextStore
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.historian import MaintenanceState
from src.plugins.ai_chat.identity import GroupUserProfileStore
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.long_term_memory import LongTermMemoryStore
from src.plugins.ai_chat.memory import ConversationMemory, GroupContextMemory
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.model_preferences import ModelPreferenceStore
from src.plugins.ai_chat.pins import PinStore
from src.plugins.ai_chat.quota import UsageStore
from src.plugins.ai_chat.reminders import ReminderStore
from src.plugins.ai_chat.semantic_recall import (
    PgVectorBackend,
    SemanticDocument,
    SemanticIndexState,
)
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.turn_journal import TurnJournal


TEST_DSN = os.getenv("TEST_POSTGRES_DSN", "").strip()
TEST_SCHEMA = os.getenv("TEST_POSTGRES_SCHEMA", "qq_bot_test").strip()


@unittest.skipUnless(TEST_DSN, "TEST_POSTGRES_DSN is not configured")
class PostgresIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = PostgresDatabase(
            TEST_DSN,
            schema=TEST_SCHEMA,
            min_size=1,
            max_size=8,
        )
        cls.database.require_revision(HEAD_REVISION)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.database.close()

    def setUp(self) -> None:
        self._truncate_schema()

    def test_reaction_concurrency_and_readonly_feedback_export(self) -> None:
        from tools.export_feedback_cases import export_cases
        from src.context_feedback_evaluation import validate_export
        ledger, journal = MessageLedger(self.database), TurnJournal(self.database)
        other_journal = TurnJournal(self.database)
        self.addCleanup(ledger.close)
        self.addCleanup(journal.close)
        self.addCleanup(other_journal.close)
        scope = ConversationScope("onebot-v11", "group", "100")

        def message(key, user, text, target=scope):
            return ledger.record_message(target, native_message_id=key, sender_native_user_id=user,
                sender_display="fixture", body=MessageBody((TextNode(0, text),)), occurred_at=1000)

        past = message("1", "10", "Earlier topic")
        question = message("2", "10", "What do you think?")
        turn = journal.start_turn(scope, trigger_canonical_message_id=question.canonical_message_id,
                                  objective="What do you think?", provider="fixture", model="fixture")
        answer = message("3", "900", "Original answer")
        journal.link_send(turn.turn_id, answer.canonical_message_id)
        journal.record_context_plan(turn.turn_id, {"scope_key": scope.key,
            "current_message_id": question.canonical_message_id,
            "focus_message_id": past.canonical_message_id})
        journal.finish_turn(turn.turn_id, status="succeeded", final_text="Original answer")
        message("4", "10", "Future answer must not leak")
        message("other", "11", "Other group secret", ConversationScope("onebot-v11", "group", "200"))

        def vote(index):
            return (journal if index % 2 else other_journal).set_reaction_vote(scope, turn.turn_id,
                reactor_principal_id=question.sender_principal_id, message_id=answer.canonical_message_id,
                emoji_id="77", vote=-1, is_add=True, source_time=1010)

        with ThreadPoolExecutor(max_workers=4) as executor:
            self.assertEqual(sum(executor.map(vote, range(16))), 1)
        self.assertEqual(journal.recent_context_plans(negative_only=True)[0]["votes"]["asker"], -1)
        journal.set_context_feedback(turn.turn_id, verdict="correct", note="admin overrides vote",
                                     actor="admin", resource_version=1)
        with psycopg.connect(TEST_DSN, autocommit=True) as connection:
            result = export_cases(connection, schema=TEST_SCHEMA, scopes=[scope.key])
            case = validate_export(result)[0]
            self.assertEqual(case["label"], "positive")
            self.assertEqual(case["label_source"], "administrator")
            self.assertEqual([m["id"] for m in case["messages"]], [past.canonical_message_id])
            self.assertEqual(case["question"]["id"], question.canonical_message_id)
            self.assertNotIn("Future answer", json.dumps(result))
            self.assertNotIn("Other group secret", json.dumps(result))
            self.assertEqual(case["unsupported_reason"], "")
            empty = export_cases(connection, schema=TEST_SCHEMA, scopes=["onebot-v11:group:200"])
            self.assertEqual(empty["cases"], [])
            ledger.hide_history(scope)
            hidden = export_cases(connection, schema=TEST_SCHEMA, scopes=[scope.key])
            self.assertEqual(hidden["cases"], [])
            self.assertEqual(hidden["skipped"][0]["reason"], "question_hidden_or_missing")

    def _truncate_schema(self) -> None:
        with psycopg.connect(TEST_DSN) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT tablename FROM pg_tables
                    WHERE schemaname = %s AND tablename != 'alembic_version'
                    ORDER BY tablename
                    """,
                    (TEST_SCHEMA,),
                )
                tables = [str(row[0]) for row in cursor.fetchall()]
                if tables:
                    cursor.execute(
                        sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(
                            sql.SQL(", ").join(
                                sql.SQL("{}.{}").format(
                                    sql.Identifier(TEST_SCHEMA),
                                    sql.Identifier(table),
                                )
                                for table in tables
                            )
                        )
                    )

    def test_legacy_snapshot_is_repeatable_and_resets_sequences(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            legacy_ledger = MessageLedger(state_dir / "bot_state.sqlite3")
            scope = ConversationScope("onebot-v11", "group", "930690526")
            try:
                first = legacy_ledger.record_message(
                    scope,
                    native_message_id="1001",
                    sender_native_user_id="3526452465",
                    sender_display="Kenneth",
                    body=MessageBody((TextNode(0, "旧数据库中的消息"),)),
                    occurred_at=100,
                )
                legacy_ledger.record_message(
                    scope,
                    native_message_id="1002",
                    sender_native_user_id="2291939848",
                    sender_display="群友",
                    body=MessageBody((TextNode(0, "第二条旧消息"),)),
                    occurred_at=101,
                    reply_to_native_message_id="1001",
                )
            finally:
                legacy_ledger.close()

            memory = ConversationMemory(
                5, state_dir / "conversation_history.json"
            )
            memory.append_turn(scope.key, "旧问题", "旧回答")
            preferences = ModelPreferenceStore(
                state_dir / "model_preferences.json"
            )
            preferences.set(scope.key, "deepseek-chat")
            (state_dir / "group_context.json").write_text(
                json.dumps({"930690526": []}),
                encoding="utf-8",
            )
            learned_stickers = [
                {"type": "face", "data": {"id": "14"}},
            ]
            (state_dir / "learned_stickers.json").write_text(
                json.dumps(learned_stickers),
                encoding="utf-8",
            )

            snapshot = capture_legacy_snapshot(state_dir)
            report = migrate_legacy_snapshot(
                snapshot,
                TEST_DSN,
                schema=TEST_SCHEMA,
            )
            repeated = migrate_legacy_snapshot(
                snapshot,
                TEST_DSN,
                schema=TEST_SCHEMA,
                resume=True,
            )
            verification = verify_legacy_snapshot(
                snapshot,
                TEST_DSN,
                schema=TEST_SCHEMA,
            )

            self.assertTrue(report["verified"])
            self.assertTrue(repeated["verified"])
            self.assertTrue(verification["verified"])
            self.assertGreaterEqual(report["rows"], 8)
            with self.assertRaisesRegex(RuntimeError, "target contains data"):
                migrate_legacy_snapshot(
                    snapshot,
                    TEST_DSN,
                    schema=TEST_SCHEMA,
                )

            ledger = MessageLedger(self.database)
            try:
                migrated = ledger.get_in_scope(
                    scope,
                    first.canonical_message_id,
                )
                self.assertEqual(migrated.rendered_text, "旧数据库中的消息")
                new_message = ledger.record_message(
                    scope,
                    native_message_id="1003",
                    sender_native_user_id="3526452465",
                    sender_display="Kenneth",
                    body=MessageBody((TextNode(0, "PostgreSQL 新消息"),)),
                    occurred_at=102,
                )
                self.assertGreater(
                    new_message.canonical_message_id,
                    first.canonical_message_id,
                )
            finally:
                ledger.close()

            self.assertEqual(
                ConversationMemory(5, self.database).get(scope.key)[0]["content"],
                "旧问题",
            )
            self.assertEqual(
                ModelPreferenceStore(self.database).get(scope.key, "default"),
                "deepseek-chat",
            )
            self.assertEqual(
                open_json_state(self.database, "learned_stickers").load(),
                learned_stickers,
            )

    def test_runtime_stores_and_pgvector_use_the_unified_schema(self) -> None:
        scope = ConversationScope(
            "onebot-v11",
            "group",
            "930690526",
            actor_native_user_id="3526452465",
            bot_native_user_id="3580515978",
        )
        ledger = MessageLedger(self.database)
        context = ContextStore(
            self.database,
            input_budget_tokens=1200,
            high_watermark_tokens=180,
            low_watermark_tokens=90,
            compartment_target_tokens=70,
            raw_tail_min_messages=3,
            max_compartments=10,
        )
        pins = PinStore(self.database, max_per_scope=10)
        reminders = ReminderStore(self.database, max_per_scope=10)
        deliveries = DeliveryStore(self.database, max_attempts=3, lease_seconds=30)
        bridges = MirrorStateStore(self.database)
        usage = UsageStore(self.database, daily_call_limit=5)
        semantic_state = SemanticIndexState(self.database)
        maintenance = MaintenanceState(self.database)
        journal = TurnJournal(self.database)
        jobs = DurableJobStore(self.database, lease_seconds=30)
        stores = (
            jobs,
            journal,
            maintenance,
            semantic_state,
            usage,
            bridges,
            deliveries,
            reminders,
            pins,
            context,
            ledger,
        )
        try:
            messages = []
            for index in range(1, 13):
                messages.append(
                    ledger.record_message(
                        scope,
                        native_message_id=str(index),
                        sender_native_user_id=str(1000 + index % 2),
                        sender_display=f"User {index % 2}",
                        body=MessageBody(
                            (TextNode(0, f"项目讨论 {index} " + "x" * 180),)
                        ),
                        occurred_at=100 + index,
                    )
                )

            roster = ledger.render_roster(scope)
            self.assertIn("User 0", roster)
            self.assertIn("User 1", roster)

            projection = context.build_projection(ledger, scope)
            valid, detail = context.verify_scope(ledger, scope)
            self.assertTrue(valid, detail)
            self.assertTrue(projection.text)

            pinned, created = pins.pin(
                ledger,
                scope,
                messages[0].canonical_message_id,
                pinned_by_principal_id=messages[0].sender_principal_id,
            )
            self.assertTrue(created)
            self.assertEqual(pinned.canonical_message_id, messages[0].canonical_message_id)

            reminder = reminders.create(
                scope,
                creator_native_user_id="3526452465",
                creator_principal_id=messages[0].sender_principal_id,
                message="检查 PostgreSQL 备份",
                scheduled_for=200,
                now=100,
            )
            self.assertEqual(reminders.claim_due(now=200)[0].reminder_id, reminder.reminder_id)
            self.assertTrue(reminders.mark_sent(reminder.reminder_id, sent_at=201))

            delivery, created = deliveries.enqueue(
                idempotency_key="integration:reply:1",
                source_scope_key=scope.key,
                source_canonical_message_id=messages[0].canonical_message_id,
                target_scope=scope,
                body=MessageBody((TextNode(0, "投递测试"),)),
                now=100,
            )
            self.assertTrue(created)
            claimed_delivery = deliveries.claim_due(now=100)[0]
            self.assertEqual(claimed_delivery.delivery_id, delivery.delivery_id)
            self.assertTrue(
                deliveries.mark_committed(
                    delivery.delivery_id,
                    native_message_id="sent-1",
                    now=101,
                )
            )

            job, created = jobs.enqueue(
                kind="integration.verify",
                idempotency_key="integration:job:1",
                payload={"schema": TEST_SCHEMA},
                scope_key=scope.key,
                now=100,
            )
            self.assertTrue(created)
            duplicate, duplicate_created = jobs.enqueue(
                kind="integration.verify",
                idempotency_key="integration:job:1",
                now=100,
            )
            self.assertFalse(duplicate_created)
            self.assertEqual(duplicate.job_id, job.job_id)
            claimed_job = jobs.claim_due("integration-worker", now=100)[0]
            self.assertTrue(
                jobs.mark_succeeded(
                    claimed_job.job_id,
                    "integration-worker",
                    result={"ok": True},
                    now=101,
                )
            )

            bridges.register_source(
                messages[0].canonical_message_id,
                scope,
                "1",
                occurred_at=101,
            )
            bridges.register_delivery(
                delivery.delivery_id,
                messages[0].canonical_message_id,
                scope,
            )
            bridges.confirm_delivery(delivery.delivery_id, "sent-1", confirmed_at=102)
            self.assertTrue(bridges.source_seen(scope, "1"))
            self.assertTrue(bridges.is_mirror_delivery(delivery.delivery_id))

            usage_id = usage.record(
                scope_key=scope.key,
                source="turn",
                provider="deepseek",
                model="deepseek-chat",
                input_tokens=40,
                output_tokens=10,
                occurred_at=100,
            )
            self.assertGreater(usage_id, 0)

            document = SemanticDocument(
                scope.key,
                "message",
                f"msg#{messages[0].canonical_message_id}",
                "PostgreSQL integration document",
                {"message_id": messages[0].canonical_message_id},
            )
            self.assertEqual(semantic_state.changed([document]), [document])
            semantic_state.mark([document])
            self.assertEqual(semantic_state.changed([document]), [])

            maintenance.mark_completed("historian", "2026-08-10")
            self.assertTrue(maintenance.completed("historian", "2026-08-10"))

            turn = journal.start_turn(
                scope,
                trigger_canonical_message_id=messages[-1].canonical_message_id,
                objective="验证 PostgreSQL",
                provider="deepseek-openai-compatible",
                model="deepseek-chat",
                prompt_version="v1",
                tool_catalog_version="tools-v1",
            )
            journal.record_tool_started(
                turn.turn_id,
                1,
                "database_check",
                {"schema": TEST_SCHEMA},
                ("read:database",),
            )
            journal.record_tool_finished(
                turn.turn_id,
                1,
                "database_check",
                "succeeded",
                '{"ok":true}',
                ("read:database",),
            )
            finished = journal.finish_turn(
                turn.turn_id,
                status="succeeded",
                final_text="数据库正常",
            )
            self.assertEqual(finished.tool_call_count, 1)

            history = ConversationMemory(5, self.database)
            history.append_turn(scope.key, "问题", "回答")
            self.assertEqual(
                ConversationMemory(5, self.database).get(scope.key)[-1]["content"],
                "回答",
            )
            group_context = GroupContextMemory(10, 2000, self.database)
            group_context.append(930690526, "Kenneth", "群上下文")
            self.assertIn(
                "群上下文",
                GroupContextMemory(10, 2000, self.database).render(930690526),
            )
            long_term = LongTermMemoryStore(self.database)
            long_term.add(
                scope.key,
                "group",
                "数据库运行在 tank",
                creator_user_id=3526452465,
            )
            self.assertIn("tank", LongTermMemoryStore(self.database).render(scope.key, ""))
            profiles = GroupUserProfileStore(self.database)
            profiles.observe(930690526, 3526452465, "Kenneth", "群名片")
            self.assertIn(
                "Kenneth",
                GroupUserProfileStore(self.database).describe_user(
                    930690526, 3526452465
                ),
            )
            preferences = ModelPreferenceStore(self.database)
            preferences.set(scope.key, "deepseek-chat")
            self.assertEqual(
                ModelPreferenceStore(self.database).get(scope.key, "default"),
                "deepseek-chat",
            )
            vector_backend = PgVectorBackend(
                TEST_DSN,
                dimensions=1024,
                schema=TEST_SCHEMA,
            )
            vector = [0.0] * 1024
            vector[0] = 1.0
            vector_backend.upsert(document, vector, model="integration-model")
            hits = vector_backend.search(
                [scope.key],
                "PostgreSQL",
                vector,
                model="integration-model",
                limit=3,
            )
            self.assertEqual(hits[0].source_handle, document.source_handle)

            def record_concurrently(index: int) -> int:
                message = ledger.record_message(
                    scope,
                    native_message_id=f"concurrent-{index}",
                    sender_native_user_id="3526452465",
                    sender_display="Kenneth",
                    body=MessageBody((TextNode(0, f"并发消息 {index}"),)),
                    occurred_at=300 + index,
                )
                return message.canonical_message_id

            with ThreadPoolExecutor(max_workers=6) as executor:
                concurrent_ids = list(executor.map(record_concurrently, range(12)))
            self.assertEqual(len(concurrent_ids), len(set(concurrent_ids)))
        finally:
            for store in stores:
                store.close()


if __name__ == "__main__":
    unittest.main()
