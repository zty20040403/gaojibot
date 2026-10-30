"""Real execution -> paragraph plan -> existing M3 ordering/window transitions.

No network. The parent validation batch supplies the normal isolated PG runner.
The fixture is composed, not subclassed, so unrelated tests are not rerun.
"""
import unittest
from unittest.mock import patch

from tests import test_qq_official_turn_platform as platform_tests
from src.plugins.ai_chat.adapters.qq_official import turn_platform
from src.plugins.ai_chat.adapters.qq_official.rate_policy import LocalRatePermit
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.adapters.qq_official.turn_platform import TurnDeliveryPolicy
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.qq_official.errors import QQAPIError


class ReplyParagraphDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = platform_tests.PlatformTests(
            "test_frozen_result_handoff_is_atomic_and_duplicate_never_reenqueues")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        await self.fixture.asyncSetUp()
        self.addAsyncCleanup(self.fixture.asyncTearDown)
        self.f = self.fixture.f
        self.outbox = self.fixture.outbox

    async def enqueue(self, text="第一段\n\n第二段[split]第三段"):
        self.fixture.freeze(MessageBody((TextNode(0, text),)))
        result = await self.fixture.platform.send(self.fixture.request())
        self.assertEqual(result.status, "pending")
        return result.receipt["delivery_ids"]

    def start_request(self, delivery):
        permit = LocalRatePermit(self.f.account.key, self.f.endpoint.key, "group", "passive",
            "unverified", float(self.f.now))
        reservation = self.outbox.prepare(self.f.token, delivery, permit)
        self.assertTrue(self.outbox.mark_dispatching(self.f.token, reservation))
        self.outbox.request_started(reservation)
        return reservation

    def commit(self, reservation, message_id):
        receipt = self.outbox.capture_receipt(reservation, {"id": message_id})
        result = self.outbox.finalize(self.f.token, receipt, ledger=self.f.ledger)
        self.assertEqual(result.status, "committed")
        return result

    async def test_actual_final_handoff_persists_paragraphs_and_requires_previous_committed(self):
        ids = await self.enqueue()
        self.assertEqual(len(ids), 3)
        self.assertEqual([p["text_content"] for p in self.fixture.manifest()["parts"]],
            ["第一段", "第二段", "第三段"])
        rows = self.f.query("SELECT * FROM deliveries ORDER BY part_index")
        self.assertEqual([row["part_index"] for row in rows], [0, 1, 2])
        self.assertTrue(all(row["expires_at"] == self.fixture.handle.deadline_at for row in rows))
        self.assertTrue(all(row["reply_to_native_message_id"] == "TEST_INPUT" for row in rows))
        self.assertEqual(self.outbox.list_due(self.f.token), (ids[0],))
        first = self.start_request(ids[0])
        receipt = self.outbox.capture_receipt(first, {"id": "TEST_FIRST"})
        self.assertFalse(self.outbox.list_due(self.f.token))
        self.assertEqual(self.outbox.finalize(self.f.token, receipt, ledger=self.f.ledger).status, "committed")
        self.assertEqual(self.outbox.list_due(self.f.token), (ids[1],))
        self.commit(self.start_request(ids[1]), "TEST_SECOND")
        self.assertEqual(self.outbox.list_due(self.f.token), (ids[2],))
        self.commit(self.start_request(ids[2]), "TEST_THIRD")
        attempts = self.f.query("SELECT msg_seq FROM delivery_attempts ORDER BY msg_seq")
        self.assertEqual([row["msg_seq"] for row in attempts], [1, 2, 3])
        self.assertEqual((await self.fixture.platform.send(self.fixture.request())).status, "committed")
        self.assertEqual(len(self.f.query("SELECT * FROM delivery_attempts")), 3)

    async def test_unknown_first_paragraph_stops_later_parts_without_new_attempt(self):
        ids = await self.enqueue()
        first = self.start_request(ids[0])
        result = self.outbox.fail(self.f.token, first, QQAPIError("transport_ambiguous", stage="send"))
        self.assertEqual(result.status, "ambiguous")
        self.assertFalse(self.outbox.list_due(self.f.token))
        self.assertEqual(self.outbox.candidate(self.f.token, ids[1]).reason, "previous_part")
        self.assertEqual(len(self.f.query("SELECT * FROM delivery_attempts")), 1)
        self.assertFalse(self.fixture.deliveries.requeue(ids[0]))

    async def test_exhausted_window_waits_without_reservation_or_deadline_extension(self):
        self.f.query("UPDATE qq_official_reply_windows SET max_replies=1")
        ids = await self.enqueue()
        self.commit(self.start_request(ids[0]), "TEST_ONLY_SLOT")
        result = self.outbox.candidate(self.f.token, ids[1])
        self.assertEqual((result.status, result.reason), ("pending", "waiting_reply_window"))
        self.assertEqual(len(self.f.query("SELECT * FROM delivery_attempts")), 1)
        window = self.f.query("SELECT next_seq,used_count,reserved_count FROM qq_official_reply_windows")[0]
        self.assertEqual((window["next_seq"], window["used_count"], window["reserved_count"]), (2, 1, 0))
        self.assertTrue(all(row["expires_at"] == self.fixture.handle.deadline_at
            for row in self.f.query("SELECT expires_at FROM deliveries")))
        self.f.now = self.fixture.handle.deadline_at
        self.assertEqual(self.outbox.candidate(self.f.token, ids[1]).reason, "delivery_expired")
        self.assertFalse(self.outbox.list_due(self.f.token))
        self.assertEqual(len(self.f.query("SELECT * FROM delivery_attempts")), 1)

    async def test_unrepresentable_tail_creates_no_partial_durable_plan(self):
        self.fixture.platform = self.fixture.make_platform(policy=TurnDeliveryPolicy(max_chars=2))
        self.fixture.freeze(MessageBody((TextNode(0, "a\n\nb\n\nc\n\nddd"),)))
        result = await self.fixture.platform.send(self.fixture.request())
        self.assertEqual((result.status, result.reason), ("failed", "needs_attachment"))
        self.assertFalse(self.f.query("SELECT * FROM delivery_plans"))
        self.assertFalse(self.f.query("SELECT * FROM deliveries"))
        self.assertFalse(self.f.query("SELECT * FROM delivery_attempts"))

    async def test_already_frozen_legacy_plan_is_never_resegmented_on_replay(self):
        def old_renderer(*args, **kwargs):
            kwargs.pop("paragraphs")
            return render_plan(*args, **kwargs)
        with patch.object(turn_platform, "render_plan", side_effect=old_renderer):
            ids = await self.enqueue()
        self.assertEqual(len(ids), 1)
        original = self.fixture.manifest()
        with patch.object(turn_platform, "render_plan", side_effect=AssertionError("must restore existing plan")):
            replay = await self.fixture.platform.send(self.fixture.request())
        self.assertEqual(replay.receipt["delivery_ids"], ids)
        self.assertEqual(self.fixture.manifest(), original)
        self.assertEqual(len(self.f.query("SELECT * FROM delivery_plans")), 1)
