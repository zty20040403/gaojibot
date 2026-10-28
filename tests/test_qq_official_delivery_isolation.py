"""Real PostgreSQL boundaries between legacy delivery workers and official parts."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
import hashlib
import json
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.delivery import DeliveryStore, body_fingerprint
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode, body_to_json
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from src.plugins.ai_chat.turn_journal import TurnJournal
from tests.postgres_fixture import postgres_for_test


class _DatabaseTests(unittest.TestCase):
    @contextmanager
    def tx(self):
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            yield cursor
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def query(self, statement, values=()):
        with self.tx() as cursor:
            return [dict(row) for row in cursor.execute(statement, values).fetchall()]

    @staticmethod
    def insert(cursor, table, values, returning=None):
        assert table in {"qq_official_accounts", "qq_official_endpoints", "delivery_plans", "deliveries", "delivery_attempts"}
        statement = f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})"
        if returning is not None:
            statement += f" RETURNING {returning}"
        cursor.execute(statement, tuple(values.values()))
        return cursor.fetchone()[0] if returning else None


class OfficialDeliveryIsolationTests(_DatabaseTests):
    def setUp(self):
        self.database = postgres_for_test(self)
        self.store = DeliveryStore(self.database, recover_interrupted=False)
        self.addCleanup(self.store.close)
        self.now = int(time.time())
        self.body = MessageBody((TextNode(0, "TEST_BODY_SENTINEL"),))
        self.scope = ConversationScope("onebot-v11", "group", "123")
        self.endpoint = EndpointKey(AccountKey("qq-official", "TEST_APP_SENTINEL", "test"), "group", "TEST_GROUP_SENTINEL")

    def legacy(self, key="legacy", **kwargs):
        return self.store.enqueue(idempotency_key=key, source_scope_key=self.scope.key,
                                  target_scope=self.scope, body=self.body, now=self.now, **kwargs)[0]

    def official(self, status="pending", *, endpoint=None, key=None):
        endpoint = endpoint or self.endpoint
        actor = ActorKey(endpoint.account, "group_member", endpoint.native_conversation_id, "TEST_ACTOR_SENTINEL") if endpoint.kind == "group" else ActorKey(endpoint.account, "c2c_user", "", endpoint.native_conversation_id)
        plan_id = uuid.uuid4().hex
        with self.tx() as cursor:
            cursor.execute("""INSERT INTO qq_official_accounts
                (account_key,enabled,gateway_state,created_at,updated_at) VALUES (?,TRUE,'idle',?,?)
                ON CONFLICT(account_key) DO NOTHING""", (endpoint.account.key, self.now - 1000, self.now - 1000))
            cursor.execute("""INSERT INTO qq_official_endpoints
                (endpoint_key,account_key,kind,native_conversation_id,canonical_scope_key,binding_state,membership_state,created_at,updated_at)
                VALUES (?,?,?,?,?,'active','present',?,?) ON CONFLICT(endpoint_key) DO NOTHING""",
                (endpoint.key, endpoint.account.key, endpoint.kind, endpoint.native_conversation_id, endpoint.scope.key,
                 self.now - 1000, self.now - 1000))
            self.insert(cursor, "delivery_plans", dict(
                plan_id=plan_id, idempotency_key="qqp1:" + hashlib.sha256(plan_id.encode()).hexdigest(),
                account_key=endpoint.account.key, endpoint_key=endpoint.key, requester_actor_key=actor.key,
                source_scope_key=endpoint.scope.key, endpoint_generation=1, route_epoch=1, capability_revision=1,
                purpose="final", payload_mode="persistent", manifest_json='{"v":1,"parts":[]}',
                manifest_digest="a" * 64, digest_scheme="sha256", part_count=1, expires_at=self.now + 1000,
                priority=0, created_at=self.now - 1000,
            ))
            delivery_id = self.insert(cursor, "deliveries", dict(
                idempotency_key=key or "TEST_KEY_SENTINEL_" + plan_id, source_scope_key=endpoint.scope.key,
                target_platform="qq-official", target_kind=endpoint.kind,
                target_native_conversation_id=endpoint.native_conversation_id,
                reply_to_native_message_id="TEST_REPLY_SENTINEL", body_json=body_to_json(self.body),
                content_fingerprint=body_fingerprint(self.body), status="pending", next_attempt_at=self.now - 10,
                created_at=self.now - 1000, updated_at=self.now - 1000,
                plan_id=plan_id, part_index=0, account_key=endpoint.account.key, endpoint_key=endpoint.key,
                requester_actor_key=actor.key, endpoint_generation=1, route_epoch=1, capability_revision=1,
                purpose="final", payload_mode="persistent", variant="primary", payload_digest="b" * 64,
                digest_scheme="sha256", priority=0, expires_at=self.now + 1000,
                last_error="TEST_ERROR_SENTINEL",
            ), "delivery_id")
            if status in {"sending", "ambiguous", "accepted", "committed"}:
                attempt_state = "dispatched" if status == "sending" else status
                attempt_id = self.insert(cursor, "delivery_attempts", dict(
                    delivery_id=delivery_id, attempt=1, state=attempt_state, started_at=self.now - 900,
                    account_key=endpoint.account.key, endpoint_key=endpoint.key, fencing_epoch=1,
                    endpoint_generation=1, route_epoch=1, capability_revision=1,
                    send_mode="proactive", window_charge_state="none", payload_mode="persistent",
                    payload_digest="c" * 64, digest_scheme="sha256", wire_payload_json='{"content":"TEST_WIRE_SENTINEL"}',
                    send_by_at=self.now + 900, dispatched_at=self.now - 800,
                    acceptance="confirmed_yes" if status in {"accepted", "committed"} else "unknown",
                ), "attempt_id")
                cursor.execute("""UPDATE deliveries SET status=?,attempts=1,current_attempt_id=?,claim_epoch=1,
                    lease_until=?,native_message_id=? WHERE delivery_id=?""",
                    (status, attempt_id, self.now - 1, "TEST_NATIVE_SENTINEL" if status in {"accepted", "committed"} else "", delivery_id))
            elif status != "pending":
                cursor.execute("UPDATE deliveries SET status=? WHERE delivery_id=?", (status, delivery_id))
        return self.store.get(delivery_id)

    def snapshot(self, delivery_id):
        return (
            self.query("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)),
            self.query("SELECT * FROM delivery_attempts WHERE delivery_id=? ORDER BY attempt", (delivery_id,)),
        )

    def test_generic_enqueue_rejects_official_target_before_writing(self):
        for scope in (self.endpoint.scope, EndpointKey(self.endpoint.account, "private", "TEST_PRIVATE").scope):
            with self.assertRaisesRegex(ValueError, "account-bound outbox"):
                self.store.enqueue(idempotency_key="official", source_scope_key=scope.key,
                                   target_scope=scope, body=self.body)
        self.assertEqual(self.query("SELECT * FROM deliveries"), [])

    def test_legacy_idempotency_key_cannot_return_official_body(self):
        official = self.official(key="same-key")
        before = self.snapshot(official.delivery_id)
        with self.assertRaisesRegex(ValueError, "another owner"):
            self.legacy("same-key")
        self.assertEqual(self.snapshot(official.delivery_id), before)

    def test_claim_due_only_creates_attempts_for_legacy_rows(self):
        official = self.official()
        legacy = self.legacy()
        before = self.snapshot(official.delivery_id)
        self.assertEqual([row.delivery_id for row in self.store.claim_due(now=self.now)], [legacy.delivery_id])
        self.assertEqual(self.store.get(legacy.delivery_id).status, "sending")
        self.assertEqual(self.snapshot(official.delivery_id), before)

    def test_direct_attempt_and_internal_attempt_helper_cannot_bypass_owner(self):
        for status in ("pending", "failed"):
            official = self.official(status)
            before = self.snapshot(official.delivery_id)
            self.assertIsNone(self.store.begin_direct_attempt(official.delivery_id, now=self.now))
            with self.tx() as cursor:
                self.assertIsNone(self.store.begin_direct_attempt(official.delivery_id, now=self.now, transaction_cursor=cursor))
            with self.assertRaisesRegex(ValueError, "legacy delivery"), self.tx() as cursor:
                self.store._insert_attempt(cursor, official.delivery_id, 1, self.now)
            self.assertEqual(self.snapshot(official.delivery_id), before)

    def test_generic_finish_failure_and_defer_cannot_modify_official_send(self):
        official = self.official("sending")
        before = self.snapshot(official.delivery_id)
        self.assertFalse(self.store.mark_committed(official.delivery_id, native_message_id="FORGED", now=self.now))
        self.assertFalse(self.store.mark_ambiguous(official.delivery_id, "FORGED", now=self.now))
        self.assertFalse(self.store.mark_failed(official.delivery_id, "FORGED", retryable=True, now=self.now))
        # Removing fields on a stale/forged Python object cannot bypass SQL ownership.
        forged = replace(official, account_key=None, endpoint_key=None, lease_until=self.now + 1000)
        self.assertFalse(self.store.defer_unsent(forged, "FORGED"))
        with self.tx() as cursor:
            self.store._finish_attempt(cursor, official.delivery_id, "committed", "FORGED", self.now)
        self.assertEqual(self.snapshot(official.delivery_id), before)

    def test_generic_requeue_cancel_and_finish_leave_every_official_status_unchanged(self):
        for status in ("pending", "sending", "ambiguous", "accepted", "committed", "failed", "cancelled"):
            with self.subTest(status=status):
                official = self.official(status)
                before = self.snapshot(official.delivery_id)
                self.assertFalse(self.store.requeue(official.delivery_id, now=self.now))
                self.assertFalse(self.store.cancel(official.delivery_id, now=self.now))
                self.assertFalse(self.store.mark_failed(official.delivery_id, "FORGED", now=self.now))
                self.assertFalse(self.store.mark_committed(official.delivery_id, native_message_id="FORGED", now=self.now))
                self.assertEqual(self.snapshot(official.delivery_id), before)

    def test_restart_recovery_only_parks_legacy_send(self):
        official = self.official("sending")
        legacy = self.legacy()
        self.store.begin_direct_attempt(legacy.delivery_id, now=self.now)
        before = self.snapshot(official.delivery_id)
        reopened = DeliveryStore(self.database)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.recovered_ambiguous, 1)
        self.assertEqual(reopened.get(legacy.delivery_id).status, "ambiguous")
        self.assertEqual(self.snapshot(official.delivery_id), before)

    def test_expiry_recovery_only_parks_legacy_send(self):
        official = self.official("sending")
        legacy = self.legacy()
        self.store.begin_direct_attempt(legacy.delivery_id, now=self.now - 1000)
        before = self.snapshot(official.delivery_id)
        self.assertEqual(self.store.park_expired_attempts(now=self.now), 1)
        self.assertEqual(self.store.get(legacy.delivery_id).status, "ambiguous")
        self.assertEqual(self.snapshot(official.delivery_id), before)

    def test_official_echo_is_not_hashed_or_matched_by_legacy_reconciler(self):
        official = self.official("ambiguous")
        before = self.snapshot(official.delivery_id)
        with patch("src.plugins.ai_chat.delivery.body_fingerprint", side_effect=AssertionError("must not fingerprint official echo")):
            self.assertIsNone(self.store.reconcile_echo(self.endpoint.scope, self.body,
                              native_message_id="FORGED", observed_at=self.now))
        self.assertEqual(self.snapshot(official.delivery_id), before)

    def test_legacy_ambiguous_final_lookup_excludes_official_prefix_collision(self):
        official = self.official("ambiguous", key="subagent-final:999:official")
        legacy = self.legacy("subagent-final:1:legacy")
        self.store.begin_direct_attempt(legacy.delivery_id, now=self.now)
        self.store.mark_ambiguous(legacy.delivery_id, "lost", now=self.now)
        self.assertEqual([row.delivery_id for row in self.store.ambiguous_finals()], [legacy.delivery_id])
        self.assertEqual(self.store.get(official.delivery_id).status, "ambiguous")

    def test_target_scope_preserves_account_environment_and_actual_kind(self):
        endpoints = [self.endpoint,
            EndpointKey(AccountKey("qq-official", "OTHER_APP", "test"), "group", self.endpoint.native_conversation_id),
            EndpointKey(AccountKey("qq-official", self.endpoint.account.app_id, "sandbox"), "group", self.endpoint.native_conversation_id),
            EndpointKey(self.endpoint.account, "private", self.endpoint.native_conversation_id)]
        keys = set()
        for endpoint in endpoints:
            delivery = self.official(endpoint=endpoint)
            self.assertEqual(delivery.target_scope, endpoint.scope)
            self.assertEqual(delivery.target_scope.account, endpoint.account)
            keys.add(delivery.target_scope.key)
        self.assertEqual(len(keys), 4)
        self.assertEqual(self.legacy().target_scope.key, "onebot-v11:group:123")

    def test_incomplete_or_conflicting_official_target_fails_closed(self):
        delivery = self.official()
        for changes in ({"account_key": None}, {"endpoint_key": None}, {"endpoint_key": "garbage"},
                        {"target_native_conversation_id": "OTHER"}, {"target_kind": "private"},
                        {"target_platform": "onebot-v11"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "endpoint identity"):
                _ = replace(delivery, **changes).target_scope

    def test_official_operator_summary_and_repr_exclude_raw_identity_and_hashes(self):
        official = self.official("accepted")
        legacy = self.legacy()
        summaries = {row.delivery_id: row for row in self.store.recent_summaries()}
        summary = summaries[official.delivery_id]
        output = json.dumps(asdict(summary)) + repr(official)
        for value in ("TEST_APP_SENTINEL", "TEST_GROUP_SENTINEL", "TEST_ACTOR_SENTINEL", "TEST_REPLY_SENTINEL",
                      "TEST_KEY_SENTINEL", "TEST_NATIVE_SENTINEL", "TEST_ERROR_SENTINEL", "TEST_BODY_SENTINEL",
                      body_fingerprint(self.body), official.account_key, official.endpoint_key):
            self.assertNotIn(value, output)
        self.assertEqual(summary.status, "accepted")
        self.assertFalse(hasattr(summary, "body"))
        self.assertEqual(summaries[legacy.delivery_id].target_native_conversation_id, "123")
        self.assertEqual(summaries[legacy.delivery_id].source_scope_key, self.scope.key)
        self.assertEqual(self.store.stats()["accepted"], 1)

    def test_direct_attempt_joins_caller_transaction_and_rolls_back(self):
        legacy = self.legacy()
        with self.assertRaisesRegex(RuntimeError, "rollback"), self.store.transaction() as cursor:
            claimed = self.store.begin_direct_attempt(legacy.delivery_id, now=self.now, transaction_cursor=cursor)
            self.assertEqual(claimed.status, "sending")
            self.assertEqual(cursor.execute("SELECT COUNT(*) FROM delivery_attempts WHERE delivery_id=?", (legacy.delivery_id,)).fetchone()[0], 1)
            raise RuntimeError("rollback")
        self.assertEqual(self.store.get(legacy.delivery_id).status, "pending")
        self.assertEqual(self.query("SELECT * FROM delivery_attempts"), [])
        with self.store.transaction() as cursor:
            self.store.begin_direct_attempt(legacy.delivery_id, now=self.now, transaction_cursor=cursor)
        self.assertEqual(self.store.get(legacy.delivery_id).status, "sending")


class StrictTurnSendAssociationTests(_DatabaseTests):
    def setUp(self):
        self.database = postgres_for_test(self)
        self.journal = TurnJournal(self.database)
        self.addCleanup(self.journal.close)
        self.endpoint = EndpointKey(AccountKey("qq-official", "TEST_TURN_APP", "test"), "group", "TEST_TURN_GROUP")
        self.turn = self.start()
        self.other = self.start()

    def start(self):
        return self.journal.start_turn(self.endpoint.scope, trigger_canonical_message_id=1, objective="synthetic",
            provider="test", model="test", prompt_version="v1", tool_catalog_version="v1")

    def test_strict_replay_is_exact_and_cannot_reassign_turn_node_or_part(self):
        self.journal.link_send(self.turn.turn_id, 42, node_id="part", chunk_index=2, strict_association=True)
        original = self.query("SELECT * FROM turn_send_links")
        self.journal.link_send(self.turn.turn_id, 42, node_id="part", chunk_index=2, strict_association=True)
        for turn_id, node_id, chunk in ((self.other.turn_id, "part", 2), (self.turn.turn_id, "other", 2), (self.turn.turn_id, "part", 3)):
            with self.assertRaisesRegex(ValueError, "different association"):
                self.journal.link_send(turn_id, 42, node_id=node_id, chunk_index=chunk, strict_association=True)
        self.assertEqual(self.query("SELECT * FROM turn_send_links"), original)

    def test_legacy_link_reassignment_keeps_existing_behavior(self):
        self.journal.link_send(self.turn.turn_id, 42, node_id="first", chunk_index=-1)
        self.journal.link_send(self.other.turn_id, 42, node_id="second", chunk_index=3)
        row = self.query("SELECT * FROM turn_send_links")[0]
        self.assertEqual((row["turn_id"], row["node_id"], row["chunk_index"]), (self.other.turn_id, "second", 3))

    def test_link_and_official_ledger_record_roll_back_together(self):
        ledger = MessageLedger(self.database)
        self.addCleanup(ledger.close)
        bot = ActorKey(self.endpoint.account, "bot", "", "TEST_ACTUAL_BOT")
        with self.assertRaisesRegex(RuntimeError, "rollback"), self.tx() as cursor:
            stored = ledger.record_message(self.endpoint.scope, native_message_id="TEST_SENT_MESSAGE",
                sender_native_user_id=bot.native_user_id, sender_display="Bot", body=MessageBody((TextNode(0, "sent"),)),
                direction="outbound", actor=bot, source_endpoint=self.endpoint, transaction_cursor=cursor)
            self.journal.link_send(self.turn.turn_id, stored.canonical_message_id, node_id="outbox:1",
                                  transaction_cursor=cursor, strict_association=True)
            self.assertEqual(cursor.execute("SELECT COUNT(*) FROM turn_send_links").fetchone()[0], 1)
            raise RuntimeError("rollback")
        self.assertEqual(self.query("SELECT * FROM turn_send_links"), [])
        self.assertEqual(self.query("SELECT * FROM messages"), [])

    def test_conflicting_external_link_rolls_back_other_work_in_same_transaction(self):
        self.journal.link_send(self.turn.turn_id, 42, strict_association=True)
        with self.assertRaisesRegex(ValueError, "different association"), self.tx() as cursor:
            cursor.execute("UPDATE agent_turns SET objective='changed' WHERE turn_id=?", (self.other.turn_id,))
            self.journal.link_send(self.other.turn_id, 42, transaction_cursor=cursor, strict_association=True)
        self.assertEqual(self.query("SELECT objective FROM agent_turns WHERE turn_id=?", (self.other.turn_id,))[0]["objective"], "synthetic")

    def test_concurrent_strict_links_cannot_overwrite_winner(self):
        barrier = threading.Barrier(2)

        def link(turn_id):
            with self.tx() as cursor:
                barrier.wait(timeout=5)
                try:
                    self.journal.link_send(turn_id, 42, transaction_cursor=cursor, strict_association=True)
                    return turn_id
                except ValueError:
                    return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(link, turn_id) for turn_id in (self.turn.turn_id, self.other.turn_id)]
            results = [future.result(timeout=10) for future in futures]
        winners = [value for value in results if value is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.query("SELECT turn_id FROM turn_send_links")[0]["turn_id"], winners[0])

    def test_strict_mode_does_not_coerce_distinct_input_values(self):
        for kwargs in ({"chunk_index": -1}, {"chunk_index": True}, {"node_id": ""}):
            with self.assertRaisesRegex(ValueError, "invalid strict"):
                self.journal.link_send(self.turn.turn_id, 42, strict_association=True, **kwargs)
        self.assertEqual(self.query("SELECT * FROM turn_send_links"), [])
