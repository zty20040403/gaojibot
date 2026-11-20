from dataclasses import replace
import json

from tests.qq_official_outbox_fixture import OutboxFixture
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.plugins.ai_chat.adapters.qq_official.lease import LeaseLost
from src.plugins.ai_chat.adapters.qq_official.send_types import OutboxInvariantError
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

    def test_retired_otp_purposes_cannot_create_a_plan(self):
        self.assertIsNone(self.query("SELECT to_regclass('qq_official_admin_otp_sources') AS source")[0]['source'])
        for purpose in ('approval_otp', 'binding_otp'):
            with self.subTest(purpose=purpose), self.assertRaisesRegex(OutboxInvariantError, 'invalid_delivery_plan'):
                self.enqueue(purpose=purpose)
        self.assertFalse(self.query('SELECT * FROM delivery_plans'))

    def test_same_logical_plan_reuses_original_parts(self):
        first = self.enqueue()
        duplicate = self.enqueue()
        self.assertTrue(first.created)
        self.assertFalse(duplicate.created)
        self.assertEqual(first.delivery_ids, duplicate.delivery_ids)
        self.assertEqual(len(self.query("SELECT * FROM deliveries")), 1)
        replay = self.enqueue(expires_at=self.now+601)
        self.assertEqual(replay.delivery_ids, first.delivery_ids)

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

    def test_progress_uses_task_deadline(self):
        ref = self.enqueue(purpose='progress', expires_at=self.now+600)
        self.assertTrue(ref.created)

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

    def test_bot_and_cross_endpoint_requesters_cannot_grant_authority(self):
        for actor in (ActorKey(self.account, "bot", "", "TEST_BOT"),
                      ActorKey(self.account, "group_member", "TEST_OTHER", "TEST_MEMBER")):
            with self.subTest(actor=actor.identity_kind), self.assertRaises(OutboxInvariantError):
                self.enqueue(requester=actor)
