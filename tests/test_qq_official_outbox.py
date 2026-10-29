"""Physical attempt accounting, failure/restart boundaries and priority."""
from dataclasses import replace
from unittest.mock import patch

from tests.qq_official_outbox_fixture import OutboxFixture
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import ActorKey
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.outbox import OfficialOutbox
from src.plugins.ai_chat.adapters.qq_official.outbox_types import OutboxInvariantError, SendOutcome, SendReservation
from src.plugins.ai_chat.adapters.qq_official.rate_policy import LocalRatePermit
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.qq_official.errors import QQAPIError, classify_error


class OfficialOutboxTests(OutboxFixture):
    def setUp(self):
        super().setUp()
        self.deliveries = DeliveryStore(self.database)
        self.addCleanup(self.deliveries.close)
        self.outbox = OfficialOutbox(self.deliveries, admission=lambda c, e, a: True,
                                    pepper=b"TEST_outbox_pepper" * 3, clock=lambda: self.now)
        with self.tx() as cursor:
            result = observe_ready(cursor, self.token, {"op": 0, "t": "READY", "d": {"session_id": "TEST_SESSION", "user": {"id": "TEST_REAL_BOT", "bot": True}}}, self.now)
            self.assertEqual(result.status, "verified")
        self.window_id = self.window()

    def enqueue(self, key="TEST_LOGICAL", **kwargs):
        args = dict(rendered=render_plan(MessageBody((TextNode(0, "TEST answer"),)), self.endpoint), requester=self.actor,
            logical_key=key, source_scope_key=self.endpoint.scope.key, expires_at=self.now + 600,
            source_native_message_id="TEST_SOURCE")
        args.update(kwargs)
        return self.outbox.enqueue(self.token, **args)

    def permit(self, mode="passive"):
        return LocalRatePermit(self.account.key, self.endpoint.key, "group", mode, "unverified", float(self.now))

    def prepare(self, delivery_id, mode="passive"):
        return self.outbox.prepare(self.token, delivery_id, self.permit(mode))

    def start_request(self, reservation):
        self.assertTrue(self.outbox.mark_dispatching(self.token, reservation))
        return self.outbox.request_started(reservation)

    def test_configured_wait_ceiling_reaches_plan_and_rejects_invalid_config(self):
        for value in (True, False, 0, -1, 604801, 60.0, "60", None):
            with self.subTest(value=value), self.assertRaisesRegex(OutboxInvariantError, "invalid_wait_limit"):
                OfficialOutbox(self.deliveries, admission=lambda *_: True, pepper=b"TEST" * 8,
                               max_wait_seconds=value)
        self.outbox = OfficialOutbox(self.deliveries, admission=lambda *_: True,
            pepper=b"TEST" * 8, clock=lambda: self.now, max_wait_seconds=60)
        with self.assertRaisesRegex(OutboxInvariantError, "delivery_wait_limit_exceeded"):
            self.enqueue(expires_at=self.now + 61)
        deadline = self.now + 60
        first = self.enqueue(expires_at=deadline)
        self.now += 1
        replay = self.enqueue(expires_at=deadline)
        self.assertEqual(replay.delivery_ids, first.delivery_ids)
        self.assertFalse(replay.created)

    def test_unsolicited_missing_window_is_cancelled_and_not_revived(self):
        delivery = self.enqueue(purpose="unsolicited", expires_at=self.now + 30).delivery_ids[0]
        self.query("UPDATE qq_official_reply_windows SET state='expired'")
        result = self.outbox.candidate(self.token, delivery)
        self.assertEqual((result.status, result.reason), ("cancelled", "unsolicited_window_unavailable"))
        self.query("UPDATE qq_official_reply_windows SET state='active'")
        self.assertEqual(self.outbox.candidate(self.token, delivery).status, "cancelled")
        self.assertFalse(self.query("SELECT * FROM delivery_attempts"))

    def test_unsolicited_loses_window_between_candidate_and_prepare(self):
        delivery = self.enqueue(purpose="unsolicited", expires_at=self.now + 30).delivery_ids[0]
        self.assertEqual(self.outbox.candidate(self.token, delivery).send_mode, "passive")
        self.query("UPDATE qq_official_reply_windows SET state='expired'")
        self.assertEqual(self.prepare(delivery).status, "cancelled")
        self.assertFalse(self.query("SELECT * FROM delivery_attempts"))

    def test_progress_requires_a_registered_live_authority(self):
        with self.assertRaisesRegex(OutboxInvariantError, "progress_authority_unavailable"):
            self.enqueue(purpose="progress", expires_at=self.now + 30)
        self.assertFalse(self.query("SELECT * FROM delivery_attempts"))

    def test_prepare_uses_existing_attempt_route_and_atomic_window(self):
        ref = self.enqueue()
        with patch.object(self.deliveries, "begin_direct_attempt", wraps=self.deliveries.begin_direct_attempt) as begin:
            reservation = self.prepare(ref.delivery_ids[0])
        self.assertIsInstance(reservation, SendReservation)
        self.assertEqual(begin.call_count, 1)
        self.assertEqual(reservation.payload()["msg_seq"], 1)
        self.assertEqual(reservation.payload()["msg_id"], "TEST_SOURCE")
        window = self.query("SELECT * FROM qq_official_reply_windows")[0]
        self.assertEqual((window["next_seq"], window["reserved_count"], window["used_count"]), (2, 1, 0))
        attempt = self.query("SELECT * FROM delivery_attempts")[0]
        self.assertEqual(ActorKey.parse(attempt["bot_actor_key"]), ActorKey(self.account, "bot", "", "TEST_REAL_BOT"))
        self.assertEqual(attempt["state"], "reserved")
        self.assertEqual(attempt["wire_payload_json"], reservation.wire_json)

    def test_prepare_rollback_never_burns_seq_or_part(self):
        ref = self.enqueue()
        with patch("src.plugins.ai_chat.adapters.qq_official.outbox.reserve_send_policy", side_effect=RuntimeError("TEST rollback")):
            with self.assertRaises(RuntimeError):
                self.prepare(ref.delivery_ids[0])
        self.assertFalse(self.query("SELECT * FROM delivery_attempts"))
        self.assertEqual(self.query("SELECT next_seq FROM qq_official_reply_windows")[0]["next_seq"], 1)
        self.assertEqual(self.deliveries.get(ref.delivery_ids[0]).status, "pending")

    def test_cancel_before_request_releases_slot_but_never_reuses_seq(self):
        delivery = self.enqueue().delivery_ids[0]
        first = self.prepare(delivery)
        self.assertTrue(self.outbox.mark_dispatching(self.token, first))
        self.assertEqual(self.outbox.cancel_unsent(self.token, first).status, "pending")
        self.now += 1
        second = self.prepare(delivery)
        self.assertEqual(second.payload()["msg_seq"], 2)
        attempts = self.query("SELECT * FROM delivery_attempts ORDER BY attempt_id")
        self.assertEqual([r["state"] for r in attempts], ["cancelled_unsent", "reserved"])
        self.assertEqual([r["attempt"] for r in attempts], [1, 2])
        self.start_request(second)
        with self.assertRaisesRegex(OutboxInvariantError, "no_local_unsent_proof"):
            self.outbox.cancel_unsent(self.token, second)

    def test_handoff_single_use_and_copy_not_an_issued_handle(self):
        reservation = self.prepare(self.enqueue().delivery_ids[0])
        with self.assertRaisesRegex(OutboxInvariantError, "unissued"):
            self.outbox.mark_dispatching(self.token, replace(reservation))
        self.start_request(reservation)
        with self.assertRaisesRegex(OutboxInvariantError, "handoff"):
            self.outbox.request_started(reservation)

    def test_timeout_retains_charge_and_is_never_eligible_again(self):
        delivery = self.enqueue().delivery_ids[0]
        reservation = self.prepare(delivery)
        self.start_request(reservation)
        result = self.outbox.fail(self.token, reservation, QQAPIError("transport_ambiguous", stage="send"))
        self.assertEqual(result.status, "ambiguous")
        self.assertFalse(self.outbox.list_due(self.token))
        window = self.query("SELECT * FROM qq_official_reply_windows")[0]
        self.assertEqual((window["reserved_count"], window["used_count"]), (1, 0))
        self.assertFalse(self.deliveries.requeue(delivery))

    def test_auth_rejection_new_attempt_with_one_persisted_retry(self):
        delivery = self.enqueue().delivery_ids[0]
        first = self.prepare(delivery)
        self.start_request(first)
        rejected = classify_error(401, None, stage="request")
        self.assertEqual(self.outbox.fail(self.token, first, rejected).status, "pending")
        second = self.prepare(delivery)
        self.assertIsInstance(second, SendReservation)
        self.assertEqual(second.payload()["msg_seq"], 2)
        self.start_request(second)
        self.assertEqual(self.outbox.fail(self.token, second, rejected).status, "failed")
        row = self.query("SELECT * FROM deliveries")[0]
        self.assertEqual((row["auth_retries_used"], row["attempts"]), (1, 2))
        self.assertEqual(self.query("SELECT gateway_state FROM qq_official_accounts")[0]["gateway_state"], "paused")

    def test_format_switch_once_and_moderation_never_switches(self):
        delivery = self.enqueue().delivery_ids[0]
        first = self.prepare(delivery)
        first_wire = first.wire_json
        self.start_request(first)
        self.assertEqual(self.outbox.fail(self.token, first, classify_error(400, 50037)).status, "pending")
        second = self.prepare(delivery)
        self.assertEqual(second.payload()["msg_type"], 0)
        self.assertEqual(self.query("SELECT wire_payload_json FROM delivery_attempts WHERE attempt_id=?", (first.attempt_id,))[0]["wire_payload_json"], first_wire)
        self.start_request(second)
        self.assertEqual(self.outbox.fail(self.token, second, classify_error(400, 50037)).status, "failed")
        other = self.enqueue("TEST_MODERATION").delivery_ids[0]
        third = self.prepare(other)
        self.start_request(third)
        self.assertEqual(self.outbox.fail(self.token, third, classify_error(400, 40034006)).status, "failed")
        self.assertFalse(self.query("SELECT format_fallback_used FROM deliveries WHERE delivery_id=?", (other,))[0]["format_fallback_used"])

    def test_priority_and_ambiguous_first_part_do_not_block_new_plan(self):
        rendered = render_plan(MessageBody((TextNode(0, "alpha beta gamma delta epsilon"),)), self.endpoint, max_chars=18)
        old = self.enqueue("TEST_OLD", rendered=rendered, purpose="background_result")
        first = self.prepare(old.delivery_ids[0])
        self.start_request(first)
        self.outbox.fail(self.token, first, QQAPIError("transport_ambiguous", stage="send"))
        newest = self.enqueue("TEST_NEW")
        self.assertEqual(self.outbox.list_due(self.token), newest.delivery_ids)
        self.assertEqual(self.outbox.candidate(self.token, old.delivery_ids[1]).reason, "previous_part")

    def test_passive_rate_permission_cannot_turn_into_proactive_after_window_expiry(self):
        delivery = self.enqueue().delivery_ids[0]
        self.assertEqual(self.outbox.candidate(self.token, delivery).send_mode, "passive")
        self.query("UPDATE qq_official_endpoints SET proactive_state='allowed'")
        self.now += 300
        result = self.prepare(delivery)
        self.assertIsInstance(result, SendOutcome)
        self.assertEqual(result.reason, "mode_changed")
        self.assertFalse(self.query("SELECT * FROM delivery_attempts"))
        self.now = result.retry_at
        reservation = self.prepare(delivery, "proactive")
        self.assertNotIn("msg_id", reservation.payload())
        self.assertNotIn("msg_seq", reservation.payload())
        self.assertEqual(self.query("SELECT reserved_count FROM qq_official_quota_periods")[0]["reserved_count"], 1)

    def test_new_window_default_off_and_route_change_cancels_old_plan(self):
        delivery = self.enqueue(source_native_message_id="TEST_EXPIRED_SOURCE").delivery_ids[0]
        self.assertEqual(self.outbox.candidate(self.token, delivery).reason, "waiting_reply_window")
        self.outbox.allow_newer_windows = True
        self.now += 5
        self.assertEqual(self.outbox.candidate(self.token, delivery).send_mode, "passive")
        self.query("UPDATE qq_official_endpoints SET route_epoch=route_epoch+1")
        self.assertEqual(self.outbox.candidate(self.token, delivery).status, "cancelled")

    def test_restart_reserved_without_marker_is_ambiguous_not_unsent(self):
        delivery = self.enqueue().delivery_ids[0]
        self.prepare(delivery)
        self.account_lease.close()
        self.token = self.lease().acquire()
        self.assertFalse(self.outbox.recover(self.token))
        self.assertEqual(self.deliveries.get(delivery).status, "ambiguous")
        self.assertFalse(self.outbox.list_due(self.token))
        self.assertEqual(self.query("SELECT reserved_count FROM qq_official_reply_windows")[0]["reserved_count"], 1)

    def test_queued_authority_loss_and_last_second_dispatch_checks(self):
        delivery = self.enqueue().delivery_ids[0]
        reservation = self.prepare(delivery)
        self.query("UPDATE qq_official_endpoints SET capability_revision=capability_revision+1")
        self.assertFalse(self.outbox.mark_dispatching(self.token, reservation))
        self.assertEqual(self.outbox.cancel_unsent(self.token, reservation).status, "pending")
        self.now += 1
        self.outbox.admission = lambda c, e, a: False
        self.assertEqual(self.outbox.candidate(self.token, delivery).status, "cancelled")

    def test_connection_failure_releases_slot_with_new_attempt_and_sequence(self):
        delivery = self.enqueue().delivery_ids[0]
        first = self.prepare(delivery)
        self.start_request(first)
        result = self.outbox.fail(self.token, first, QQAPIError("connection_failed", accepted=False, stage="request"))
        self.assertEqual(result.status, "pending")
        self.now = result.retry_at
        second = self.prepare(delivery)
        self.assertEqual(second.payload()["msg_seq"], 2)
        self.assertEqual(self.query("SELECT window_charge_state FROM delivery_attempts WHERE attempt_id=?", (first.attempt_id,))[0]["window_charge_state"], "released")
