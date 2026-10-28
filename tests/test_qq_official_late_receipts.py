"""Live-holder receipt reconciliation uses local PostgreSQL only; no HTTP calls."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.lease import LeaseLost, assert_epoch
from src.plugins.ai_chat.adapters.qq_official.outbox import OfficialOutbox
from src.plugins.ai_chat.adapters.qq_official.outbox_types import OutboxInvariantError
from src.plugins.ai_chat.adapters.qq_official.rate_policy import LocalRatePermit
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.adapters.qq_official.sender import OfficialSender
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey
from tests.qq_official_outbox_fixture import OutboxFixture


class OfficialLateReceiptTests(OutboxFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = DatabaseCalls()
        self.deliveries = DeliveryStore(self.database)
        self.ledger = MessageLedger(self.database)
        self.addCleanup(self.deliveries.close)
        self.addCleanup(self.ledger.close)
        self.outbox = OfficialOutbox(self.deliveries, admission=lambda c, e, a: True,
            pepper=b"TEST_LATE_RECEIPT_PEPPER" * 2, clock=lambda: self.now)
        with self.tx() as cursor:
            observe_ready(cursor, self.token, {"op": 0, "t": "READY", "d": {
                "session_id": "TEST_SESSION", "user": {"id": "TEST_BOT", "bot": True}}}, self.now)
        self.window()
        self.rest = SimpleNamespace(
            token_manager=SimpleNamespace(credentials=SimpleNamespace(app_id=self.account.app_id),
                get=AsyncMock(return_value="TEST_ACCESS_TOKEN")),
            send_message=AsyncMock(side_effect=AssertionError("receipt reconciliation must not send")),
        )
        self.monotonic = 1000.0
        self.sender = self.make_sender()

    async def asyncTearDown(self):
        await self.sender.stop()
        await self.calls.close()

    async def ensure_current(self):
        def check():
            with self.tx() as cursor:
                assert_epoch(cursor, self.token)
        await self.calls(check)

    def make_sender(self):
        return OfficialSender(self.account, self.token, self.outbox, self.rest, self.ledger,
            database_calls=self.calls, ensure_current=self.ensure_current,
            clock=lambda: self.monotonic, wall_clock=lambda: self.now,
            poll_seconds=0.01, receipt_sweep_seconds=5)

    def attempt(self, key):
        rendered = render_plan(MessageBody((TextNode(0, "TEST receipt recovery"),)), self.endpoint)
        delivery = self.outbox.enqueue(self.token, rendered=rendered, requester=self.actor,
            logical_key=key, source_scope_key=self.endpoint.scope.key, expires_at=self.now + 200,
            source_native_message_id="TEST_SOURCE").delivery_ids[0]
        permit = LocalRatePermit(self.account.key, self.endpoint.key, "group", "passive", "unverified", self.monotonic)
        reservation = self.outbox.prepare(self.token, delivery, permit)
        self.assertTrue(self.outbox.mark_dispatching(self.token, reservation))
        self.outbox.request_started(reservation)
        return reservation

    def capture(self, reservation, *, native="TEST_LATE_NATIVE", number=1):
        with patch("src.plugins.ai_chat.adapters.qq_official.receipts.secrets.token_hex", return_value=f"{number:032x}"):
            return self.outbox.capture_receipt(reservation, {"id": native})

    async def new_holder(self):
        await self.calls(self.account_lease.close)
        self.account_lease = self.lease()
        self.token = await self.calls(self.account_lease.acquire)
        self.sender = self.make_sender()

    async def eventually(self, predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)

    async def test_receipt_appended_after_new_holder_initial_scan_is_finalized(self):
        reservation = self.attempt("TEST_LATE_AFTER_START")
        await self.new_holder()
        self.assertEqual(await self.sender.recover(), ())
        self.assertEqual(self.deliveries.get(reservation.delivery_id).status, "ambiguous")
        receipt = self.capture(reservation)
        self.assertEqual(self.outbox.pending_receipts(self.token), (receipt,))
        result = await self.sender.sweep_receipts()
        self.assertEqual([item.status for item in result], ["committed"])
        self.assertEqual(self.deliveries.get(reservation.delivery_id).status, "committed")
        self.assertEqual(self.outbox.pending_receipts(self.token), ())
        self.assertEqual(await self.sender.sweep_receipts(), ())
        self.assertEqual(len(self.query("SELECT * FROM messages WHERE direction='outbound'")), 1)
        self.rest.send_message.assert_not_awaited()

    async def test_scan_is_fenced_account_scoped_and_has_no_recovery_side_effects(self):
        reservation = self.attempt("TEST_SCAN_ONLY")
        receipt = self.capture(reservation)
        before = [dict(row) for row in self.query("SELECT * FROM delivery_attempts")]
        self.assertEqual(self.outbox.pending_receipts(self.token), (receipt,))
        self.assertEqual(self.outbox.pending_receipts(self.token, after_receipt_id=receipt), ())
        self.assertEqual([dict(row) for row in self.query("SELECT * FROM delivery_attempts")], before)
        other = AccountKey("qq-official", "TEST_OTHER_RECEIPT_ACCOUNT", "test")
        self.provision(EndpointKey(other, "group", self.endpoint.native_conversation_id))
        other_token = self.lease(other).acquire()
        self.assertEqual(self.outbox.pending_receipts(other_token), ())
        await self.calls(self.account_lease.close)
        with self.assertRaises(LeaseLost):
            self.outbox.pending_receipts(self.token)

    async def test_bounded_pages_allow_later_receipt_past_held_first_hundred(self):
        await self.sender.recover()
        held = self.attempt("TEST_HELD_PREFIX")
        final = self.attempt("TEST_FINAL_AFTER_HELD_PREFIX")
        for number in range(1, 102):
            self.capture(held, native=f"TEST_CONFLICT_{number}", number=number)
        final_receipt = self.capture(final, native="TEST_FINAL_NATIVE", number=200)
        first = self.outbox.pending_receipts(self.token)
        second = self.outbox.pending_receipts(self.token, after_receipt_id=first[-1])
        self.assertEqual((len(first), len(second)), (100, 2))
        self.assertIn(final_receipt, second)
        self.assertEqual(len(self.outbox.recover(self.token)), 100)
        first_results = await self.sender.sweep_receipts()
        self.assertEqual(len(first_results), 100)
        self.assertTrue(all(result.status == "accepted" for result in first_results))
        self.assertEqual(self.deliveries.get(final.delivery_id).status, "sending")
        second_results = await self.sender.sweep_receipts()
        self.assertEqual(len(second_results), 2)
        self.assertEqual(self.deliveries.get(final.delivery_id).status, "committed")
        self.assertEqual(len(self.query("SELECT * FROM delivery_receipts WHERE applied_at IS NULL")), 101)
        self.assertEqual(len(self.sender._receipt_ids), 0)
        self.rest.send_message.assert_not_awaited()

    async def test_running_sender_sweeps_on_interval_without_repeating_each_poll(self):
        reservation = self.attempt("TEST_PERIODIC_LATE_RECEIPT")
        await self.new_holder()
        await self.sender.recover()
        with patch.object(self.outbox, "pending_receipts", wraps=self.outbox.pending_receipts) as scan, \
                patch.object(self.outbox, "list_due", wraps=self.outbox.list_due) as poll:
            loop = asyncio.create_task(self.sender.run())
            try:
                await self.eventually(lambda: poll.call_count >= 1)
                polled = poll.call_count
                self.monotonic += 5
                self.sender.wake()
                await self.eventually(lambda: scan.call_count >= 1 and poll.call_count > polled)
                scanned = scan.call_count
                self.capture(reservation)
                for _ in range(5):
                    self.sender.wake()
                    await asyncio.sleep(0.01)
                self.assertEqual(scan.call_count, scanned)
                self.assertEqual(self.deliveries.get(reservation.delivery_id).status, "ambiguous")
                self.monotonic += 5
                self.sender.wake()
                await self.eventually(lambda: self.deliveries.get(reservation.delivery_id).status == "committed")
                self.assertEqual(scan.call_count, scanned + 1)
            finally:
                await self.sender.stop()
                await loop
        self.rest.send_message.assert_not_awaited()

    async def test_scan_rejects_unbounded_or_invalid_cursor_arguments(self):
        for limit in (0, -1, 101, True, "100"):
            with self.subTest(limit=limit), self.assertRaisesRegex(OutboxInvariantError, "invalid_receipt_scan"):
                self.outbox.pending_receipts(self.token, limit=limit)
        for after in ("", "x" * 65, "BAD\nCURSOR", 1):
            with self.subTest(after=after), self.assertRaisesRegex(OutboxInvariantError, "invalid_receipt_scan"):
                self.outbox.pending_receipts(self.token, after_receipt_id=after)
