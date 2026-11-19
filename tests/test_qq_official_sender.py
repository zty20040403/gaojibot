"""Real PostgreSQL + explicitly fake loopback transport, never a live QQ account."""
from __future__ import annotations

import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official import sender as sender_module
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.lease import LeaseLost, assert_epoch
from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.adapters.qq_official.sender import OfficialSender
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
        self.outbox = OfficialSendStore(self.deliveries, admission=lambda c, e, a: True,
            pepper=b"TEST_PROTECTED_PEPPER" * 3, clock=lambda: self.now)
        with self.tx() as cursor:
            observe_ready(cursor, self.token, {"op": 0, "t": "READY", "d": {
                "session_id": "TEST_SESSION", "user": {"id": "TEST_REAL_BOT", "bot": True}}}, self.now)
        self.inbound()
        self.query("UPDATE qq_official_endpoints SET proactive_state='unknown'")
        self.fake = FakePlatform(clock=lambda: self.now)
        self.fake.app_id = self.account.app_id
        await self.fake.start()
        self.fake.add_window("group", self.endpoint.native_conversation_id, "TEST_SOURCE")
        self.fake.proactive_allowed[('group', self.endpoint.native_conversation_id)] = True
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
            wall_clock=lambda: self.now, poll_seconds=0.01, sleep=self.fast_sleep, **kwargs)

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

    async def fast_sleep(self, seconds):
        self.now += int(seconds) or 1
        await asyncio.sleep(0)

    def inbound(self, endpoint=None, message='TEST_SOURCE'):
        endpoint = endpoint or self.endpoint
        import secrets
        self.query("""INSERT INTO qq_official_inbound_events
            (inbox_id,account_key,endpoint_key,endpoint_generation,route_epoch,event_type,native_message_id,
             status,echo_state,received_at,created_at,updated_at)
            VALUES (?,?,?,1,1,?,?,'processed','not_echo',?,?,?)""",
            (secrets.token_hex(16), self.account.key, endpoint.key,
             'GROUP_AT_MESSAGE_CREATE' if endpoint.kind=='group' else 'C2C_MESSAGE_CREATE', message,
             self.now,self.now,self.now))

    async def test_health_tracks_real_sender_start_delivery_and_shutdown(self):
        from prometheus_client import CollectorRegistry
        from src.plugins.ai_chat.adapters.qq_official.observability import OfficialObservability
        runtime = SimpleNamespace(config=SimpleNamespace(enabled=True), _token=self.token,
            _stopping=False, store=SimpleNamespace(transaction=self.tx), _calls=self.calls,
            _gateway=SimpleNamespace(health={'state':'ready'}), sender=self.sender)
        health = OfficialObservability(SimpleNamespace(official_runtime=runtime),
            registry=CollectorRegistry(), clock=lambda: self.now)
        runner = None
        try:
            self.assertFalse((await health.sample())['healthy'])
            delivery = self.enqueue()
            runner = asyncio.create_task(self.sender.run())
            await self.eventually(lambda: self.sender._running)
            self.assertTrue((await health.sample())['healthy'])
            outcome = await self.sender.send_delivery(delivery)
            self.assertEqual(outcome.status, 'committed')
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
            self.assertEqual((await health.sample())['reason'], 'sender_unavailable')
        finally:
            if runner is not None:
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
            await health.close()

    async def test_repeated_cancellation_drains_accepted_receipt_through_finalization(self):
        entered,release = threading.Event(),threading.Event()
        original = self.outbox.capture_receipt
        def capture(reservation, body):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('TEST receipt barrier timed out')
            return original(reservation,body)
        delivery = self.enqueue()
        with patch.object(self.outbox,'capture_receipt',side_effect=capture):
            pending = asyncio.create_task(self.sender.send_delivery(delivery))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait,3))
                worker = self.sender._tasks[delivery]
                worker.cancel()
                await asyncio.sleep(0)  # First cancellation enters the receipt drain.
                worker.cancel()  # Runtime shutdown can cancel that drain again.
                await asyncio.sleep(0)
                release.set()
                await asyncio.gather(worker,pending,return_exceptions=True)
            finally:
                release.set()
                await self.sender.stop()
                await asyncio.gather(pending,return_exceptions=True)
        self.assertEqual(len(self.fake.received),1)
        receipts = self.query('SELECT applied_at FROM delivery_receipts')
        self.assertEqual(len(receipts),1)
        self.assertIsNotNone(receipts[0]['applied_at'])
        self.assertEqual(len(self.query("SELECT * FROM messages WHERE direction='outbound'")),1)
        self.sender = self.make_sender()
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertEqual(len(self.fake.received),1)

    async def test_default_proactive_delivers_after_five_minutes_and_records_reference(self):
        self.now += 400
        delivery = self.enqueue()
        result = await self.sender.send_delivery(delivery)
        self.assertEqual(result.status, 'committed')
        self.assertNotIn('msg_id', self.fake.received[0]['payload'])
        self.assertEqual(self.attempts()[0]['send_mode'], 'proactive')
        self.assertEqual(self.query('SELECT proactive_state FROM qq_official_endpoints WHERE endpoint_key=?',
            (self.endpoint.key,))[0]['proactive_state'], 'allowed')
        self.assertEqual(len(self.query("SELECT * FROM messages WHERE direction='outbound'")), 1)
        self.assertEqual(len(self.query('SELECT * FROM qq_official_message_indexes')), 1)
        self.assertEqual(self.query('SELECT * FROM qq_official_reply_windows'), [])

    async def test_connection_failure_falls_back_or_retries_once(self):
        original = self.rest.send_message
        calls = 0

        async def flaky_markdown(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise QQAPIError('connection_failed', accepted=False, stage='send')
            return await original(*args, **kwargs)

        with patch.object(self.rest, 'send_message', side_effect=flaky_markdown):
            result = await self.sender.send_delivery(self.enqueue('TEST_CONN_MD'))
        self.assertEqual(result.status, 'committed')
        self.assertEqual(self.fake.received[0]['payload']['msg_type'], 0)

        plain_id = self.outbox.enqueue(self.token,
            rendered=render_plan(MessageBody((TextNode(0, 'TEST plain'),)), self.endpoint, markdown=False),
            requester=self.actor, logical_key='TEST_CONN_PLAIN', source_scope_key=self.endpoint.scope.key,
            expires_at=self.now + 200, source_native_message_id='TEST_SOURCE').delivery_ids[0]
        with patch.object(self.rest, 'send_message', side_effect=QQAPIError('connection_failed', accepted=False, stage='send')):
            failed = await self.sender.send_delivery(plain_id)
        self.assertEqual((failed.status, failed.reason), ('failed', 'connection_failed'))

    async def test_proactive_rejection_uses_latest_inbound_and_group_five_replies(self):
        self.fake.proactive_allowed[('group', self.endpoint.native_conversation_id)] = False
        self.inbound(message='TEST_NEWEST')
        self.fake.add_window('group', self.endpoint.native_conversation_id, 'TEST_NEWEST')
        for i in range(5):
            result = await self.sender.send_delivery(self.enqueue(str(i)))
            self.assertEqual(result.status, 'committed')
        result = await self.sender.send_delivery(self.enqueue('sixth'))
        self.assertEqual((result.status,result.reason), ('failed','proactive_disabled_no_reply_window'))
        sent = [r['payload'] for r in self.fake.received]
        self.assertEqual([r['msg_seq'] for r in sent], [1,2,3,4,5])
        self.assertEqual({r['msg_id'] for r in sent}, {'TEST_NEWEST'})
        self.assertEqual(self.query("SELECT * FROM deliveries WHERE status='pending'"), [])

    async def test_private_has_four_replies_and_five_minute_expiry(self):
        endpoint = EndpointKey(self.account, 'private', 'TEST_PRIVATE')
        actor = ActorKey(self.account, 'c2c_user', '', 'TEST_PRIVATE')
        self.provision(endpoint); self.inbound(endpoint)
        self.fake.add_window('user','TEST_PRIVATE','TEST_SOURCE')
        for i in range(4):
            result = await self.sender.send_delivery(self.enqueue('private'+str(i),endpoint=endpoint,actor=actor))
            self.assertEqual(result.status,'committed')
        result = await self.sender.send_delivery(self.enqueue('private-fifth',endpoint=endpoint,actor=actor))
        self.assertEqual(result.status,'failed')
        self.now += 300
        self.query("UPDATE qq_official_endpoints SET proactive_state='denied'")
        result = await self.sender.send_delivery(self.enqueue('expired'))
        self.assertEqual(result.reason,'proactive_disabled_no_reply_window')

    async def test_markdown_rejection_falls_back_once_but_content_rejection_does_not(self):
        self.fake.next_errors.append((400,50037))
        result = await self.sender.send_delivery(self.enqueue())
        self.assertEqual(result.status,'committed')
        self.assertEqual(self.fake.received[0]['payload']['msg_type'],0)
        self.assertEqual(len(self.attempts()),2)
        self.fake.next_errors.append((400,40034006))
        result = await self.sender.send_delivery(self.enqueue('rejected'))
        self.assertEqual((result.status,result.reason),('failed','content_rejected'))
        self.assertEqual(len(self.fake.received),1)

    async def test_response_loss_is_ambiguous_and_never_replayed(self):
        self.fake.drop_next_response = True
        delivery = self.enqueue()
        result = await self.sender.send_delivery(delivery)
        self.assertEqual(result.status,'ambiguous')
        self.assertEqual(result.reason,'possibly_not_sent')
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'ambiguous')
        self.assertEqual(len(self.fake.received),1)

    async def test_auth_refresh_once_and_rate_wait_retry(self):
        # The fake rotates the rejected token, like Tencent's refresh endpoint.
        original = self.rest.send_message
        first = True
        async def unauthorized(*args,**kwargs):
            nonlocal first
            if first:
                first=False
                self.fake.access_token='TEST_REFRESHED_TOKEN'
                raise QQAPIError('authentication',accepted=False,status=401)
            return await original(*args,**kwargs)
        with patch.object(self.rest,'send_message',side_effect=unauthorized):
            result=await self.sender.send_delivery(self.enqueue())
        self.assertEqual(result.status,'committed')
        self.assertEqual(self.fake.token_calls,2)
        self.fake.next_errors.append((429,50002))
        result=await self.sender.send_delivery(self.enqueue('rate'))
        self.assertEqual(result.status,'committed')
        self.assertEqual(len(self.attempts()),4)

    async def test_same_endpoint_serializes_while_other_endpoint_can_send(self):
        gate=asyncio.Event();entered=asyncio.Event();order=[]
        original=self.rest.send_message
        async def slow(kind,target,payload,**kwargs):
            order.append(target)
            if len(order)==1:
                entered.set();await gate.wait()
            return await original(kind,target,payload,**kwargs)
        other=EndpointKey(self.account,'group','TEST_OTHER')
        self.provision(other)
        self.query("UPDATE qq_official_endpoints SET proactive_state='unknown'")
        self.fake.proactive_allowed[('group','TEST_OTHER')]=True
        actor=ActorKey(self.account,'group_member','TEST_OTHER','TEST_MEMBER')
        with patch.object(self.rest,'send_message',side_effect=slow):
            first=asyncio.create_task(self.sender.send_delivery(self.enqueue('first')))
            await asyncio.wait_for(entered.wait(),5)
            second=asyncio.create_task(self.sender.send_delivery(self.enqueue('second')))
            independent=await asyncio.wait_for(self.sender.send_delivery(self.enqueue('other',endpoint=other,actor=actor)),5)
            self.assertEqual(independent.status,'committed')
            self.assertFalse(second.done())
            gate.set()
            self.assertEqual([r.status for r in await asyncio.gather(first,second)],['committed','committed'])
        self.assertEqual(order,['TEST_GROUP','TEST_OTHER','TEST_GROUP'])

    async def test_full_sender_cannot_overtake_an_earlier_durable_row(self):
        self.enterContext(patch.object(sender_module, '_SEND_SLOTS', threading.BoundedSemaphore(1)))
        self.enterContext(patch.object(sender_module, '_WAIT_SLOTS', threading.BoundedSemaphore(3)))
        peer = self.make_sender()
        self.addAsyncCleanup(peer.stop)
        await peer.recover()
        active, earlier, later = (self.enqueue(key) for key in ('TEST_ACTIVE', 'TEST_EARLY', 'TEST_LATE'))
        entered, release = asyncio.Event(), asyncio.Event()
        send = self.rest.send_message

        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return await send(*args, **kwargs)

        with (
            patch.object(self.rest, 'send_message', side_effect=held),
            patch.object(peer, '_submit', wraps=peer._submit) as submit,
            patch.object(peer, '_deliver', wraps=peer._deliver) as deliver,
        ):
            first, duplicate = (asyncio.create_task(self.sender.send_delivery(active)) for _ in range(2))
            await asyncio.wait_for(entered.wait(), 5)
            late = asyncio.create_task(peer.send_delivery(later))
            await self.eventually(lambda: submit.called)
            with self.assertRaises(sender_module.SenderUnavailable):
                await peer.send_delivery(earlier)
            self.assertFalse(peer._tasks)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            release.set()
            self.assertEqual((await asyncio.wait_for(duplicate, 5)).status, 'committed')
            await self.eventually(lambda: deliver.await_count and not peer._tasks)
            self.assertFalse(late.done())
            self.assertEqual([row['delivery_id'] for row in self.attempts()], [active])
            self.query('UPDATE deliveries SET next_attempt_at=? WHERE delivery_id=?', (self.now + 1, earlier))
            self.assertEqual(self.outbox.list_due(self.token), ())
            self.now += 1
            runner = asyncio.create_task(peer.run())
            self.assertEqual((await asyncio.wait_for(late, 5)).status, 'committed')
        await peer.stop()
        await asyncio.wait_for(runner, 5)
        self.assertEqual([row['delivery_id'] for row in self.attempts()], [active, earlier, later])
        self.assertEqual(len(self.fake.received), 3)

    async def test_lease_loss_wakes_waiters_and_recovery_never_replays_unknown(self):
        self.enterContext(patch.object(sender_module, '_SEND_SLOTS', threading.BoundedSemaphore(1)))
        active, spill = self.enqueue('TEST_ACTIVE'), self.enqueue('TEST_SPILL')
        entered = asyncio.Event()

        async def interrupted(*args, **kwargs):
            entered.set()
            await asyncio.Future()

        self.sender.poll_seconds = 60
        with patch.object(self.rest, 'send_message', side_effect=interrupted), patch.object(
            self.sender._stopped, 'wait', wraps=self.sender._stopped.wait,
        ) as parked:
            caller = asyncio.create_task(self.sender.send_delivery(active))
            await asyncio.wait_for(entered.wait(), 5)
            blocked = asyncio.create_task(self.sender.send_delivery(spill))
            await self.eventually(lambda: parked.await_count)
            await self.calls(self.account_lease.close)
            with self.assertRaises(LeaseLost):
                await self.sender._fence()
            with self.assertRaises(LeaseLost):
                await asyncio.wait_for(blocked, 5)
            await self.sender.stop()
            self.assertIsInstance((await asyncio.gather(caller, return_exceptions=True))[0], (LeaseLost, asyncio.CancelledError))
        self.assertFalse(self.sender._tasks)
        self.assertEqual(self.outbox.get(self.token, spill)['status'], 'pending')
        self.token = self.lease().acquire()
        self.sender = self.make_sender()
        self.assertEqual((await self.sender.send_delivery(active)).status, 'ambiguous')
        self.assertEqual((await self.sender.send_delivery(spill)).status, 'committed')
        self.assertEqual(len(self.fake.received), 1)

    async def test_unexpected_errors_are_visible_and_do_not_repeat_a_send(self):
        before = self.enqueue('TEST_BEFORE_SEND')
        with patch.object(self.rest, 'prepare_send', side_effect=RuntimeError('TEST_PREPARE')):
            with self.assertRaisesRegex(RuntimeError, 'TEST_PREPARE'):
                await self.sender.send_delivery(before)
        self.assertEqual((await self.sender.send_delivery(before)).status, 'failed')
        self.assertFalse(self.attempts())

        send = self.rest.send_message

        async def lost_result(*args, **kwargs):
            await send(*args, **kwargs)
            raise RuntimeError('TEST_POST')

        during = self.enqueue('TEST_DURING_SEND')
        with patch.object(self.rest, 'send_message', side_effect=lost_result):
            with self.assertRaisesRegex(RuntimeError, 'TEST_POST'):
                await self.sender.send_delivery(during)
        self.assertEqual((await self.sender.send_delivery(during)).status, 'ambiguous')
        self.assertEqual(self.attempts()[0]['acceptance'], 'unknown')
        self.assertFalse(self.query('SELECT * FROM delivery_receipts'))

        for stage in ('capture_receipt', 'finalize'):
            with self.subTest(stage=stage):
                after = self.enqueue(stage)
                with patch.object(self.outbox, stage, side_effect=RuntimeError('TEST_RECEIPT')):
                    with self.assertRaisesRegex(RuntimeError, 'TEST_RECEIPT'):
                        await self.sender.send_delivery(after)
                self.assertEqual(self.outbox.get(self.token, after)['status'], 'sending')
                self.assertEqual(len(self.query('SELECT 1 FROM delivery_receipts WHERE applied_at IS NULL')), stage == 'finalize')
                self.assertEqual(self.outbox.list_due(self.token), ())
                self.assertEqual((await self.sender.send_delivery(after)).status, 'committed')
        self.assertEqual((len(self.attempts()), len(self.fake.received)), (3, 3))

    async def test_pump_stops_if_error_settlement_cannot_be_persisted(self):
        delivery = self.enqueue()
        with (
            patch.object(self.rest, 'prepare_send', side_effect=RuntimeError('TEST_SEND')) as prepare,
            patch.object(self.outbox, 'settle_send_error', side_effect=OSError('TEST_STORAGE')),
            self.assertLogs(sender_module.__name__, 'ERROR') as logs,
        ):
            await asyncio.wait_for(self.sender.run(), 5)
            await self.eventually(lambda: not self.sender._tasks)
        prepare.assert_awaited_once()
        self.assertIn('settlement failed', logs.output[0])
        self.assertIn('RuntimeError', logs.output[1])
        self.assertEqual(self.outbox.get(self.token, delivery)['status'], 'pending')
        self.assertFalse(self.attempts())
