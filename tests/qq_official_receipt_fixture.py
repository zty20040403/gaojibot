"""Real lease/database tests for minimal receipts and atomic send settlement."""
from __future__ import annotations

import hashlib
import secrets

from tests.qq_official_outbox_fixture import OutboxFixture
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import ActorKey
from src.plugins.ai_chat.turn_journal import TurnJournal
from src.plugins.ai_chat.adapters.qq_official.send_types import (
    OfficialAttemptContext, digest, SendReservation, stable_json,
)
from src.plugins.ai_chat.adapters.qq_official.plans import enqueue_plan, keyed_digest
from src.plugins.ai_chat.adapters.qq_official.send_ledger import finalize_receipt, record_receipt
from src.plugins.ai_chat.adapters.qq_official.renderer import content_payload, render_plan


class ReceiptFixture(OutboxFixture):
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
        return SendReservation(delivery_id=delivery_id, attempt_id=claimed.current_attempt_id, attempt_number=1,
            fencing_epoch=token.epoch, endpoint=endpoint, wire_json=wire, payload_digest=wire_digest,
            send_by_at=send_by_at, payload_mode="ephemeral" if ephemeral else "persistent")

    def capture(self, reservation, body=None, kind="response"):
        body = {"id": "TEST_NATIVE", "ext_info": {"ref_idx": "TEST_REF"}} if body is None else body
        with self.tx() as cursor:
            return record_receipt(cursor, reservation, body, self.now + 1)

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
