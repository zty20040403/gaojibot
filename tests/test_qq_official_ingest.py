"""Real isolated PostgreSQL ingestion transactions; no model or network sends."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import secrets
import threading
from types import SimpleNamespace
import unittest

from tests.postgres_fixture import postgres_for_test
from src.plugins.ai_chat.adapters.qq_official.ingest import (
    process_inbox,
)
from src.plugins.ai_chat.adapters.qq_official.lease import LeaseLost
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MentionNode
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey
from src.plugins.ai_chat.storage.jobs import DurableJobStore


NOW = 1_800_000_000


class QQOfficialIngestTests(unittest.TestCase):
    def setUp(self):
        self.database = postgres_for_test(self)
        self.ledger = MessageLedger(self.database)
        self.jobs = DurableJobStore(self.database)
        self.addCleanup(self.ledger.close)
        self.addCleanup(self.jobs.close)
        self.account = AccountKey("qq-official", "TEST_APP", "test")
        self.endpoint = EndpointKey(self.account, "group", "TEST_GROUP")
        self.token = SimpleNamespace(account_key=self.account.key, epoch=1)
        self.provision(self.endpoint)

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

    def query(self, sql, args=()):
        with self.tx() as cursor:
            cursor.execute(sql, args)
            return cursor.fetchall() if sql.lstrip().upper().startswith("SELECT") else []

    def provision(self, endpoint, *, binding=None, membership=None):
        with self.tx() as cursor:
            cursor.execute("""INSERT INTO qq_official_accounts
                (account_key,enabled,fencing_epoch,gateway_state,created_at,updated_at)
                VALUES (?,TRUE,1,'active',?,?) ON CONFLICT DO NOTHING""", (endpoint.account.key, NOW, NOW))
            cursor.execute("""INSERT INTO qq_official_endpoints
                (endpoint_key,account_key,kind,native_conversation_id,canonical_scope_key,
                 binding_state,membership_state,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)""", (endpoint.key, endpoint.account.key, endpoint.kind,
                    endpoint.native_conversation_id, endpoint.scope.key,
                    binding or ("active" if endpoint.kind == "group" else "unbound"),
                    membership or ("present" if endpoint.kind == "group" else "unknown"), NOW, NOW))

    def seed(self, *, endpoint=None, native=None, mention=False, reference=None, own_index=None,
             issued=NOW - 20, received=NOW, bot=False, author=None, body=None, generation=1, route=1):
        endpoint = endpoint or self.endpoint
        native = native or "TEST_MSG_" + secrets.token_hex(4)
        inbox = secrets.token_hex(16)
        author_id = author or ("TEST_MEMBER" if endpoint.kind == "group" else endpoint.native_conversation_id)
        raw = {"op": 0, "t": "GROUP_MESSAGE_CREATE" if endpoint.kind == "group" else "C2C_MESSAGE_CREATE",
               "s": 42, "id": "TEST_EVENT_" + inbox,
               "d": {"id": native, "content": body or "TEST conversation text",
                     "author": {"member_openid" if endpoint.kind == "group" else "user_openid": author_id,
                                "nickname": "TEST display", "bot": bot}}}
        if endpoint.kind == "group":
            raw["d"]["group_openid"] = endpoint.native_conversation_id
        if issued is not None:
            raw["d"]["timestamp"] = datetime.fromtimestamp(issued, timezone.utc).isoformat()
        if mention:
            raw["d"]["mentions"] = [{"member_openid": "TEST_BOT", "is_you": True}]
        ext = {}
        if reference is not None:
            ext["ref_msg_idx"] = reference
        if own_index is not None:
            ext["msg_idx"] = own_index
        raw["d"]["message_scene"] = {"ext": ext}
        with self.tx() as cursor:
            cursor.execute("""INSERT INTO qq_official_inbound_events
                (inbox_id,account_key,endpoint_key,endpoint_generation,route_epoch,event_type,
                 native_message_id,event_id,payload_json,received_at,payload_expires_at,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (inbox, endpoint.account.key, endpoint.key, generation,
                    route, raw["t"], native, raw["id"], json.dumps(raw, ensure_ascii=False), received,
                    received + 86400, received, received))
        return inbox

    def guard(self, cursor, token):
        row = cursor.execute("""SELECT fencing_epoch,enabled FROM qq_official_accounts
            WHERE account_key=? FOR UPDATE""", (token.account_key,)).fetchone()
        if row is None or not row["enabled"] or row["fencing_epoch"] != token.epoch:
            raise LeaseLost("TEST stale epoch")

    def process(self, inbox, *, cursor=None, **kwargs):
        args = dict(inbox_id=inbox, lease_token=self.token, ledger=self.ledger, jobs=self.jobs,
                    epoch_guard=self.guard, admission=lambda cursor, endpoint, actor: True, now=NOW)
        args.update(kwargs)
        if cursor is not None:
            return process_inbox(cursor, **args)
        with self.tx() as transaction:
            return process_inbox(transaction, **args)

    def count(self, table):
        return self.query("SELECT COUNT(*) AS n FROM " + table)[0]["n"]


    def test_group_message_then_mention_record_in_order_without_jobs(self):
        ordinary = self.process(self.seed(native='TEST_FIRST'))
        trigger = self.process(self.seed(native='TEST_SECOND', mention=True))
        self.assertFalse(ordinary.reply_requested)
        self.assertTrue(trigger.reply_requested)
        self.assertLess(ordinary.canonical_message_id, trigger.canonical_message_id)
        self.assertEqual(self.count('durable_jobs'), 0)

    def test_idempotent_replay_does_not_refresh_window_or_create_another_turn(self):
        inbox = self.seed(mention=True)
        first = self.process(inbox)
        self.query("UPDATE qq_official_reply_windows SET used_count=2,reserved_count=1,next_seq=4")
        before = dict(self.query("SELECT * FROM qq_official_reply_windows")[0])
        replay = self.process(inbox, now=NOW + 100)
        self.assertEqual(replay, first)
        self.assertEqual(self.count("durable_jobs"), 0)
        self.assertEqual(dict(self.query("SELECT * FROM qq_official_reply_windows")[0]), before)
        self.assertEqual(before["expires_at"], NOW - 20 + 300 - 5)

    def test_later_at_evidence_upgrades_pending_without_changing_first_body(self):
        inbox = self.seed(body="TEST original body")
        self.query("UPDATE qq_official_inbound_events SET trigger_requested=TRUE WHERE inbox_id=?", (inbox,))
        result = self.process(inbox)
        self.assertTrue(result.reply_requested)
        stored = self.ledger.get_in_scope(self.endpoint.scope, result.canonical_message_id)
        self.assertEqual(stored.rendered_text, "TEST original body")


    def test_existing_window_observation_preserves_original_expiry_and_usage(self):
        inbox = self.seed(native="TEST_WINDOW", mention=True)
        self.query("""INSERT INTO qq_official_reply_windows
            (window_id,account_key,endpoint_key,endpoint_generation,route_epoch,credential_kind,
             credential_value,issued_at,received_at,expires_at,max_replies,used_count,reserved_count,
             next_seq,state,created_at,updated_at)
            VALUES ('TEST_WINDOW_ROW',?,?,1,1,'msg_id','TEST_WINDOW',?,?,?,5,2,1,4,'active',?,?)""",
            (self.account.key, self.endpoint.key, NOW - 100, NOW - 100, NOW + 100, NOW - 100, NOW - 90))
        before = dict(self.query("SELECT * FROM qq_official_reply_windows")[0])
        self.process(inbox)
        self.assertEqual(dict(self.query("SELECT * FROM qq_official_reply_windows")[0]), before)

    def test_caller_rollback_rolls_back_ledger_identity_indexes_window_job_and_inbox(self):
        inbox = self.seed(mention=True, own_index="TEST_IDX")
        with self.assertRaisesRegex(RuntimeError, "TEST rollback"):
            with self.tx() as cursor:
                self.process(inbox, cursor=cursor)
                self.assertEqual(self.count("messages"), 0)  # No internal commit.
                raise RuntimeError("TEST rollback")
        for table in ("messages", "principals", "qq_official_messages", "qq_official_message_indexes",
                      "qq_official_reply_windows", "durable_jobs"):
            self.assertEqual(self.count(table), 0, table)
        self.assertEqual(self.query("SELECT status FROM qq_official_inbound_events")[0]["status"], "pending")
        self.assertTrue(self.process(inbox).reply_requested)


    def test_concurrent_workers_create_one_logical_turn(self):
        inbox = self.seed(mention=True)
        barrier = threading.Barrier(2)
        def work(_):
            barrier.wait(timeout=10)
            return self.process(inbox)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(work, range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.count("messages"), 1)
        self.assertEqual(self.count("durable_jobs"), 0)

    def test_stale_epoch_raises_before_message_read_or_any_side_effect(self):
        inbox = self.seed(mention=True)
        self.query("UPDATE qq_official_accounts SET fencing_epoch=2")
        with self.assertRaises(LeaseLost):
            self.process(inbox)
        self.assertEqual(self.count("messages"), 0)
        self.assertEqual(self.query("SELECT status FROM qq_official_inbound_events")[0]["status"], "pending")

    def test_old_generation_route_removed_and_unbound_groups_are_terminal(self):
        for change in ("generation=generation+1", "route_epoch=route_epoch+1", "membership_state='absent'",
                       "membership_state='unknown'", "binding_state='unbound'", "binding_state='revoked'"):
            endpoint = EndpointKey(self.account, "group", "TEST_" + secrets.token_hex(5))
            self.provision(endpoint)
            inbox = self.seed(endpoint=endpoint, mention=True)
            self.query("UPDATE qq_official_endpoints SET " + change + " WHERE endpoint_key=?", (endpoint.key,))
            with self.subTest(change=change):
                self.assertEqual(self.process(inbox).status, "revoked")
        self.assertEqual(self.count("messages"), 0)

    def test_canonical_history_cannot_change_actual_route(self):
        inbox = self.seed(mention=True)
        other_scope = EndpointKey(self.account, "group", "TEST_OTHER").scope.key
        self.query("UPDATE qq_official_endpoints SET canonical_scope_key=?", (other_scope,))
        self.assertEqual(self.process(inbox).reason_code, "endpoint_identity_mismatch")
        self.assertEqual(self.count("messages"), 0)

    def test_private_unbound_requires_current_admission_and_default_is_deny(self):
        endpoint = EndpointKey(self.account, "private", "TEST_PRIVATE")
        self.provision(endpoint)
        denied = self.seed(endpoint=endpoint)
        self.assertEqual(self.process(denied, admission=None).reason_code, "policy_denied")
        allowed = self.seed(endpoint=endpoint)
        received = []
        def admit(cursor, actual, actor):
            received.append((actual, actor))
            return actual == endpoint and actor.native_user_id == "TEST_PRIVATE"
        result = self.process(allowed, admission=admit)
        self.assertTrue(result.reply_requested)
        self.assertEqual(received[0][0], endpoint)
        window = self.query("SELECT expires_at,max_replies FROM qq_official_reply_windows")[0]
        self.assertEqual(window["max_replies"], 4)
        self.assertEqual(window["expires_at"], NOW - 20 + 3600 - 5)

    def test_exact_reply_target_triggers_without_mention(self):
        original = self.process(self.seed(native="TEST_TARGET", own_index="TEST_REF"))
        reply = self.process(self.seed(reference="TEST_REF"))
        self.assertTrue(reply.reply_requested)
        stored = self.ledger.get_in_scope(self.endpoint.scope, reply.canonical_message_id)
        self.assertEqual(stored.reply_to_canonical_message_id, original.canonical_message_id)
        self.assertEqual(stored.reply_to_native_message_id, "TEST_TARGET")
        self.assertEqual(self.count("qq_official_message_indexes"), 1)  # References never register aliases.
        self.assertTrue(self.process(self.seed(reference="TEST_REF")).reply_requested)
        self.assertEqual(self.count("qq_official_message_indexes"), 1)

    def test_same_index_other_account_endpoint_or_environment_cannot_trigger(self):
        other_endpoints = [EndpointKey(self.account, "group", "TEST_OTHER"),
                           EndpointKey(AccountKey("qq-official", "TEST_OTHER_APP", "test"), "group", "TEST_GROUP"),
                           EndpointKey(AccountKey("qq-official", "TEST_APP", "sandbox"), "group", "TEST_GROUP")]
        for endpoint in other_endpoints:
            self.provision(endpoint)
            token = SimpleNamespace(account_key=endpoint.account.key, epoch=1)
            self.process(self.seed(endpoint=endpoint, own_index="TEST_SHARED_INDEX"), lease_token=token)
        reply = self.process(self.seed(reference="TEST_SHARED_INDEX"))
        self.assertFalse(reply.reply_requested)
        self.assertIsNone(self.ledger.get_in_scope(self.endpoint.scope, reply.canonical_message_id).reply_to_native_message_id)

    def test_conflicting_index_marks_ambiguous_and_preserves_first_target(self):
        self.process(self.seed(native="TEST_FIRST", own_index="TEST_COLLISION"))
        self.process(self.seed(native="TEST_SECOND", own_index="TEST_COLLISION"))
        index = self.query("SELECT * FROM qq_official_message_indexes")[0]
        self.assertTrue(index["ambiguous"])
        self.assertEqual(index["native_message_id"], "TEST_FIRST")
        self.assertFalse(self.process(self.seed(reference="TEST_COLLISION")).reply_requested)

    def test_index_kind_collision_with_different_target_is_unresolved(self):
        self.process(self.seed(native="TEST_A", own_index="TEST_INDEX"))
        other = self.process(self.seed(native="TEST_B"))
        self.query("""INSERT INTO qq_official_message_indexes
            (endpoint_key,account_key,index_kind,index_value,native_message_id,canonical_message_id,created_at,updated_at)
            VALUES (?,?,'ref_idx','TEST_INDEX','TEST_B',?,?,?)""",
            (self.endpoint.key, self.account.key, other.canonical_message_id, NOW, NOW))
        self.assertFalse(self.process(self.seed(reference="TEST_INDEX")).reply_requested)

    def test_outbound_receipt_and_bot_origin_never_make_human_ledger_or_turn(self):
        self.query("""INSERT INTO qq_official_messages
            (message_id,account_key,endpoint_key,native_message_id,direction,created_at)
            VALUES ('TEST_OUTBOUND',?,?,'TEST_SENT','outbound',?)""", (self.account.key, self.endpoint.key, NOW))
        result = self.process(self.seed(native="TEST_SENT", mention=True))
        self.assertEqual(result.echo_state, "confirmed_self")
        result = self.process(self.seed(bot=True, mention=True))
        self.assertEqual(result.echo_state, "possible_self")
        self.assertEqual(self.count("messages"), 0)
        self.assertEqual(self.count("durable_jobs"), 0)
        self.assertEqual(self.count("qq_official_reply_windows"), 0)


    def test_outbound_ref_index_can_resolve_without_inventing_a_bot_ledger_author(self):
        self.query("""INSERT INTO qq_official_messages
            (message_id,account_key,endpoint_key,native_message_id,direction,created_at)
            VALUES ('TEST_OUTBOUND',?,?,'TEST_SENT','outbound',?)""", (self.account.key, self.endpoint.key, NOW))
        self.query("""INSERT INTO qq_official_message_indexes
            (endpoint_key,account_key,index_kind,index_value,native_message_id,created_at,updated_at)
            VALUES (?,?,'ref_idx','TEST_OUT_REF','TEST_SENT',?,?)""", (self.endpoint.key, self.account.key, NOW, NOW))
        result = self.process(self.seed(reference="TEST_OUT_REF"))
        self.assertTrue(result.reply_requested)
        message = self.ledger.get_in_scope(self.endpoint.scope, result.canonical_message_id)
        self.assertEqual(message.reply_to_native_message_id, "TEST_SENT")
        self.assertIsNone(message.reply_to_canonical_message_id)
        self.assertEqual(self.count("messages"), 1)

    def test_global_bot_id_does_not_alias_group_or_c2c_human_identity(self):
        self.query("""UPDATE qq_official_accounts SET bot_native_id='TEST_SELF',
            bot_identity_source='verified_test',bot_identity_verified_at=?""", (NOW,))
        result = self.process(self.seed(author="TEST_SELF", mention=True))
        self.assertEqual(result.echo_state, "not_echo")
        self.assertTrue(result.reply_requested)
        private = EndpointKey(self.account, "private", "TEST_SELF")
        self.provision(private)
        result = self.process(self.seed(endpoint=private))
        self.assertEqual(result.echo_state, "not_echo")
        self.assertTrue(result.reply_requested)
        normal = self.process(self.seed(author=self.account.app_id, mention=True))
        self.assertTrue(normal.reply_requested)  # AppID is never the bot author.

    def test_bot_mention_does_not_create_a_human_group_principal(self):
        result = self.process(self.seed(mention=True, body="<@TEST_BOT> TEST hello"))
        body = self.ledger.get_in_scope(self.endpoint.scope, result.canonical_message_id).body
        self.assertFalse(any(isinstance(node, MentionNode) for node in body.nodes))
        native = self.query("SELECT native_user_id FROM principal_identities")
        self.assertEqual([row["native_user_id"] for row in native], ["TEST_MEMBER"])

    def test_duplicate_message_alias_never_enqueues_a_second_turn(self):
        first = self.process(self.seed(native="TEST_SAME", mention=True))
        second = self.process(self.seed(native="TEST_SAME", mention=True))
        self.assertEqual(second.reason_code, "duplicate_message")
        self.assertEqual(second.canonical_message_id, first.canonical_message_id)
        self.assertFalse(second.reply_requested)
        self.assertEqual(self.count("durable_jobs"), 0)

    def test_missing_or_future_timestamp_cannot_create_fresh_window(self):
        for issued in (None, NOW + 60):
            self.process(self.seed(issued=issued, mention=True))
        self.assertEqual(self.count("qq_official_reply_windows"), 0)
        self.process(self.seed(issued=NOW - 500, mention=True))
        window = self.query("SELECT * FROM qq_official_reply_windows")[0]
        self.assertEqual(window["state"], "expired")
        self.assertLess(window["expires_at"], NOW)

    def test_payload_error_and_identity_mismatch_are_terminal_not_retries(self):
        for edit in ("bad_json", "wrong_endpoint", "wrong_event", "nul"):
            inbox = self.seed(mention=True)
            raw = json.loads(self.query("SELECT payload_json FROM qq_official_inbound_events WHERE inbox_id=?", (inbox,))[0][0])
            if edit == "wrong_endpoint":
                raw["d"]["group_openid"] = "OTHER"
            elif edit == "wrong_event":
                raw["id"] = "OTHER_EVENT"
            elif edit == "nul":
                raw["d"]["content"] = "private\x00text"
            self.query("UPDATE qq_official_inbound_events SET payload_json=? WHERE inbox_id=?",
                       ("{broken" if edit == "bad_json" else json.dumps(raw), inbox))
            with self.subTest(edit=edit):
                self.assertEqual(self.process(inbox).status, "quarantined")
        self.assertEqual(self.count("messages"), 0)
        self.assertEqual(self.count("durable_jobs"), 0)

    def test_expired_payload_and_different_account_are_not_processed(self):
        inbox = self.seed()
        self.query("UPDATE qq_official_inbound_events SET payload_expires_at=?", (NOW - 1,))
        self.assertEqual(self.process(inbox).reason_code, "payload_expired")
        other = EndpointKey(AccountKey("qq-official", "TEST_OTHER", "test"), "group", "TEST_GROUP")
        self.provision(other)
        other_inbox = self.seed(endpoint=other)
        self.assertEqual(self.process(other_inbox).status, "missing")
        self.assertEqual(self.count("messages"), 0)



if __name__ == "__main__":
    unittest.main()
