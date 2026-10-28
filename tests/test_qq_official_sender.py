"""Real PostgreSQL + explicitly fake loopback transport, never a live QQ account."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import threading
import unittest
from unittest.mock import patch

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.authority import OwnerAuthority
from src.plugins.ai_chat.adapters.qq_official.binding import BindingContext, BindingReducer, parse_binding_command
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.lease import LeaseLost, assert_epoch
from src.plugins.ai_chat.adapters.qq_official.outbox import OfficialOutbox
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.adapters.qq_official.sender import OfficialSender, SenderUnavailable
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.qq_official.credentials import Credentials
from src.qq_official.errors import QQAPIError
from src.qq_official.http import RestClient
from src.qq_official.testing.fake_platform import FakePlatform
from src.qq_official.token import TokenManager
from tests.qq_official_outbox_fixture import OutboxFixture


class OfficialSenderTests(OutboxFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = DatabaseCalls()
        self.deliveries = DeliveryStore(self.database)
        self.ledger = MessageLedger(self.database)
        self.addCleanup(self.deliveries.close)
        self.addCleanup(self.ledger.close)
        self.outbox = OfficialOutbox(self.deliveries, admission=lambda c, e, a: True,
            pepper=b"TEST_PROTECTED_PEPPER" * 3, clock=lambda: self.now)
        with self.tx() as cursor:
            observe_ready(cursor, self.token, {"op": 0, "t": "READY", "d": {
                "session_id": "TEST_SESSION", "user": {"id": "TEST_REAL_BOT", "bot": True}}}, self.now)
        self.window()
        self.fake = FakePlatform(clock=lambda: self.now)
        self.fake.app_id = self.account.app_id
        await self.fake.start()
        self.fake.add_window("group", self.endpoint.native_conversation_id, "TEST_SOURCE")
        self.tokens = TokenManager(Credentials(self.account.app_id, self.fake.app_secret),
            self.fake.base_url, allow_test_server=True)
        self.rest = RestClient(self.tokens, allow_test_server=True)
        self.sender = self.make_sender()

    async def asyncTearDown(self):
        await self.sender.stop()
        await self.calls.close()
        await self.rest.aclose()
        await self.tokens.aclose()
        await self.fake.close()

    async def ensure_current(self):
        def check():
            with self.tx() as cursor:
                assert_epoch(cursor, self.token)
        await self.calls(check)

    def make_sender(self, **kwargs):
        return OfficialSender(self.account, self.token, self.outbox, self.rest, self.ledger,
            database_calls=self.calls, ensure_current=self.ensure_current,
            wall_clock=lambda: self.now, poll_seconds=0.01, **kwargs)

    def enqueue(self, key="TEST_LOGICAL", *, endpoint=None, actor=None, **kwargs):
        endpoint = endpoint or self.endpoint
        args = dict(rendered=render_plan(MessageBody((TextNode(0, "TEST final **answer**"),)), endpoint),
            requester=actor or self.actor, logical_key=key, source_scope_key=endpoint.scope.key,
            expires_at=self.now + 200, source_native_message_id="TEST_SOURCE")
        args.update(kwargs)
        return self.outbox.enqueue(self.token, **args).delivery_ids[0]

    async def eventually(self, predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)

    def attempts(self):
        return self.query("SELECT * FROM delivery_attempts ORDER BY attempt_id")

    async def test_loopback_send_uses_real_outbox_and_commits_bot_ledger(self):
        delivery = self.enqueue()
        with patch.object(self.rest, "send_message", wraps=self.rest.send_message) as send:
            result = await self.sender.send_delivery(delivery)
            self.assertEqual(result.status, "committed")
            self.assertEqual(send.await_count, 1)
        self.assertEqual(len(self.fake.received), 1)
        payload = self.fake.received[0]["payload"]
        self.assertEqual((payload["msg_id"], payload["msg_seq"]), ("TEST_SOURCE", 1))
        self.assertEqual(self.attempts()[0]["state"], "committed")
        self.assertEqual(self.fake.token_calls, 1)
        native = self.fake.received[0]["receipt"]["id"]
        canonical = self.ledger.canonical_id_for_native(self.endpoint.scope, native, endpoint=self.endpoint)
        self.assertIsNotNone(canonical)
        row = self.query("SELECT * FROM messages WHERE canonical_message_id=?", (canonical,))[0]
        self.assertEqual(ActorKey.parse(row["sender_actor_key"]), ActorKey(self.account, "bot", "", "TEST_REAL_BOT"))
        self.assertEqual((await self.sender.send_delivery(delivery)).status, "committed")
        self.assertEqual(len(self.fake.received), 1)

    async def test_token_preflight_failure_creates_no_attempt_or_charge(self):
        delivery = self.enqueue()
        with patch.object(self.tokens, "get", side_effect=QQAPIError("SECRET_TOKEN_FAILURE", stage="token")):
            with self.assertRaisesRegex(SenderUnavailable, "token_preflight_unavailable") as error:
                await self.sender.send_delivery(delivery)
        self.assertNotIn("SECRET", str(error.exception))
        self.assertFalse(self.attempts())
        self.assertEqual(self.query("SELECT next_seq,reserved_count FROM qq_official_reply_windows")[0]["next_seq"], 1)
        self.assertFalse(self.fake.received)

    async def test_dropped_response_is_ambiguous_and_never_replayed(self):
        delivery = self.enqueue()
        self.fake.drop_next_response = True
        result = await self.sender.send_delivery(delivery)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(len(self.fake.received), 1)
        self.assertEqual((await self.sender.send_delivery(delivery)).status, "ambiguous")
        self.assertEqual(len(self.fake.received), 1)
        self.assertFalse(self.query("SELECT * FROM delivery_receipts"))
        self.assertEqual(self.attempts()[0]["acceptance"], "unknown")

    async def test_auth_retry_is_a_new_attempt_not_inline_transport_retry(self):
        delivery = self.enqueue()
        self.fake.next_errors.append((401, 0))
        with patch.object(self.rest, "send_message", wraps=self.rest.send_message) as send:
            self.assertEqual((await self.sender.send_delivery(delivery)).status, "pending")
            self.assertEqual(send.await_count, 1)
            # FakePlatform otherwise always returns the rejected token, which
            # M0 correctly refuses to revive as a newly authenticated token.
            self.fake.access_token = "TEST_ACCESS_TOKEN_REFRESHED"
            self.assertEqual((await self.sender.send_delivery(delivery)).status, "committed")
            self.assertEqual(send.await_count, 2)
        self.assertEqual([r["msg_seq"] for r in self.attempts()], [1, 2])
        self.assertEqual(self.fake.token_calls, 2)

    async def test_capture_retries_database_only_and_preserves_single_http(self):
        delivery = self.enqueue()
        real_capture = self.outbox.capture_receipt
        count = 0

        def capture(reservation, body):
            nonlocal count
            count += 1
            if count < 3:
                raise RuntimeError("TEST_PRIVATE_DB_PAYLOAD")
            return real_capture(reservation, body)
        with patch.object(self.outbox, "capture_receipt", side_effect=capture):
            self.assertEqual((await self.sender.send_delivery(delivery)).status, "committed")
        self.assertEqual((count, len(self.fake.received)), (3, 1))

    async def test_failed_capture_pauses_and_ram_recovery_never_resends(self):
        delivery = self.enqueue()
        with patch.object(self.outbox, "capture_receipt", side_effect=RuntimeError("TEST_PRIVATE_DB_BODY")) as capture:
            with self.assertRaisesRegex(SenderUnavailable, "receipt_capture_unavailable"):
                await self.sender.send_delivery(delivery)
            self.assertEqual(capture.call_count, 3)
        with self.assertRaisesRegex(SenderUnavailable, "sender_paused"):
            await self.sender.send_delivery(delivery)
        self.assertEqual(len(self.fake.received), 1)
        self.assertEqual((await self.sender.recover())[0].status, "committed")
        self.assertEqual(len(self.fake.received), 1)
        self.assertFalse(self.sender._unrecorded)

    async def test_finalize_failure_keeps_receipt_and_recovery_is_only_local(self):
        delivery = self.enqueue()
        with patch.object(self.outbox, "finalize", side_effect=RuntimeError("TEST_PRIVATE_SQL")):
            with self.assertRaises(SenderUnavailable):
                await self.sender.send_delivery(delivery)
        self.assertEqual(len(self.query("SELECT * FROM delivery_receipts")), 1)
        self.assertEqual((await self.sender.recover())[0].status, "committed")
        self.assertEqual(len(self.fake.received), 1)

    async def test_lease_loss_after_success_still_captures_but_does_not_finalize(self):
        delivery = self.enqueue()
        original = self.rest.send_message

        async def lose(*args, **kwargs):
            body = await original(*args, **kwargs)
            await self.calls(self.account_lease.close)
            return body
        with patch.object(self.rest, "send_message", side_effect=lose):
            with self.assertRaises(LeaseLost):
                await self.sender.send_delivery(delivery)
        receipts = self.query("SELECT * FROM delivery_receipts")
        self.assertEqual(len(receipts), 1)
        self.assertIsNone(receipts[0]["applied_at"])
        self.assertEqual(self.attempts()[0]["state"], "dispatched")
        self.assertEqual(len(self.fake.received), 1)

    async def test_lost_lease_before_handoff_does_not_invoke_rest(self):
        delivery = self.enqueue()
        original = self.outbox.mark_dispatching

        def lose(*args):
            result = original(*args)
            self.account_lease.close()
            return result
        with patch.object(self.outbox, "mark_dispatching", side_effect=lose), \
                patch.object(self.rest, "send_message", wraps=self.rest.send_message) as send:
            with self.assertRaises(LeaseLost):
                await self.sender.send_delivery(delivery)
            self.assertEqual(send.await_count, 0)

    async def test_cancel_after_handoff_is_ambiguous(self):
        delivery = self.enqueue()
        entered = asyncio.Event()

        async def hang(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(self.rest, "send_message", side_effect=hang) as send:
            work = asyncio.create_task(self.sender.send_delivery(delivery))
            await entered.wait()
            work.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await work
            self.assertEqual(send.await_count, 1)
        self.assertEqual(self.attempts()[0]["state"], "ambiguous")
        self.assertEqual(self.query("SELECT reserved_count FROM qq_official_reply_windows")[0]["reserved_count"], 1)

    async def test_timeout_after_handoff_is_ambiguous(self):
        delivery = self.enqueue()
        self.sender.request_timeout = 0.02
        with patch.object(self.rest, "send_message", side_effect=lambda *a: None):
            # AsyncMock awaits its side effect; use a real awaitable below.
            async def hang(*args, **kwargs):
                await asyncio.Event().wait()
            self.rest.send_message.side_effect = hang
            self.assertEqual((await self.sender.send_delivery(delivery)).status, "ambiguous")
        self.assertEqual(self.attempts()[0]["state"], "ambiguous")

    async def test_cancel_during_prepare_retains_handle_and_releases_unsent_charge(self):
        delivery = self.enqueue()
        entered, release = threading.Event(), threading.Event()
        original = self.outbox.prepare

        def blocked(*args):
            result = original(*args)
            entered.set()
            release.wait(5)
            return result
        with patch.object(self.outbox, "prepare", side_effect=blocked):
            work = asyncio.create_task(self.sender.send_delivery(delivery))
            await self.eventually(entered.is_set)
            work.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await work
        self.assertEqual(self.attempts()[0]["state"], "cancelled_unsent")
        self.assertEqual(self.query("SELECT reserved_count FROM qq_official_reply_windows")[0]["reserved_count"], 0)
        self.assertFalse(self.fake.received)

    async def test_cancel_during_capture_drains_positive_evidence(self):
        delivery = self.enqueue()
        entered, release = threading.Event(), threading.Event()
        original = self.outbox.capture_receipt

        def blocked(*args):
            entered.set()
            release.wait(5)
            return original(*args)
        with patch.object(self.outbox, "capture_receipt", side_effect=blocked):
            work = asyncio.create_task(self.sender.send_delivery(delivery))
            await self.eventually(entered.is_set)
            work.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await work
        self.assertEqual(len(self.query("SELECT * FROM delivery_receipts")), 1)
        self.assertEqual(self.attempts()[0]["state"], "dispatched")
        self.assertEqual((await self.sender.recover())[0].status, "committed")
        self.assertEqual(len(self.fake.received), 1)

    async def test_concurrent_same_delivery_has_one_handoff(self):
        delivery = self.enqueue()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.rest.send_message

        async def blocked(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        with patch.object(self.rest, "send_message", side_effect=blocked) as send:
            first = asyncio.create_task(self.sender.send_delivery(delivery))
            await entered.wait()
            self.assertEqual((await self.sender.send_delivery(delivery)).reason, "local_attempt_inflight")
            release.set()
            self.assertEqual((await first).status, "committed")
            self.assertEqual(send.await_count, 1)

    async def test_gate_wait_does_not_claim_or_block_another_plan(self):
        waiting = self.enqueue("TEST_BACKGROUND", source_native_message_id="TEST_OLD_SOURCE")
        self.query("UPDATE qq_official_endpoints SET proactive_state='allowed' WHERE endpoint_key=?", (self.endpoint.key,))
        fast = self.enqueue("TEST_FOREGROUND")
        gate = self.sender.rate_gate
        waiting_at_gate = asyncio.Event()
        original = gate.acquire

        async def held(endpoint, mode, **kwargs):
            if mode == "proactive":
                waiting_at_gate.set()
                await asyncio.Event().wait()
            return await original(endpoint, mode, **kwargs)
        with patch.object(gate, "acquire", side_effect=held):
            loop = asyncio.create_task(self.sender.run())
            await waiting_at_gate.wait()
            await self.eventually(lambda: self.deliveries.get(fast).status == "committed")
            self.assertEqual(self.deliveries.get(waiting).attempts, 0)
            await self.sender.stop()
            await loop
        self.assertEqual(len(self.fake.received), 1)

    async def test_recovery_called_once_before_new_network(self):
        delivery = self.enqueue()
        with patch.object(self.outbox, "recover", wraps=self.outbox.recover) as recover:
            await self.sender.send_delivery(delivery)
            await self.sender.send_delivery(delivery)
            await self.sender.recover()
        self.assertEqual(recover.call_count, 1)

    async def test_mode_change_after_gate_never_bypasses_proactive_gate(self):
        delivery = self.enqueue()
        original = self.sender.rate_gate.acquire

        async def change(*args, **kwargs):
            permit = await original(*args, **kwargs)
            self.query("UPDATE qq_official_reply_windows SET state='expired'")
            self.query("UPDATE qq_official_endpoints SET proactive_state='allowed'")
            return permit
        with patch.object(self.sender.rate_gate, "acquire", side_effect=change):
            result = await self.sender.send_delivery(delivery)
        self.assertEqual((result.status, result.reason), ("pending", "mode_changed"))
        self.assertFalse(self.attempts())
        self.assertFalse(self.fake.received)

    async def test_server_cooldown_is_reread_without_attempt_or_db_lock(self):
        delivery = self.enqueue()
        real_candidate = self.outbox.candidate
        delays = []

        def candidate(*args):
            result = real_candidate(*args)
            self.query("UPDATE qq_official_endpoints SET passive_blocked_until=?", (self.now + 2,))
            return result

        async def sleep(delay):
            delays.append(delay)
            self.assertFalse(self.attempts())
            self.now += int(delay)
            if len(delays) == 1:
                self.query("UPDATE qq_official_endpoints SET passive_blocked_until=?", (self.now + 2,))
        self.sender = self.make_sender(clock=lambda: float(self.now), sleep=sleep)
        with patch.object(self.outbox, "candidate", side_effect=candidate):
            self.assertEqual((await self.sender.send_delivery(delivery)).status, "committed")
        self.assertEqual(delays, [2, 2])

    async def test_waiting_transport_slot_discards_stale_gate_permit(self):
        first, second = self.enqueue("TEST_ONE"), self.enqueue("TEST_TWO")
        self.sender = self.make_sender(max_inflight=1)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.rest.send_message

        async def blocked(*args, **kwargs):
            if not entered.is_set():
                entered.set()
                await release.wait()
            return await original(*args, **kwargs)
        with patch.object(self.rest, "send_message", side_effect=blocked), \
                patch.object(self.sender.rate_gate, "acquire", wraps=self.sender.rate_gate.acquire) as gate:
            a = asyncio.create_task(self.sender.send_delivery(first))
            await entered.wait()
            b = asyncio.create_task(self.sender.send_delivery(second))
            await self.eventually(lambda: gate.await_count == 2)
            self.assertEqual(len(self.attempts()), 1)
            release.set()
            self.assertEqual([r.status for r in await asyncio.gather(a, b)], ["committed", "committed"])
            self.assertEqual(gate.await_count, 3)

    async def test_cancel_after_dispatch_mark_before_handoff_is_proven_unsent(self):
        delivery = self.enqueue()
        entered, release = threading.Event(), threading.Event()
        original = self.outbox.mark_dispatching

        def blocked(*args):
            result = original(*args)
            entered.set()
            release.wait(5)
            return result
        with patch.object(self.outbox, "mark_dispatching", side_effect=blocked):
            work = asyncio.create_task(self.sender.send_delivery(delivery))
            await self.eventually(entered.is_set)
            work.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await work
        self.assertEqual(self.attempts()[0]["state"], "cancelled_unsent")
        self.assertFalse(self.fake.received)

    async def test_fresh_lease_recovers_durable_receipt_without_network(self):
        delivery = self.enqueue()
        with patch.object(self.outbox, "finalize", side_effect=RuntimeError("TEST_DB_OUTAGE")):
            with self.assertRaises(SenderUnavailable):
                await self.sender.send_delivery(delivery)
        await self.calls(self.account_lease.close)
        self.account_lease = self.lease()
        self.token = await self.calls(self.account_lease.acquire)
        self.sender = self.make_sender()
        with patch.object(self.rest, "send_message", wraps=self.rest.send_message) as send:
            self.assertEqual((await self.sender.recover())[0].status, "committed")
            self.assertEqual(send.await_count, 0)

    async def test_run_sends_split_plan_in_part_order(self):
        rendered = render_plan(MessageBody((TextNode(0, "First part\n\nSecond part\n\nThird part"),)),
            self.endpoint, max_chars=15, max_parts=4)
        ref = self.outbox.enqueue(self.token, rendered=rendered, requester=self.actor,
            logical_key="TEST_PARTS", source_scope_key=self.endpoint.scope.key,
            expires_at=self.now + 200, source_native_message_id="TEST_SOURCE")
        self.assertGreater(len(ref.delivery_ids), 1)
        loop = asyncio.create_task(self.sender.run())
        await self.eventually(lambda: all(self.deliveries.get(i).status == "committed" for i in ref.delivery_ids))
        await self.sender.stop()
        await loop
        attempts = self.attempts()
        self.assertEqual([r["delivery_id"] for r in attempts], list(ref.delivery_ids))
        self.assertEqual([r["msg_seq"] for r in attempts], list(range(1, len(ref.delivery_ids) + 1)))

    def binding_effect(self):
        private = EndpointKey(self.account, "private", "TEST_OWNER")
        actor = ActorKey(self.account, "c2c_user", "", "TEST_OWNER")
        self.provision(private)
        self.window(endpoint=private, source="TEST_OWNER_SOURCE")
        self.fake.add_window("user", "TEST_OWNER", "TEST_OWNER_SOURCE")
        authority = OwnerAuthority(clock=lambda: self.now)
        reducer = BindingReducer(self.outbox.pepper, epoch_guard=assert_epoch, authority=authority, clock=lambda: self.now)
        self.binding = reducer
        with self.tx() as cursor:
            authority.apply_trusted_config(cursor, self.account, ("TEST_OWNER",), "TEST_CONFIG")
            result = reducer.apply(cursor, context=BindingContext(private, actor, "TEST_RECEIPT", self.now),
                command=parse_binding_command("/bind create"), lease_token=self.token)
        self.assertEqual(result.status, "sending")
        return result.effect

    def binding_preview(self):
        issued = self.binding_effect()
        self.query("UPDATE qq_official_endpoints SET binding_state='unbound' WHERE endpoint_key=?", (self.endpoint.key,))
        with self.tx() as cursor:
            self.binding.mark_issued(cursor, account=self.account, challenge_id=issued.challenge_id,
                outcome="success", lease_token=self.token)
            submitted = self.binding.apply(cursor, context=BindingContext(self.endpoint, self.actor, "TEST_SUBMIT", self.now),
                command=parse_binding_command(f"/bind submit {issued.challenge_id} {issued.code}"), lease_token=self.token)
            self.assertEqual(submitted.status, "pending")
            owner = ActorKey(self.account, "c2c_user", "", "TEST_OWNER")
            status = self.binding.apply(cursor, context=BindingContext(issued.endpoint, owner, "TEST_STATUS", self.now),
                command=parse_binding_command(f"/bind status {issued.challenge_id}"), lease_token=self.token)
        return status.effect

    async def test_owner_preview_uses_stored_candidate_and_displays_confirmation_context(self):
        effect = self.binding_preview()
        # Tampered transient display hints do not replace the sealed DB identity.
        effect = replace(effect, actual_group=EndpointKey(self.account, "group", "TEST_WRONG_GROUP"),
                         actual_submitter=ActorKey(self.account, "group_member", "TEST_WRONG_GROUP", "TEST_WRONG_MEMBER"))
        self.assertEqual(await self.sender.emit_binding(effect), "success")
        content = self.fake.received[0]["payload"]["content"]
        for expected in ("TEST_GROUP", "TEST_MEMBER", effect.candidate_id, f"/bind confirm {effect.candidate_id} 1", "默认关闭"):
            self.assertIn(expected, content)
        self.assertNotIn("TEST_WRONG", content)
        manifest = json.loads(self.query("SELECT manifest_json FROM delivery_plans")[0]["manifest_json"])
        self.assertEqual(manifest["control"]["candidate_id"], effect.candidate_id)

    async def test_owner_preview_revoked_after_enqueue_is_not_sent(self):
        effect = self.binding_preview()
        original = self.outbox.candidate

        def revoke(*args):
            self.query("UPDATE qq_official_binding_candidates SET state='rejected' WHERE candidate_id=?", (effect.candidate_id,))
            return original(*args)
        with patch.object(self.outbox, "candidate", side_effect=revoke):
            self.assertEqual(await self.sender.emit_binding(effect), "failed")
        self.assertFalse(self.fake.received)

    async def test_preview_wrong_version_and_revoked_grant_are_rejected(self):
        effect = self.binding_preview()
        self.assertEqual(await self.sender.emit_binding(replace(effect, candidate_version=2)), "failed")
        self.query("UPDATE qq_official_owner_grants SET state='revoked'")
        self.assertEqual(await self.sender.emit_binding(effect), "failed")
        self.assertFalse(self.fake.received)

    async def test_otp_uses_same_gate_real_issuer_window_and_never_persists_code(self):
        effect = self.binding_effect()
        with patch.object(self.sender.rate_gate, "acquire", wraps=self.sender.rate_gate.acquire) as gate:
            self.assertEqual(await self.sender.emit_binding(effect), "success")
            self.assertEqual(gate.await_count, 1)
        received = self.fake.received[0]
        self.assertEqual((received["kind"], received["target"]), ("user", "TEST_OWNER"))
        self.assertEqual(received["payload"]["msg_id"], "TEST_OWNER_SOURCE")
        self.assertIn(effect.code, received["payload"]["content"])
        for table in ("delivery_plans", "deliveries", "delivery_attempts", "delivery_receipts", "messages"):
            stored = json.dumps([dict(r) for r in self.query("SELECT * FROM " + table)], default=str)
            self.assertNotIn(effect.code, stored, table)
        self.assertEqual(await self.sender.emit_binding(effect), "failed")
        self.assertEqual(len(self.fake.received), 1)

    async def _assert_otp_rejection(self, status, code):
        effect = self.binding_effect()
        self.fake.next_errors.append((status, code))
        with patch.object(self.rest, "send_message", wraps=self.rest.send_message) as send:
            self.assertEqual(await self.sender.emit_binding(effect), "failed")
            self.assertEqual(await self.sender.emit_binding(effect), "failed")
            self.assertEqual(send.await_count, 1)

    async def test_otp_auth_rejection_never_retries(self):
        await self._assert_otp_rejection(401, 0)

    async def test_otp_format_rejection_never_retries(self):
        await self._assert_otp_rejection(400, 50037)

    async def test_otp_expired_unknown_or_wrong_endpoint_has_no_http(self):
        effect = self.binding_effect()
        with patch.object(self.rest, "send_message", wraps=self.rest.send_message) as send:
            self.assertEqual(await self.sender.emit_binding(replace(effect, challenge_id="BC-" + "f" * 32)), "failed")
            self.assertEqual(await self.sender.emit_binding(replace(effect, endpoint=self.endpoint)), "failed")
            self.now += 601
            self.assertEqual(await self.sender.emit_binding(effect), "failed")
            self.assertEqual(send.await_count, 0)

    async def test_otp_no_window_never_uses_proactive_even_if_allowed(self):
        effect = self.binding_effect()
        self.query("UPDATE qq_official_reply_windows SET state='expired' WHERE endpoint_key=?", (effect.endpoint.key,))
        self.query("UPDATE qq_official_endpoints SET proactive_state='allowed' WHERE endpoint_key=?", (effect.endpoint.key,))
        self.assertEqual(await self.sender.emit_binding(effect), "failed")
        self.assertFalse(self.attempts())
        self.assertFalse(self.fake.received)

    async def test_transport_account_mismatch_fails_before_io(self):
        with patch.object(self.tokens, "credentials", Credentials("TEST_OTHER_APP", "TEST_OTHER_SECRET")):
            with self.assertRaisesRegex(SenderUnavailable, "transport_account_mismatch"):
                self.make_sender()
        self.assertFalse(self.fake.received)

    async def test_wall_clock_rollback_during_token_preflight_does_not_extend_deadline(self):
        delivery = self.enqueue(expires_at=self.now + 5)
        monotonic = [10.0]
        self.sender = self.make_sender(clock=lambda: monotonic[0])
        original = self.tokens.get

        async def late():
            token = await original()
            monotonic[0] += 6
            self.now -= 100
            return token
        with patch.object(self.tokens, "get", side_effect=late):
            self.assertEqual((await self.sender.send_delivery(delivery)).reason, "send_deadline_expired")
        self.assertFalse(self.attempts())
        self.assertFalse(self.fake.received)

    async def test_slow_shared_token_refresh_precedes_rate_admission(self):
        first, second = self.enqueue("TEST_REFRESH_ONE"), self.enqueue("TEST_REFRESH_TWO")
        release, entered = asyncio.Event(), asyncio.Event()
        count = 0
        original = self.tokens.get

        async def blocked():
            nonlocal count
            count += 1
            if count >= 2:
                entered.set()
            await release.wait()
            return await original()
        with patch.object(self.tokens, "get", side_effect=blocked), \
                patch.object(self.sender.rate_gate, "acquire", wraps=self.sender.rate_gate.acquire) as gate:
            works = [asyncio.create_task(self.sender.send_delivery(i)) for i in (first, second)]
            await entered.wait()
            # No permits, transport slots, sequences or charges accumulate while
            # both real callers wait on the same simulated token refresh.
            self.assertEqual(gate.await_count, 0)
            self.assertFalse(self.attempts())
            release.set()
            self.assertEqual([r.status for r in await asyncio.gather(*works)], ["committed", "committed"])
            self.assertEqual(gate.await_count, 2)

    async def test_expiry_during_rate_wait_cancels_unsent_then_reauthenticates_before_new_gate(self):
        delivery = self.enqueue()
        token_time = [100.0]
        self.tokens._clock = lambda: token_time[0]
        acquire = self.sender.rate_gate.acquire

        async def expire(*args, **kwargs):
            permit = await acquire(*args, **kwargs)
            token_time[0] = self.tokens.expires_at
            return permit
        with patch.object(self.sender.rate_gate, "acquire", side_effect=expire), \
                patch.object(self.rest, "send_message", wraps=self.rest.send_message) as send:
            result = await self.sender.send_delivery(delivery)
            self.assertEqual(result.status, "pending")
            self.assertEqual(send.await_count, 0)
        self.assertEqual(self.attempts()[0]["state"], "cancelled_unsent")
        self.assertEqual(self.query("SELECT auth_retries_used FROM deliveries WHERE delivery_id=?", (delivery,))[0]["auth_retries_used"], 0)
        self.assertEqual(self.fake.token_calls, 1)
        self.now += 1
        self.fake.access_token = "TEST_FRESH_TOKEN_AFTER_RATE_WAIT"
        with patch.object(self.sender.rate_gate, "acquire", wraps=acquire) as gate:
            self.assertEqual((await self.sender.send_delivery(delivery)).status, "committed")
            self.assertEqual(gate.await_count, 1)
        self.assertEqual(self.fake.token_calls, 2)
        self.assertEqual(len(self.fake.received), 1)

    async def test_prepared_wire_path_does_not_refresh_even_when_get_would_block(self):
        delivery = self.enqueue()
        get = self.tokens.get
        calls = 0

        async def only_preflight():
            nonlocal calls
            calls += 1
            if calls > 1:
                await asyncio.Event().wait()
            return await get()
        with patch.object(self.tokens, "get", side_effect=only_preflight):
            async with asyncio.timeout(5):
                self.assertEqual((await self.sender.send_delivery(delivery)).status, "committed")
        self.assertEqual(calls, 1)

    async def test_full_waiter_capacity_yields_to_higher_priority_unclaimed_reply(self):
        waiting = self.enqueue("TEST_BACKGROUND_CAPACITY", purpose="background_result", source_native_message_id="TEST_OLD")
        self.query("UPDATE qq_official_endpoints SET proactive_state='allowed' WHERE endpoint_key=?", (self.endpoint.key,))
        self.sender = self.make_sender(max_inflight=1, max_waiters=1)
        entered = asyncio.Event()
        original = self.sender.rate_gate.acquire

        async def blocked(endpoint, mode, **kwargs):
            if mode == "proactive":
                entered.set()
                await asyncio.Event().wait()
            return await original(endpoint, mode, **kwargs)
        with patch.object(self.sender.rate_gate, "acquire", side_effect=blocked):
            run = asyncio.create_task(self.sender.run())
            await entered.wait()
            foreground = self.enqueue("TEST_URGENT_REPLY")
            self.sender.wake()
            await self.eventually(lambda: self.deliveries.get(foreground).status == "committed")
            self.assertEqual(self.deliveries.get(waiting).attempts, 0)
            self.assertLessEqual(len(self.sender._tasks), 1)
            await self.sender.stop()
            await run
        self.assertFalse(self.sender._tasks)
        self.assertFalse(self.sender._active)
        self.assertFalse(self.sender._rate_waiters)

    async def test_last_instant_prepared_expiry_is_proven_unsent_not_account_pause(self):
        delivery = self.enqueue()
        token_time = [100.0]
        self.tokens._clock = lambda: token_time[0]
        original = self.rest.prepared_is_current

        def expire_after_check(prepared):
            result = original(prepared)
            token_time[0] = self.tokens.expires_at
            return result
        with patch.object(self.rest, "prepared_is_current", side_effect=expire_after_check):
            result = await self.sender.send_delivery(delivery)
        self.assertEqual(result.status, "pending")
        self.assertEqual(result.reason, "prepared_auth_stale")
        self.assertFalse(self.fake.received)
        attempt = self.attempts()[0]
        self.assertEqual((attempt["state"], attempt["acceptance"]), ("cancelled_unsent", "confirmed_no"))
        self.assertEqual(self.query("SELECT reserved_count FROM qq_official_reply_windows")[0]["reserved_count"], 0)
        self.assertNotEqual(self.query("SELECT gateway_state FROM qq_official_accounts")[0]["gateway_state"], "paused")
        self.assertEqual(self.query("SELECT auth_retries_used FROM deliveries WHERE delivery_id=?", (delivery,))[0]["auth_retries_used"], 0)
        self.now += 1
        self.fake.access_token = "TEST_REFRESHED_AFTER_HANDOFF_EXPIRY"
        self.assertEqual((await self.sender.send_delivery(delivery)).status, "committed")
        self.assertEqual(len(self.fake.received), 1)

    async def test_otp_last_instant_prepared_expiry_is_terminal_without_http(self):
        effect = self.binding_effect()
        token_time = [100.0]
        self.tokens._clock = lambda: token_time[0]
        original = self.rest.prepared_is_current

        def expire_after_check(prepared):
            result = original(prepared)
            token_time[0] = self.tokens.expires_at
            return result
        with patch.object(self.rest, "prepared_is_current", side_effect=expire_after_check):
            self.assertEqual(await self.sender.emit_binding(effect), "failed")
        self.assertEqual(await self.sender.emit_binding(effect), "failed")
        self.assertFalse(self.fake.received)
        self.assertEqual(self.attempts()[0]["state"], "cancelled_unsent")
        self.assertEqual(self.query("SELECT status FROM deliveries")[0]["status"], "failed")

    async def test_priority_preemption_never_cancels_an_existing_http_attempt(self):
        background = self.enqueue("TEST_INFLIGHT_BACKGROUND", purpose="background_result")
        self.sender = self.make_sender(max_inflight=1, max_waiters=1)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.rest.send_message

        async def hold(*args, **kwargs):
            if not entered.is_set():
                entered.set()
                await release.wait()
            return await original(*args, **kwargs)
        with patch.object(self.rest, "send_message", side_effect=hold) as send:
            run = asyncio.create_task(self.sender.run())
            await entered.wait()
            foreground = self.enqueue("TEST_NEW_FOREGROUND")
            self.sender.wake()
            await asyncio.sleep(0.05)
            self.assertEqual(send.await_count, 1)
            self.assertEqual(self.attempts()[0]["state"], "dispatched")
            release.set()
            await self.eventually(lambda: self.deliveries.get(foreground).status == "committed")
            self.assertEqual(self.deliveries.get(background).status, "committed")
            await self.sender.stop()
            await run


if __name__ == "__main__":
    unittest.main()
