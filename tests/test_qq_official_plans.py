from dataclasses import replace
import hashlib
import json

from tests.qq_official_outbox_fixture import OutboxFixture
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.plugins.ai_chat.adapters.qq_official.lease import LeaseLost
from src.plugins.ai_chat.adapters.qq_official.outbox_types import OutboxInvariantError
from src.plugins.ai_chat.adapters.qq_official.plans import enqueue_plan
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan


class PlanTests(OutboxFixture):
    def enqueue(self, *, cursor=None, **kwargs):
        args = dict(token=self.token, rendered=render_plan(MessageBody((TextNode(0, "TEST frozen answer"),)), self.endpoint),
                    requester=self.actor, logical_key="TEST logical", source_scope_key=self.endpoint.scope.key,
                    source_native_message_id="TEST_SOURCE", expires_at=self.now + 600, now=self.now,
                    admission=lambda c, e, a: True)
        args.update(kwargs)
        if cursor is not None:
            return enqueue_plan(cursor, **args)
        with self.tx() as cursor:
            return enqueue_plan(cursor, **args)

    def test_same_logical_plan_reuses_parts_but_changed_body_or_deadline_conflicts(self):
        first = self.enqueue()
        duplicate = self.enqueue()
        self.assertTrue(first.created)
        self.assertFalse(duplicate.created)
        self.assertEqual(first.delivery_ids, duplicate.delivery_ids)
        self.assertEqual(len(self.query("SELECT * FROM deliveries")), 1)
        for change in ({"expires_at": self.now + 601}, {"rendered": render_plan(MessageBody((TextNode(0, "DIFFERENT"),)), self.endpoint)}):
            with self.subTest(change=tuple(change)), self.assertRaisesRegex(OutboxInvariantError, "identity_conflict"):
                self.enqueue(**change)

    def test_normal_purposes_obey_default_wait_limit_and_preserve_absolute_deadline(self):
        for purpose in ("final", "requested_artifact", "background_result"):
            with self.subTest(purpose=purpose):
                with self.assertRaisesRegex(OutboxInvariantError, "delivery_wait_limit_exceeded"):
                    self.enqueue(logical_key=purpose, purpose=purpose, expires_at=self.now + 86401)
                deadline = self.now + 86400
                first = self.enqueue(logical_key=purpose, purpose=purpose, expires_at=deadline)
                self.now += 1
                replay = self.enqueue(logical_key=purpose, purpose=purpose, expires_at=deadline)
                self.assertEqual(first.delivery_ids, replay.delivery_ids)
                self.assertFalse(replay.created)
                stored = self.query("SELECT expires_at FROM delivery_plans WHERE plan_id=?", (first.plan_id,))[0]
                self.assertEqual(stored["expires_at"], deadline)

    def test_direct_plan_validates_configured_wait_limit_without_bool_coercion(self):
        for value in (True, False, 0, -1, 604801, 60.0, float("inf"), "60", None):
            with self.subTest(value=value), self.assertRaisesRegex(OutboxInvariantError, "invalid_wait_limit"):
                self.enqueue(max_wait_seconds=value)
        for value in (1, 604800):
            with self.subTest(valid_limit=value):
                self.enqueue(logical_key=f"TEST_LIMIT_{value}", max_wait_seconds=value, expires_at=self.now + value)
                with self.assertRaisesRegex(OutboxInvariantError, "delivery_wait_limit_exceeded"):
                    self.enqueue(logical_key=f"TEST_TOO_LONG_{value}", max_wait_seconds=value, expires_at=self.now + value + 1)

    def test_unsolicited_and_progress_have_short_deadlines(self):
        for purpose in ("unsolicited", "progress"):
            with self.subTest(purpose=purpose):
                with self.assertRaisesRegex(OutboxInvariantError, "delivery_wait_limit_exceeded"):
                    self.enqueue(logical_key=purpose, purpose=purpose, expires_at=self.now + 31)
                ref = self.enqueue(logical_key=purpose, purpose=purpose, expires_at=self.now + 30)
                self.assertTrue(ref.created)
                with self.assertRaisesRegex(OutboxInvariantError, "invalid_delivery_plan"):
                    self.enqueue(logical_key=purpose + "_bool", purpose=purpose, expires_at=True)

    def test_frozen_manifest_full_identity_and_parts_order(self):
        rendered = render_plan(MessageBody((TextNode(0, "abcdefghij klmnopqrst uvwxyz"),)), self.endpoint, max_chars=15)
        ref = self.enqueue(rendered=rendered)
        rows = self.query("SELECT * FROM deliveries ORDER BY part_index")
        self.assertEqual(len(rows), len(rendered.parts))
        self.assertEqual(tuple(row["delivery_id"] for row in rows), ref.delivery_ids)
        plan = self.query("SELECT * FROM delivery_plans")[0]
        self.assertEqual(json.loads(plan["manifest_json"])["rendered"], rendered.to_json())
        self.assertTrue(all(row["account_key"] == self.account.key and row["requester_actor_key"] == self.actor.key for row in rows))

    def test_same_openid_and_logical_key_across_accounts_remain_separate(self):
        first = self.enqueue()
        other = replace(self.account, environment="sandbox")
        endpoint = EndpointKey(other, "group", self.endpoint.native_conversation_id)
        self.provision(endpoint)
        actor = ActorKey(other, "group_member", endpoint.native_conversation_id, self.actor.native_user_id)
        token = self.lease(other).acquire()
        second = self.enqueue(token=token, requester=actor, rendered=render_plan(MessageBody((TextNode(0, "TEST frozen answer"),)), endpoint), source_scope_key=endpoint.scope.key)
        self.assertNotEqual(first.plan_id, second.plan_id)
        self.assertEqual(len(self.query("SELECT * FROM deliveries")), 2)

    def test_atomic_rollback_no_partial_manifest(self):
        with self.assertRaisesRegex(RuntimeError, "TEST rollback"):
            with self.tx() as cursor:
                self.enqueue(cursor=cursor)
                raise RuntimeError("TEST rollback")
        self.assertFalse(self.query("SELECT * FROM delivery_plans"))
        self.assertFalse(self.query("SELECT * FROM deliveries"))

    def test_fence_and_current_admission_before_enqueue(self):
        with self.assertRaisesRegex(OutboxInvariantError, "not_authorized"):
            self.enqueue(admission=lambda c, e, a: False)
        self.account_lease.close()
        with self.assertRaises(LeaseLost):
            self.enqueue()
        self.assertFalse(self.query("SELECT * FROM deliveries"))

    def test_ephemeral_code_and_public_hashes_never_persist(self):
        endpoint = EndpointKey(self.account, "private", "TEST_OWNER")
        self.provision(endpoint)
        actor = ActorKey(self.account, "c2c_user", "", "TEST_OWNER")
        challenge = "BC-" + "c" * 32
        with self.tx() as cursor:
            cursor.execute("""INSERT INTO qq_official_owner_grants
                (grant_id,account_key,c2c_actor_key,source,source_ref,state,created_at,updated_at)
                VALUES ('TEST_GRANT',?,?,'config','TEST_CONFIG','active',?,?)""", (self.account.key, actor.key, self.now, self.now))
            cursor.execute("""INSERT INTO qq_official_binding_challenges
                (challenge_id,account_key,issuer_actor_key,issuer_endpoint_key,owner_grant_id,owner_revision,
                 code_hmac,expires_at,state,created_at,updated_at,delivery_epoch)
                VALUES (?,?,?,?,'TEST_GRANT',1,?,?,'sending',?,?,?)""",
                (challenge, self.account.key, actor.key, endpoint.key, "a" * 64, self.now + 600,
                 self.now, self.now, self.token.epoch))
        text = "TEST binding code 817263"
        rendered = render_plan(MessageBody((TextNode(0, text),)), endpoint, markdown=False)
        with self.assertRaisesRegex(OutboxInvariantError, "delivery_wait_limit_exceeded"):
            self.enqueue(rendered=rendered, requester=actor, source_scope_key=endpoint.scope.key,
                         purpose="binding_otp", challenge_id=challenge, pepper=b"test-pepper-" * 4,
                         expires_at=self.now + 31)
        ref = self.enqueue(rendered=rendered, requester=actor, source_scope_key=endpoint.scope.key,
                           purpose="binding_otp", challenge_id=challenge, pepper=b"test-pepper-" * 4,
                           expires_at=self.now + 30)
        rows = self.query("SELECT * FROM deliveries") + self.query("SELECT * FROM delivery_plans")
        stored = json.dumps([dict(row) for row in rows], default=str)
        for value in (text, "817263", hashlib.sha256(text.encode()).hexdigest(), rendered.plan_hash,
                      rendered.source_hash, rendered.parts[0].part_hash, rendered.parts[0].text_hash):
            self.assertNotIn(value, stored)
        self.assertEqual(len(ref.delivery_ids), 1)
        self.assertEqual(rows[0]["content_fingerprint"], "")
        self.assertIsNone(rows[0]["body_archive_path"])

    def test_bot_and_cross_endpoint_requesters_cannot_grant_authority(self):
        for actor in (ActorKey(self.account, "bot", "", "TEST_BOT"),
                      ActorKey(self.account, "group_member", "TEST_OTHER", "TEST_MEMBER")):
            with self.subTest(actor=actor.identity_kind), self.assertRaises(OutboxInvariantError):
                self.enqueue(requester=actor)
