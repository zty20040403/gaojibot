"""Cross-review regressions for evidence/authority changes between send phases."""
from tests.qq_official_outbox_fixture import OutboxFixture
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.outbox import OfficialOutbox
from src.plugins.ai_chat.adapters.qq_official.outbox_types import SendReservation
from src.plugins.ai_chat.adapters.qq_official.rate_policy import LocalRatePermit
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.message_ir import MentionNode, MessageBody, TextNode
from src.qq_official.errors import QQAPIError, classify_error


class OfficialOutboxCrossReviewTests(OutboxFixture):
    def setUp(self):
        super().setUp()
        self.deliveries = DeliveryStore(self.database, recover_interrupted=False)
        self.addCleanup(self.deliveries.close)
        self.mentions = frozenset({self.actor})
        self.outbox = OfficialOutbox(self.deliveries, admission=lambda *_: True,
            pepper=b"TEST_REVIEW_PEPPER" * 3, clock=lambda: self.now,
            mention_authority=lambda *_: self.mentions)
        with self.tx() as cursor:
            observe_ready(cursor, self.token, {"op": 0, "t": "READY", "d": {
                "session_id": "TEST_SESSION", "user": {"id": "TEST_BOT", "bot": True}}}, self.now)
        self.window()

    def enqueue(self, *, mention=False):
        body = MessageBody((MentionNode(0, self.actor.native_user_id, "TEST_DISPLAY"),)) if mention else MessageBody((TextNode(0, "TEST_BODY"),))
        plan = render_plan(body, self.endpoint, allowed_mentions=self.mentions)
        return self.outbox.enqueue(self.token, rendered=plan, requester=self.actor,
            logical_key="TEST_REVIEW", source_scope_key=self.endpoint.scope.key,
            source_native_message_id="TEST_SOURCE", expires_at=self.now + 200).delivery_ids[0]

    def prepare(self, delivery):
        permit = LocalRatePermit(self.account.key, self.endpoint.key, "group", "passive", "unverified", float(self.now))
        result = self.outbox.prepare(self.token, delivery, permit)
        self.assertIsInstance(result, SendReservation)
        return result

    def start(self, reservation):
        self.assertTrue(self.outbox.mark_dispatching(self.token, reservation))
        return self.outbox.request_started(reservation)

    def test_late_prior_attempt_receipt_blocks_an_already_reserved_retry(self):
        delivery = self.enqueue()
        first = self.prepare(delivery)
        self.start(first)
        self.assertEqual(self.outbox.fail(self.token, first, classify_error(401, None)).status, "pending")
        second = self.prepare(delivery)
        self.outbox.capture_receipt(first, {"id": "TEST_LATE_ACCEPTED"})
        self.assertFalse(self.outbox.mark_dispatching(self.token, second))
        row = self.query("SELECT state FROM delivery_attempts WHERE attempt_id=?", (second.attempt_id,))[0]
        self.assertEqual(row["state"], "reserved")
        self.assertEqual(self.outbox.cancel_unsent(self.token, second).status, "pending")
        self.assertEqual(self.outbox.candidate(self.token, delivery).reason, "receipt_needs_finalization")

    def test_mention_authority_removed_after_reserve_blocks_handoff(self):
        reservation = self.prepare(self.enqueue(mention=True))
        self.assertIn("<qqbot-at-user", reservation.wire_json)
        self.mentions = frozenset()
        self.assertFalse(self.outbox.mark_dispatching(self.token, reservation))

    def test_error_settlement_does_not_require_new_mention_authority(self):
        reservation = self.prepare(self.enqueue(mention=True))
        self.start(reservation)
        self.mentions = frozenset()
        result = self.outbox.fail(self.token, reservation, QQAPIError("transport_ambiguous", stage="send"))
        self.assertEqual(result.status, "ambiguous")
        row = self.query("SELECT state FROM delivery_attempts WHERE attempt_id=?", (reservation.attempt_id,))[0]
        self.assertEqual(row["state"], "ambiguous")

    def test_unrecoverable_plain_variant_still_records_confirmed_rejection(self):
        reservation = self.prepare(self.enqueue(mention=True))
        self.start(reservation)
        self.mentions = frozenset()
        result = self.outbox.fail(self.token, reservation, classify_error(400, 50037))
        self.assertEqual(result.status, "failed")
        attempt = self.query("SELECT state,acceptance FROM delivery_attempts WHERE attempt_id=?", (reservation.attempt_id,))[0]
        self.assertEqual((attempt["state"], attempt["acceptance"]), ("confirmed_unaccepted", "confirmed_no"))
        self.assertFalse(self.query("SELECT format_fallback_used FROM deliveries")[0]["format_fallback_used"])
