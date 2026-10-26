from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

from src.bot_storage.database import _translate_sql

import nonebot

nonebot.init()

from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.message_ir import MessageBody, TextNode


class DeliveryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DeliveryStore(":memory:", max_attempts=3, lease_seconds=30)
        self.addCleanup(self.store.close)
        self.scope = ConversationScope("onebot-v11", "group", "100")
        self.body = MessageBody((TextNode(0, "hello"),))

    def enqueue(self, key: str = "turn:1:chunk:0"):
        return self.store.enqueue(
            idempotency_key=key,
            source_scope_key=self.scope.key,
            target_scope=self.scope,
            body=self.body,
            turn_id=1,
            now=100,
        )

    def test_enqueue_is_idempotent_and_claim_is_leased(self) -> None:
        first, created = self.enqueue()
        second, duplicate_created = self.enqueue()
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first.delivery_id, second.delivery_id)

        claimed = self.store.claim_due(now=100)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].status, "sending")
        self.assertEqual(claimed[0].attempts, 1)

    def test_timeout_parks_until_echo_reconciles(self) -> None:
        delivery, _created = self.enqueue()
        self.store.begin_direct_attempt(delivery.delivery_id, now=100)
        self.assertTrue(
            self.store.mark_ambiguous(delivery.delivery_id, "timeout", now=101)
        )
        self.assertEqual(self.store.claim_due(now=1000), [])

        reconciled = self.store.reconcile_echo(
            self.scope,
            self.body,
            native_message_id="9001",
            observed_at=102,
        )
        self.assertIsNotNone(reconciled)
        self.assertEqual(reconciled.status, "committed")  # type: ignore[union-attr]
        self.assertEqual(reconciled.native_message_id, "9001")  # type: ignore[union-attr]

    def test_retryable_failure_uses_bounded_attempts(self) -> None:
        delivery, _created = self.enqueue()
        claimed = self.store.claim_due(now=100)[0]
        self.assertTrue(
            self.store.mark_failed(
                claimed.delivery_id,
                "network",
                retryable=True,
                retry_seconds=10,
                now=101,
            )
        )
        self.assertEqual(self.store.claim_due(now=110), [])
        second = self.store.claim_due(now=111)[0]
        self.assertEqual(second.attempts, 2)

    def test_ambiguous_finals_filter_and_postgres_parameters(self) -> None:
        final, _ = self.enqueue("subagent-final:1:2")
        other, _ = self.enqueue("turn:2:chunk:0")
        for delivery in (final, other):
            self.store.begin_direct_attempt(delivery.delivery_id, now=100)
            self.store.mark_ambiguous(delivery.delivery_id, "receipt lost", now=101)
        self.assertEqual([d.delivery_id for d in self.store.ambiguous_finals()], [final.delivery_id])
        connection = Mock()
        connection.execute.return_value.fetchall.return_value = []
        with patch.object(self.store, "_connection", connection):
            self.store.ambiguous_finals(limit=3)
        statement, parameters = connection.execute.call_args.args
        translated, _ = _translate_sql(statement)
        PostgresQuery(Transformer()).convert(translated, parameters)
        self.assertEqual(parameters, ("subagent-final:%", 3))

    def test_interrupted_send_is_ambiguous_after_reopen_semantics(self) -> None:
        delivery, _created = self.enqueue()
        self.store.begin_direct_attempt(delivery.delivery_id, now=100)
        count = self.store.park_interrupted_attempts(now=101)
        self.assertEqual(count, 1)
        self.assertEqual(
            self.store.get(delivery.delivery_id).status,  # type: ignore[union-attr]
            "ambiguous",
        )

    def test_maintenance_client_does_not_recover_a_live_senders_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outbox.sqlite3"
            sender = DeliveryStore(path)
            try:
                delivery, _ = sender.enqueue(idempotency_key="live", source_scope_key=self.scope.key,
                    target_scope=self.scope, body=self.body, now=100)
                sender.begin_direct_attempt(delivery.delivery_id, now=100)
                maintenance = DeliveryStore(path, recover_interrupted=False)
                try:
                    self.assertEqual(maintenance.recovered_ambiguous, 0)
                    self.assertEqual(sender.get(delivery.delivery_id).status, "sending")
                finally:
                    maintenance.close()
            finally:
                sender.close()
            recovered = DeliveryStore(path)
            try:
                self.assertEqual(recovered.recovered_ambiguous, 1)
                self.assertEqual(recovered.get(delivery.delivery_id).status, "ambiguous")
            finally:
                recovered.close()

    def test_expired_lease_is_parked_without_retry(self) -> None:
        delivery, _created = self.enqueue("expired")
        self.store.begin_direct_attempt(delivery.delivery_id, now=100)
        self.assertEqual(self.store.park_expired_attempts(now=129), 0)
        self.assertEqual(self.store.park_expired_attempts(now=130), 1)
        self.assertEqual(
            self.store.get(delivery.delivery_id).status,  # type: ignore[union-attr]
            "ambiguous",
        )
        self.assertEqual(self.store.claim_due(now=200), [])

    def test_recent_summaries_do_not_select_or_decode_message_bodies(self) -> None:
        delivery, _created = self.enqueue()
        statements: list[str] = []
        self.store._connection.set_trace_callback(statements.append)

        summaries = self.store.recent_summaries()

        self.assertEqual(summaries[0].delivery_id, delivery.delivery_id)
        self.assertFalse(hasattr(summaries[0], "body"))
        select = next(
            statement
            for statement in statements
            if "FROM deliveries" in statement
        )
        self.assertNotIn("body_json", select)


if __name__ == "__main__":
    unittest.main()
