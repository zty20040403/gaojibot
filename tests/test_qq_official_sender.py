"""Real PostgreSQL + explicitly fake loopback transport, never a live QQ account."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
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

    async def test_default_proactive_delivers_after_five_minutes_and_records_reference(self):
        self.now += 400
        delivery = self.enqueue()
        result = await self.sender.send_delivery(delivery)
        self.assertEqual(result.status, 'committed')
        self.assertNotIn('msg_id', self.fake.received[0]['payload'])
        self.assertEqual(self.attempts()[0]['send_mode'], 'proactive')
        self.assertEqual(len(self.query("SELECT * FROM messages WHERE direction='outbound'")), 1)
        self.assertEqual(len(self.query('SELECT * FROM qq_official_message_indexes')), 1)
        self.assertEqual(self.query('SELECT * FROM qq_official_quota_periods'), [])
        self.assertEqual(self.query('SELECT * FROM qq_official_reply_windows'), [])

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
                self.tokens.invalidate()
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
