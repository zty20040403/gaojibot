"""Real lease/database tests for minimal receipts and atomic send settlement."""
from __future__ import annotations

from dataclasses import fields, replace
import hashlib
import json
import secrets
from unittest.mock import patch

from tests.qq_official_outbox_fixture import OutboxFixture
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from src.plugins.ai_chat.turn_journal import TurnJournal
from src.plugins.ai_chat.adapters.qq_official.lease import LeaseLost
from src.plugins.ai_chat.adapters.qq_official.outbox_types import (
    OfficialAttemptContext, OutboxInvariantError, digest, issue_reservation, stable_json,
)
from src.plugins.ai_chat.adapters.qq_official.plans import enqueue_plan, keyed_digest
from src.plugins.ai_chat.adapters.qq_official.receipts import finalize_receipt, record_receipt_evidence
from src.plugins.ai_chat.adapters.qq_official.renderer import content_payload, render_plan


class OfficialReceiptTests(OutboxFixture):
    def setUp(self):
        super().setUp()
        self.deliveries = DeliveryStore(self.database, recover_interrupted=False)
        self.ledger = MessageLedger(self.database)
        self.journal = TurnJournal(self.database)
        for store in (self.deliveries, self.ledger, self.journal):
            self.addCleanup(store.close)
        self.pepper = b"TEST_RECEIPT_PEPPER_LOCAL_ONLY_0123456789"
        self.bot = ActorKey(self.account, "bot", "", "TEST_ACTUAL_BOT")

    def turn(self, endpoint=None):
        endpoint = endpoint or self.endpoint
        return self.journal.start_turn(endpoint.scope, trigger_canonical_message_id=1, objective="synthetic",
            provider="test", model="test", prompt_version="v1", tool_catalog_version="v1")

    def challenge(self, endpoint, actor, token):
        grant, challenge = secrets.token_hex(16), secrets.token_hex(16)
        with self.tx() as cursor:
            cursor.execute("""INSERT INTO qq_official_owner_grants
                (grant_id,account_key,c2c_actor_key,source,source_ref,state,created_at,updated_at)
                VALUES (?,?,?,'config','TEST_CONFIG','active',?,?)""",
                (grant, endpoint.account.key, actor.key, self.now, self.now))
            cursor.execute("""INSERT INTO qq_official_binding_challenges
                (challenge_id,account_key,issuer_actor_key,issuer_endpoint_key,owner_grant_id,owner_revision,
                 code_hmac,expires_at,state,delivery_epoch,created_at,updated_at)
                VALUES (?,?,?,?,?,1,?,?,'sending',?,?,?)""",
                (challenge, endpoint.account.key, actor.key, endpoint.key, grant, "a" * 64,
                 self.now + 250, token.epoch, self.now, self.now))
        return challenge

    def attempt(self, *, endpoint=None, token=None, text="TEST_SENT_BODY", author=True, turn_id=None, ephemeral=False,
                source_native="", quote_index=None, source_scope_key=None):
        endpoint, token = endpoint or self.endpoint, token or self.token
        actor = (ActorKey(endpoint.account, "group_member", endpoint.native_conversation_id, "TEST_MEMBER")
                 if endpoint.kind == "group" else ActorKey(endpoint.account, "c2c_user", "", endpoint.native_conversation_id))
        bot = ActorKey(endpoint.account, "bot", "", "TEST_ACTUAL_BOT") if author is True else author or None
        rendered = render_plan(MessageBody((TextNode(0, text),)), endpoint)
        challenge = self.challenge(endpoint, actor, token) if ephemeral else None
        window = self.window(endpoint=endpoint, source="TEST_SOURCE_" + secrets.token_hex(4))
        window_row = self.query("SELECT * FROM qq_official_reply_windows WHERE window_id=?", (window,))[0]
        payload = content_payload(rendered, 0)
        payload.update(msg_id=window_row["credential_value"], msg_seq=1)
        if quote_index is not None:
            payload["message_reference"] = {"message_id": quote_index}
        wire = stable_json(payload)
        expires_at = self.now + (30 if ephemeral else 200)
        send_by_at = self.now + (25 if ephemeral else 190)
        with self.tx() as cursor:
            plan = enqueue_plan(cursor, token=token, rendered=rendered, requester=actor,
                logical_key="receipt-test:" + secrets.token_hex(8), source_scope_key=source_scope_key or endpoint.scope.key,
                expires_at=expires_at, now=self.now, turn_id=turn_id,
                source_native_message_id=source_native,
                purpose="binding_otp" if ephemeral else "final", challenge_id=challenge,
                pepper=self.pepper if ephemeral else None, admission=lambda *_: True)
            delivery_id = plan.delivery_ids[0]
            wire_digest = keyed_digest(self.pepper, "wire", [delivery_id, wire]) if ephemeral else digest(wire)
            attempt_fields = dict(account_key=endpoint.account.key, endpoint_key=endpoint.key,
                fencing_epoch=token.epoch, endpoint_generation=1, route_epoch=1, capability_revision=1,
                bot_actor_key=bot.key if bot else None, bot_identity_source="ready" if bot else None,
                bot_identity_revision=1 if bot else None, send_mode="passive", window_id=window, msg_seq=1,
                window_charge_state="reserved", payload_mode="ephemeral" if ephemeral else "persistent",
                payload_digest=wire_digest, digest_scheme="hmac_sha256_v1" if ephemeral else "sha256",
                digest_key_id="v1" if ephemeral else None, wire_payload_json=None if ephemeral else wire,
                send_by_at=send_by_at)
            claimed = self.deliveries.begin_direct_attempt(delivery_id, now=self.now, transaction_cursor=cursor,
                official_context=OfficialAttemptContext(token, 0, tuple(attempt_fields.items())))
            cursor.execute("UPDATE qq_official_reply_windows SET reserved_count=1,next_seq=2 WHERE window_id=?", (window,))
            cursor.execute("UPDATE delivery_attempts SET state='dispatched',dispatched_at=? WHERE attempt_id=?",
                           (self.now, claimed.current_attempt_id))
            # An independent quota reservation exercises same-transaction
            # settlement; its synthetic metric does not claim platform policy.
            quota = hashlib.sha256(secrets.token_bytes(16)).hexdigest()
            cursor.execute("""INSERT INTO qq_official_quota_periods
                (quota_key,account_key,endpoint_key,scene,send_mode,metric,period_start_at,period_end_at,
                 reset_verified,limit_count,reserved_count,policy_revision,evidence_code,created_at,updated_at)
                VALUES (?,?,?,?,'passive','synthetic_test',?,?,TRUE,100,1,1,'synthetic_test',?,?)""",
                (quota, endpoint.account.key, endpoint.key, "group" if endpoint.kind == "group" else "c2c",
                 self.now, self.now + 86400, self.now, self.now))
            cursor.execute("""INSERT INTO delivery_attempt_quotas
                (attempt_id,quota_key,account_key,endpoint_key,quota_endpoint_key,units,charge_state,created_at,updated_at)
                VALUES (?,?,?,?,?,1,'reserved',?,?)""",
                (claimed.current_attempt_id, quota, endpoint.account.key, endpoint.key, endpoint.key, self.now, self.now))
        return issue_reservation(delivery_id=delivery_id, attempt_id=claimed.current_attempt_id, attempt_number=1,
            fencing_epoch=token.epoch, endpoint=endpoint, wire_json=wire, payload_digest=wire_digest,
            send_by_at=send_by_at, payload_mode="ephemeral" if ephemeral else "persistent")

    def capture(self, reservation, body=None, kind="response"):
        body = {"id": "TEST_NATIVE", "ext_info": {"ref_idx": "TEST_REF"}} if body is None else body
        with self.tx() as cursor:
            return record_receipt_evidence(cursor, reservation=reservation, body=body, now=self.now + 1, kind=kind)

    def finalize(self, receipt, *, token=None, journal=True):
        with self.tx() as cursor:
            return finalize_receipt(cursor, token=token or self.token, receipt_id=receipt, ledger=self.ledger,
                                    journal=self.journal if journal else None, now=self.now + 2)

    def row(self, table, key, value):
        return self.query(f"SELECT * FROM {table} WHERE {key}=?", (value,))[0]

    def snapshot(self, reservation):
        return {
            "delivery": dict(self.row("deliveries", "delivery_id", reservation.delivery_id)),
            "attempt": dict(self.row("delivery_attempts", "attempt_id", reservation.attempt_id)),
            "windows": [dict(row) for row in self.query("SELECT * FROM qq_official_reply_windows ORDER BY window_id")],
            "quotas": [dict(row) for row in self.query("SELECT * FROM qq_official_quota_periods ORDER BY quota_key")],
        }

    def assert_spent_once(self, reservation):
        attempt = self.row("delivery_attempts", "attempt_id", reservation.attempt_id)
        window = self.row("qq_official_reply_windows", "window_id", attempt["window_id"])
        self.assertEqual((window["used_count"], window["reserved_count"], window["next_seq"]), (1, 0, 2))
        quota = self.query("""SELECT q.* FROM qq_official_quota_periods q JOIN delivery_attempt_quotas a USING(quota_key)
            WHERE a.attempt_id=?""", (reservation.attempt_id,))[0]
        self.assertEqual((quota["used_count"], quota["reserved_count"]), (1, 0))

    def test_capture_minimal_evidence_is_append_only_and_does_not_settle(self):
        reservation = self.attempt()
        before = self.snapshot(reservation)
        receipt = self.capture(reservation, {"id": "实际消息/ID", "ext_info": {"ref_idx": "索引/原值"},
            "content": "SECRET_REMOTE_BODY", "url": "https://secret.invalid/path", "nested": {"code": "713929"}})
        stored = dict(self.row("delivery_receipts", "receipt_id", receipt))
        self.assertEqual(stored["native_message_id"], "实际消息/ID")
        self.assertEqual(stored["ref_idx"], "索引/原值")
        self.assertIsNone(stored["applied_at"])
        for secret in ("SECRET_REMOTE_BODY", "secret.invalid", "713929"):
            self.assertNotIn(secret, repr(stored))
        self.assertEqual(self.snapshot(reservation), before)

    def test_capture_duplicate_returns_same_receipt_without_replacing_observation(self):
        reservation = self.attempt()
        first = self.capture(reservation)
        original = dict(self.row("delivery_receipts", "receipt_id", first))
        self.now += 10
        self.assertEqual(self.capture(reservation), first)
        self.assertEqual(dict(self.row("delivery_receipts", "receipt_id", first)), original)

    def test_same_native_changed_reference_is_fixed_conflict_without_overwrite(self):
        reservation = self.attempt()
        first = self.capture(reservation)
        with self.assertRaisesRegex(OutboxInvariantError, "receipt_reference_conflict"):
            self.capture(reservation, {"id": "TEST_NATIVE", "ext_info": {"ref_idx": "OTHER_REF"}})
        self.assertEqual(len(self.query("SELECT * FROM delivery_receipts")), 1)
        self.assertEqual(self.row("delivery_receipts", "receipt_id", first)["ref_idx"], "TEST_REF")

    def test_different_accepted_ids_are_retained_and_held_without_false_commit(self):
        reservation = self.attempt()
        first = self.capture(reservation, {"id": "TEST_ONE"})
        second = self.capture(reservation, {"id": "TEST_TWO"})
        self.assertNotEqual(first, second)
        outcome = self.finalize(first)
        self.assertEqual((outcome.status, outcome.reason), ("accepted", "receipt_identity_conflict"))
        self.assertEqual(len(self.query("SELECT * FROM delivery_receipts")), 2)
        self.assertEqual(self.row("deliveries", "delivery_id", reservation.delivery_id)["status"], "accepted")
        self.assertEqual(self.query("SELECT * FROM messages"), [])
        self.assert_spent_once(reservation)
        self.assertEqual(self.finalize(second).reason, "receipt_identity_conflict")
        self.assertEqual(self.row("deliveries", "delivery_id", reservation.delivery_id)["native_message_id"], "TEST_ONE")

    def test_conflicting_ref_across_response_and_verified_echo_is_held(self):
        reservation = self.attempt()
        first = self.capture(reservation)
        self.capture(reservation, {"id": "TEST_NATIVE", "ext_info": {"ref_idx": "OTHER"}}, kind="verified_echo")
        self.assertEqual(self.finalize(first).reason, "receipt_reference_conflict")
        self.assertEqual(self.query("SELECT * FROM messages"), [])

    def test_unissued_copy_and_wrong_namespace_digest_epoch_are_rejected(self):
        reservation = self.attempt()
        with self.assertRaisesRegex(OutboxInvariantError, "unissued_attempt_handle"):
            self.capture(replace(reservation))
        original = {field.name: getattr(reservation, field.name) for field in fields(reservation)}
        for changes in ({"attempt_number": 2}, {"fencing_epoch": reservation.fencing_epoch + 1},
                        {"payload_digest": "a" * 64}, {"delivery_id": reservation.delivery_id + 1},
                        {"endpoint": EndpointKey(self.account, "private", "TEST_GROUP")}, {"send_by_at": self.now + 999}):
            with self.subTest(changes=changes):
                forged = issue_reservation(**{**original, **changes})
                with self.assertRaisesRegex(OutboxInvariantError, "receipt_attempt_identity_conflict"):
                    self.capture(forged)
        self.assertEqual(self.query("SELECT * FROM delivery_receipts"), [])

    def test_invalid_success_receipts_do_not_fabricate_ids_or_indexes(self):
        reservation = self.attempt()
        invalid = ({}, {"id": True}, {"id": 123}, {"id": ""}, {"id": "  "}, {"id": "x\x00"},
                   {"id": "x\u200d"}, {"id": "x" * 257}, {"id": "ok", "code": 40034006},
                   {"id": "ok", "err_code": False}, {"id": "ok", "ext_info": []},
                   {"id": "ok", "ext_info": {"ref_idx": 7}})
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(OutboxInvariantError):
                self.capture(reservation, value)
        self.assertEqual(self.query("SELECT * FROM delivery_receipts"), [])

    def test_receipt_capture_rolls_back_with_caller_transaction(self):
        reservation = self.attempt()
        with self.assertRaisesRegex(RuntimeError, "rollback"), self.tx() as cursor:
            record_receipt_evidence(cursor, reservation=reservation, body={"id": "TEST_NATIVE"}, now=self.now)
            raise RuntimeError("rollback")
        self.assertEqual(self.query("SELECT * FROM delivery_receipts"), [])

    def test_finalization_commits_real_bot_ledger_index_journal_and_charges_once(self):
        turn = self.turn()
        reservation = self.attempt(turn_id=turn.turn_id)
        receipt = self.capture(reservation)
        self.assertEqual(self.finalize(receipt).status, "committed")
        self.assertEqual(self.finalize(receipt).status, "committed")
        self.assert_spent_once(reservation)
        self.assertEqual(len(self.query("SELECT * FROM messages")), 1)
        message = self.query("SELECT * FROM messages")[0]
        self.assertEqual(message["sender_actor_key"], self.bot.key)
        self.assertEqual(message["source_endpoint_key"], self.endpoint.key)
        self.assertEqual(message["direction"], "outbound")
        self.assertIsNone(message["reply_to_native_message_id"])
        index = self.query("SELECT * FROM qq_official_message_indexes")[0]
        self.assertEqual((index["index_kind"], index["index_value"], index["native_message_id"]), ("ref_idx", "TEST_REF", "TEST_NATIVE"))
        self.assertFalse(index["ambiguous"])
        link = self.query("SELECT * FROM turn_send_links")[0]
        self.assertEqual((link["turn_id"], link["node_id"], link["chunk_index"]), (turn.turn_id, f"outbox:{reservation.delivery_id}", 0))
        self.assertIsNotNone(self.row("delivery_receipts", "receipt_id", receipt)["applied_at"])

    def test_duplicate_echo_of_committed_attempt_is_applied_without_second_message(self):
        reservation = self.attempt()
        self.finalize(self.capture(reservation))
        echo = self.capture(reservation, kind="verified_echo")
        self.assertEqual(self.finalize(echo).status, "committed")
        self.assertEqual(len(self.query("SELECT * FROM messages")), 1)
        self.assertTrue(all(row["applied_at"] is not None for row in self.query("SELECT * FROM delivery_receipts")))
        self.assert_spent_once(reservation)

    def test_committed_replay_uses_existing_strict_link_without_journal_instance(self):
        reservation = self.attempt(turn_id=self.turn().turn_id)
        receipt = self.capture(reservation)
        self.assertEqual(self.finalize(receipt).status, "committed")
        echo = self.capture(reservation, kind="verified_echo")
        self.assertEqual(self.finalize(echo, journal=False).status, "committed")
        self.assertEqual(len(self.query("SELECT * FROM messages")), 1)
        self.assertEqual(len(self.query("SELECT * FROM turn_send_links")), 1)
        self.assertIsNotNone(self.row("delivery_receipts", "receipt_id", echo)["applied_at"])

    def test_committed_replay_does_not_invent_a_missing_journal_link(self):
        reservation = self.attempt(turn_id=self.turn().turn_id)
        receipt = self.capture(reservation)
        self.finalize(receipt)
        self.query("DELETE FROM turn_send_links")
        with self.assertRaisesRegex(OutboxInvariantError, "committed_journal_identity_conflict"):
            self.finalize(receipt, journal=False)

    def test_response_after_lease_loss_is_captured_and_new_holder_finalizes_old_epoch(self):
        reservation = self.attempt()
        self.account_lease.close()
        new_lease = self.lease()
        new_token = new_lease.acquire()
        self.assertGreater(new_token.epoch, reservation.fencing_epoch)
        receipt = self.capture(reservation)
        with self.assertRaises(LeaseLost):
            self.finalize(receipt)
        self.assertEqual(self.finalize(receipt, token=new_token).status, "committed")
        self.assert_spent_once(reservation)

    def test_route_revocation_does_not_erase_an_accepted_historical_send(self):
        reservation = self.attempt()
        receipt = self.capture(reservation)
        self.query("""UPDATE qq_official_endpoints SET binding_state='revoked',membership_state='absent',
            generation=generation+1,route_epoch=route_epoch+1 WHERE endpoint_key=?""", (self.endpoint.key,))
        self.query("UPDATE qq_official_reply_windows SET state='revoked' WHERE endpoint_key=?", (self.endpoint.key,))
        self.assertEqual(self.finalize(receipt).status, "committed")
        self.assertEqual(self.query("SELECT binding_state FROM qq_official_endpoints WHERE endpoint_key=?", (self.endpoint.key,))[0][0], "revoked")
        self.assert_spent_once(reservation)

    def test_missing_author_holds_accepted_until_attempt_evidence_is_repaired(self):
        reservation = self.attempt(author=False)
        receipt = self.capture(reservation)
        first = self.finalize(receipt)
        self.assertEqual((first.status, first.reason), ("accepted", "identity_unverified"))
        self.assertEqual(self.query("SELECT * FROM messages"), [])
        self.assertEqual(self.row("delivery_attempts", "attempt_id", reservation.attempt_id)["acceptance"], "confirmed_yes")
        self.assertIsNone(self.row("delivery_receipts", "receipt_id", receipt)["applied_at"])
        self.assert_spent_once(reservation)
        # Synthetic controlled repair of the original attempt snapshot, not a
        # fallback to the current requester or a new account identity.
        self.query("""UPDATE delivery_attempts SET bot_actor_key=?,bot_identity_source='ready',bot_identity_revision=1
            WHERE attempt_id=?""", (self.bot.key, reservation.attempt_id))
        self.assertEqual(self.finalize(receipt).status, "committed")
        self.assert_spent_once(reservation)

    def test_human_or_other_account_author_is_never_recorded_as_bot(self):
        authors = (self.actor, ActorKey(AccountKey("qq-official", "OTHER_APP", "test"), "bot", "", "TEST_ACTUAL_BOT"))
        for i, actor in enumerate(authors):
            reservation = self.attempt(author=actor)
            receipt = self.capture(reservation, {"id": f"TEST_AUTHOR_{i}"})
            self.assertEqual(self.finalize(receipt).reason, "identity_unverified")
        self.assertEqual(self.query("SELECT * FROM messages"), [])

    def test_other_account_lease_cannot_apply_receipt_with_same_native_endpoint(self):
        reservation = self.attempt()
        receipt = self.capture(reservation)
        other = EndpointKey(AccountKey("qq-official", self.account.app_id, "sandbox"), "group", self.endpoint.native_conversation_id)
        self.provision(other)
        token = self.lease(other.account).acquire()
        before = self.snapshot(reservation)
        outcome = self.finalize(receipt, token=token)
        self.assertEqual((outcome.status, outcome.reason), ("unavailable", "receipt_not_found"))
        self.assertEqual(self.snapshot(reservation), before)

    def test_late_old_receipt_does_not_overwrite_new_attempt_pointer(self):
        old = self.attempt()
        old_row = self.row("delivery_attempts", "attempt_id", old.attempt_id)
        with self.tx() as cursor:
            cursor.execute("UPDATE delivery_attempts SET state='confirmed_unaccepted',acceptance='confirmed_no' WHERE attempt_id=?", (old.attempt_id,))
            cursor.execute("UPDATE deliveries SET status='pending' WHERE delivery_id=?", (old.delivery_id,))
            names = ("account_key", "endpoint_key", "fencing_epoch", "endpoint_generation", "route_epoch", "capability_revision",
                "bot_actor_key", "bot_identity_source", "bot_identity_revision", "send_mode", "window_id", "msg_seq",
                "window_charge_state", "payload_mode", "payload_digest", "digest_scheme", "digest_key_id", "wire_payload_json", "send_by_at")
            values = {name: old_row[name] for name in names}
            values["msg_seq"] = 2
            wire = json.loads(old.wire_json)
            wire["msg_seq"] = 2
            values["wire_payload_json"] = stable_json(wire)
            values["payload_digest"] = digest(values["wire_payload_json"])
            new = self.deliveries.begin_direct_attempt(old.delivery_id, now=self.now, transaction_cursor=cursor,
                official_context=OfficialAttemptContext(self.token, 1, tuple(values.items())))
        receipt = self.capture(old)
        before = self.snapshot(old)
        self.assertEqual(self.finalize(receipt).reason, "attempt_pointer_conflict")
        self.assertEqual(self.snapshot(old), before)
        self.assertEqual(self.row("deliveries", "delivery_id", old.delivery_id)["current_attempt_id"], new.current_attempt_id)

    def test_ref_idx_collision_keeps_original_mapping_and_marks_ambiguity(self):
        first = self.attempt(text="one")
        self.finalize(self.capture(first, {"id": "TEST_FIRST", "ext_info": {"ref_idx": "SHARED_REF"}}))
        second = self.attempt(text="two")
        outcome = self.finalize(self.capture(second, {"id": "TEST_SECOND", "ext_info": {"ref_idx": "SHARED_REF"}}))
        self.assertEqual((outcome.status, outcome.reason), ("committed", "reference_index_ambiguous"))
        index = self.query("SELECT * FROM qq_official_message_indexes WHERE index_value='SHARED_REF'")[0]
        self.assertEqual(index["native_message_id"], "TEST_FIRST")
        self.assertTrue(index["ambiguous"])
        self.assertEqual(len(self.query("SELECT * FROM messages")), 2)

    def test_cross_kind_index_collision_marks_both_evidence_rows_ambiguous(self):
        first = self.attempt()
        self.finalize(self.capture(first, {"id": "FIRST", "ext_info": {"ref_idx": "COLLISION"}}))
        self.query("UPDATE qq_official_message_indexes SET index_kind='msg_idx'")
        second = self.attempt()
        result = self.finalize(self.capture(second, {"id": "SECOND", "ext_info": {"ref_idx": "COLLISION"}}))
        self.assertEqual(result.reason, "reference_index_ambiguous")
        rows = self.query("SELECT * FROM qq_official_message_indexes WHERE index_value='COLLISION'")
        self.assertEqual({row["index_kind"] for row in rows}, {"msg_idx", "ref_idx"})
        self.assertTrue(all(row["ambiguous"] for row in rows))

    def test_historical_quote_survives_later_index_ambiguity_without_rewrite(self):
        first = self.attempt()
        self.finalize(self.capture(first, {"id": "FIRST", "ext_info": {"ref_idx": "QUOTE_INDEX"}}))
        second = self.attempt(source_native="FIRST", quote_index="QUOTE_INDEX")
        receipt = self.capture(second, {"id": "SECOND"})
        self.assertEqual(self.finalize(receipt).status, "committed")
        self.query("UPDATE qq_official_message_indexes SET ambiguous=TRUE WHERE index_value='QUOTE_INDEX'")
        self.assertEqual(self.finalize(receipt).status, "committed")
        reply = self.query("SELECT reply_to_native_message_id FROM messages WHERE native_message_id='SECOND'")[0][0]
        self.assertEqual(reply, "FIRST")

    def test_source_scope_is_audit_only_and_output_uses_actual_endpoint(self):
        history = EndpointKey(self.account, "group", "OTHER_HISTORY").scope.key
        reservation = self.attempt(source_scope_key=history)
        self.assertEqual(self.finalize(self.capture(reservation)).status, "committed")
        scope = self.query("SELECT c.scope_key FROM messages m JOIN conversations c USING(conversation_id)")[0][0]
        self.assertEqual(scope, self.endpoint.scope.key)
        self.assertEqual(self.row("deliveries", "delivery_id", reservation.delivery_id)["source_scope_key"], history)

    def test_same_native_ids_across_accounts_environments_and_scene_are_independent(self):
        endpoints = (
            self.endpoint,
            EndpointKey(AccountKey("qq-official", "OTHER_APP", "test"), "group", self.endpoint.native_conversation_id),
            EndpointKey(AccountKey("qq-official", self.account.app_id, "sandbox"), "group", self.endpoint.native_conversation_id),
            EndpointKey(self.account, "private", self.endpoint.native_conversation_id),
        )
        for endpoint in endpoints:
            if endpoint != self.endpoint:
                self.provision(endpoint)
            token = self.token if endpoint.account == self.account else self.lease(endpoint.account).acquire()
            reservation = self.attempt(endpoint=endpoint, token=token)
            self.assertEqual(self.finalize(self.capture(reservation), token=token).status, "committed")
        rows = self.query("SELECT source_endpoint_key,native_message_id FROM messages")
        self.assertEqual({row["source_endpoint_key"] for row in rows}, {endpoint.key for endpoint in endpoints})
        self.assertEqual({row["native_message_id"] for row in rows}, {"TEST_NATIVE"})
        self.assertTrue(all(not row["ambiguous"] for row in self.query("SELECT * FROM qq_official_message_indexes")))

    def test_same_native_id_for_two_parts_is_not_silently_reassigned(self):
        first = self.attempt(text="one")
        first_receipt = self.capture(first)
        self.finalize(first_receipt)
        second = self.attempt(text="two")
        outcome = self.finalize(self.capture(second))
        self.assertEqual((outcome.status, outcome.reason), ("accepted", "native_message_identity_conflict"))
        self.assertEqual(len(self.query("SELECT * FROM messages")), 1)
        self.assertEqual(self.query("SELECT rendered_text FROM messages")[0][0], "one")
        self.assertEqual(self.finalize(first_receipt).status, "ambiguous")
        self.assertEqual(self.row("deliveries", "delivery_id", first.delivery_id)["status"], "committed")

    def test_otp_code_never_enters_receipts_ledger_manifest_or_public_hash(self):
        endpoint = EndpointKey(self.account, "private", "TEST_OWNER")
        self.provision(endpoint)
        text = "私密绑定码：713929"
        reservation = self.attempt(endpoint=endpoint, text=text, ephemeral=True)
        receipt = self.capture(reservation, {"id": "TEST_OTP_MESSAGE", "content": text, "secret": text})
        self.assertEqual(self.finalize(receipt).status, "committed")
        message = self.query("SELECT * FROM messages")[0]
        self.assertEqual(message["body_json"], '{"v":1,"nodes":[]}')
        self.assertEqual(message["rendered_text"], "")
        self.assertEqual(message["message_kind"], "command")
        snapshots = {table: [dict(row) for row in self.query(f"SELECT * FROM {table}")] for table in
            ("delivery_plans", "deliveries", "delivery_attempts", "delivery_receipts", "messages")}
        encoded = repr(snapshots)
        self.assertNotIn(text, encoded)
        self.assertNotIn(digest(text), encoded)
        self.assertNotIn(digest(reservation.wire_json), encoded)
        self.assertIsNone(snapshots["delivery_attempts"][0]["wire_payload_json"])
        self.assertEqual(snapshots["delivery_plans"][0]["manifest_json"], '{"v":1,"ephemeral":true}')
        self.assertEqual(snapshots["deliveries"][0]["content_fingerprint"], "")
        self.assertNotIn(text, repr(reservation))

    def test_failed_ledger_finalization_rolls_back_charges_and_state_but_retains_receipt(self):
        reservation = self.attempt()
        receipt = self.capture(reservation)
        before = self.snapshot(reservation)
        with patch.object(self.ledger, "record_message", side_effect=RuntimeError("synthetic storage outage")):
            with self.assertRaisesRegex(RuntimeError, "storage outage"):
                self.finalize(receipt)
        self.assertEqual(self.snapshot(reservation), before)
        self.assertIsNone(self.row("delivery_receipts", "receipt_id", receipt)["applied_at"])
        self.assertEqual(self.finalize(receipt).status, "committed")

    def test_failure_after_ledger_and_journal_write_rolls_back_the_complete_commit(self):
        turn = self.turn()
        reservation = self.attempt(turn_id=turn.turn_id)
        receipt = self.capture(reservation)
        before = self.snapshot(reservation)
        original = self.journal.link_send

        def fail_after_link(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("synthetic post-link failure")

        with patch.object(self.journal, "link_send", side_effect=fail_after_link):
            with self.assertRaisesRegex(RuntimeError, "post-link"):
                self.finalize(receipt)
        self.assertEqual(self.snapshot(reservation), before)
        for table in ("messages", "qq_official_messages", "qq_official_message_indexes", "turn_send_links"):
            self.assertEqual(self.query(f"SELECT * FROM {table}"), [])
        self.assertEqual(self.finalize(receipt).status, "committed")

    def test_missing_journal_preserves_accepted_receipt_for_local_retry(self):
        reservation = self.attempt(turn_id=self.turn().turn_id)
        receipt = self.capture(reservation)
        result = self.finalize(receipt, journal=False)
        self.assertEqual((result.status, result.reason), ("accepted", "turn_journal_unavailable"))
        self.assertEqual(self.query("SELECT * FROM messages"), [])
        self.assertEqual(self.finalize(receipt).status, "committed")

    def test_receipt_conflict_after_commit_is_visible_without_overwriting_history(self):
        reservation = self.attempt()
        self.finalize(self.capture(reservation))
        before = self.snapshot(reservation)
        conflict = self.capture(reservation, {"id": "OTHER_ACCEPTED_ID"})
        outcome = self.finalize(conflict)
        self.assertEqual((outcome.status, outcome.reason), ("ambiguous", "receipt_identity_conflict"))
        self.assertEqual(self.snapshot(reservation), before)
        self.assertEqual(self.query("SELECT native_message_id FROM messages")[0][0], "TEST_NATIVE")

    def test_accepted_receipt_never_reuses_existing_inbound_message_as_outbound(self):
        self.ledger.record_message(self.endpoint.scope, native_message_id="TEST_NATIVE", sender_native_user_id=self.actor.native_user_id,
            sender_display="human", body=MessageBody((TextNode(0, "human text"),)), actor=self.actor, source_endpoint=self.endpoint)
        reservation = self.attempt()
        outcome = self.finalize(self.capture(reservation))
        self.assertEqual(outcome.reason, "native_message_identity_conflict")
        message = self.query("SELECT * FROM messages")[0]
        self.assertEqual((message["direction"], message["rendered_text"]), ("inbound", "human text"))
