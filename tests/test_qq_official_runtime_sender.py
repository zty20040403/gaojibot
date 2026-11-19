"""Real official runtime, loopback platform and durable send integration.

No OneBot Bot or model is created. All credentials are synthetic TEST values.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import re
import threading
import time
import unittest
from unittest.mock import patch

from aiohttp import web

from tests import test_qq_official_runtime as runtime_fixture
from src.plugins.ai_chat.adapters.qq_official.lease import DedicatedAccountLease
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.plugins.ai_chat.turn_journal import TurnJournal
from src.qq_official.testing.fake_platform import FakePlatform


@asynccontextmanager
async def verified_ready_platform():
    """Extend only this fixture's wire READY to the complete bot contract.

    Frozen M0 FakePlatform omits user.bot and has no /users/@me route. Keep that
    incomplete fixture intact for the negative test below; do not fake a stored
    identity or bypass the runtime's authenticated READY observer.
    """
    original = web.WebSocketResponse.send_json

    async def send_json(socket, data, *args, **kwargs):
        if isinstance(data, dict) and data.get("t") == "READY":
            data = {**data, "d": {**data["d"], "user": {**data["d"]["user"], "bot": True}}}
        return await original(socket, data, *args, **kwargs)

    with patch.object(web.WebSocketResponse, "send_json", new=send_json):
        async with FakePlatform() as fake:
            yield fake


class OfficialRuntimeSenderIntegrationTests(unittest.IsolatedAsyncioTestCase):
    setUp = runtime_fixture.OfficialRuntimeTests.setUp
    runtime = runtime_fixture.OfficialRuntimeTests.runtime
    query = runtime_fixture.OfficialRuntimeTests.query
    wait_db = runtime_fixture.OfficialRuntimeTests.wait_db

    async def eventually(self, predicate):
        async with asyncio.timeout(8):
            while not predicate():
                await asyncio.sleep(0.01)

    async def open_runtime(self, fake, **kwargs):
        loaded = runtime_fixture.loaded_config(fake.app_id, base=fake.base_url)
        runtime = self.runtime(loaded=loaded, **kwargs)
        self.assertTrue(await runtime.start())
        try:
            await asyncio.wait_for(fake.wait_ready(), 5)
        except TimeoutError:
            self.fail(f'gateway not ready: {runtime.status()} {self.logger.mock_calls}')
        await self.wait_db("SELECT bot_native_id FROM qq_official_accounts WHERE bot_identity_source='ready'")
        self.assertIsNotNone(runtime.sender)
        return runtime

    async def incoming(self, fake, native="TEST_RUNTIME_INPUT", content="A synthetic user request"):
        await fake.emit("C2C_MESSAGE_CREATE", {
            "id": native, "author": {"user_openid": "TEST_RUNTIME_OWNER"},
            "content": content, "timestamp": datetime.now(timezone.utc).isoformat(),
            "message_scene": {"ext": {"msg_idx": "TEST_SOURCE_INDEX"}},
        })
        return native

    async def enqueue(self, runtime, source, *, text="TEST final answer", max_chars=2000, turn_id=None):
        endpoint = EndpointKey(runtime.loaded.account, "private", "TEST_RUNTIME_OWNER")
        actor = ActorKey(runtime.loaded.account, "c2c_user", "", "TEST_RUNTIME_OWNER")
        rendered = render_plan(MessageBody((TextNode(0, text),)), endpoint, max_chars=max_chars)
        plan = await runtime._calls(runtime.outbox.enqueue, runtime._token, rendered=rendered,
            requester=actor, logical_key="TEST_RUNTIME_PLAN", source_scope_key=endpoint.scope.key,
            source_native_message_id=source, expires_at=int(time.time()) + 120, turn_id=turn_id)
        runtime.sender.wake()
        return plan

    async def test_ready_connects_auto_sender_with_shared_ledger_and_journal(self):
        ledger, journal = MessageLedger(self.database), TurnJournal(self.database)
        self.addCleanup(ledger.close)
        self.addCleanup(journal.close)
        async with verified_ready_platform() as fake:
            runtime = await self.open_runtime(fake, ledger=ledger, journal=journal)
            self.assertFalse(await runtime.start())
            self.assertEqual(fake.received, [])
            self.assertIs(runtime.sender.ledger, ledger)
            self.assertIs(runtime.sender.journal, journal)
            self.assertFalse(runtime.status()["turn_execution_connected"])
            self.assertTrue(runtime.status()["durable_sender_connected"])
            source = await self.incoming(fake)
            inbound = (await self.wait_db("""SELECT canonical_message_id,dispatch_job_id FROM qq_official_inbound_events
                WHERE native_message_id=? AND status='processed'""", (source,)))[0]
            endpoint = EndpointKey(runtime.loaded.account, "private", "TEST_RUNTIME_OWNER")
            turn = await runtime._calls(journal.start_turn, endpoint.scope,
                trigger_canonical_message_id=inbound["canonical_message_id"], objective="local fixture",
                provider="fake", model="fake", prompt_version="v1", tool_catalog_version="v1")
            plan = await self.enqueue(runtime, source, turn_id=turn.turn_id)
            await self.wait_db("SELECT delivery_id FROM deliveries WHERE delivery_id=? AND status='committed'", (plan.delivery_ids[0],))
            self.assertEqual(len(fake.received), 1)
            self.assertEqual(fake.received[0]["payload"]["msg_id"], source)
            self.assertEqual(fake.received[0]["payload"]["msg_seq"], 1)
            message = (await asyncio.to_thread(self.query, "SELECT * FROM messages WHERE direction='outbound'"))[0]
            self.assertEqual(ActorKey.parse(message["sender_actor_key"]), ActorKey(runtime.loaded.account, "bot", "", "TEST_BOT"))
            self.assertEqual(message["source_endpoint_key"], endpoint.key)
            links = await asyncio.to_thread(self.query, "SELECT * FROM turn_send_links")
            self.assertEqual((len(links), links[0]["turn_id"]), (1, turn.turn_id))
            dispatch = await asyncio.to_thread(self.query, "SELECT status FROM durable_jobs WHERE job_id=?", (inbound["dispatch_job_id"],))
            self.assertEqual(dispatch, [])
            with patch.object(ledger, "close", wraps=ledger.close) as ledger_close, patch.object(journal, "close", wraps=journal.close) as journal_close:
                await runtime.stop()
                ledger_close.assert_not_called()
                journal_close.assert_not_called()
            await self.eventually(lambda: not fake.connections)
            contender = DedicatedAccountLease(self.database.dsn, endpoint.account, schema=self.database.schema)
            self.addCleanup(contender.close)
            self.assertIsNotNone(await asyncio.to_thread(contender.acquire))

    async def test_real_binding_control_sends_ephemeral_code_without_storing_body(self):
        async with verified_ready_platform() as fake:
            runtime = await self.open_runtime(fake)
            await self.incoming(fake, 'TEST_BIND_CREATE', '/绑定群')
            await self.eventually(lambda: len(fake.received) == 1)
            sent = fake.received[0]
            self.assertEqual((sent['kind'], sent['target']), ('user', 'TEST_RUNTIME_OWNER'))
            self.assertEqual(sent['payload']['msg_id'], 'TEST_BIND_CREATE')
            code = re.search(r'群绑定码：([0-9]{6})', sent['payload']['content']).group(1)
            self.assertTrue((await asyncio.to_thread(self.query,
                'SELECT owner_link_code_hmac FROM qq_official_accounts WHERE owner_link_code_hmac IS NOT NULL'))[0]['owner_link_code_hmac'])
            for table in ('delivery_plans', 'deliveries', 'delivery_attempts', 'delivery_receipts',
                          'messages', 'qq_official_inbound_events'):
                rows = await asyncio.to_thread(self.query, 'SELECT * FROM ' + table)
                self.assertNotIn(code, json.dumps(rows, default=str), table)
            self.assertNotIn(code, repr(self.logger.mock_calls) + repr(runtime.status()))
            await fake.emit('GROUP_AT_MESSAGE_CREATE', {
                'id': 'TEST_GROUP_BIND', 'group_openid': 'TEST_BIND_GROUP',
                'author': {'member_openid': 'TEST_BIND_MEMBER'}, 'content': code,
                'timestamp': datetime.now(timezone.utc).isoformat(),
            })
            bound = (await self.wait_db("SELECT control_decision_code FROM qq_official_inbound_events WHERE native_message_id='TEST_GROUP_BIND'"))[0]
            self.assertEqual(bound['control_decision_code'], 'confirmed')
            endpoint = EndpointKey(runtime.loaded.account, 'group', 'TEST_BIND_GROUP')
            member = ActorKey(runtime.loaded.account, 'group_member', 'TEST_BIND_GROUP', 'TEST_BIND_MEMBER')
            def authorized():
                with runtime.store.transaction() as cursor:
                    return runtime.store.authority.owner_for_actor(cursor, endpoint, member) is not None
            self.assertTrue(await runtime._calls(authorized))
            await fake.emit('GROUP_AT_MESSAGE_CREATE', {
                'id': 'TEST_BIND_REPLAY', 'group_openid': 'TEST_BIND_GROUP',
                'author': {'member_openid': 'TEST_BIND_MEMBER'}, 'content': '/绑定群 ' + code,
                'timestamp': datetime.now(timezone.utc).isoformat(),
            })
            replay = (await self.wait_db("SELECT control_decision_code FROM qq_official_inbound_events WHERE native_message_id='TEST_BIND_REPLAY'"))[0]
            self.assertEqual(replay['control_decision_code'], 'invalid')
            self.assertNotIn(code, json.dumps(await asyncio.to_thread(self.query,
                'SELECT * FROM qq_official_inbound_events'), default=str))
            await runtime.stop()

    async def test_owner_code_never_bypasses_the_process_send_limit(self):
        slots = threading.BoundedSemaphore(1)
        self.enterContext(patch('src.plugins.ai_chat.adapters.qq_official.sender._SEND_SLOTS', slots))
        async with verified_ready_platform() as fake:
            runtime = await self.open_runtime(fake)
            with slots:
                await self.incoming(fake, 'TEST_FULL', '/绑定群')
                await self.wait_db("""SELECT 1 FROM qq_official_inbound_events e JOIN qq_official_accounts a USING(account_key)
                    WHERE e.native_message_id='TEST_FULL' AND e.control_decision_code='owner_code_issued'
                      AND a.owner_link_code_hmac IS NULL""")
                self.assertFalse(fake.received)
            await self.incoming(fake, 'TEST_FREE', '/绑定群')
            await self.eventually(lambda: len(fake.received) == 1)
            self.assertEqual(fake.received[0]['payload']['msg_id'], 'TEST_FREE')
            await runtime.stop()

    async def test_unbound_group_guidance_never_sends_a_code(self):
        async with verified_ready_platform() as fake:
            runtime = await self.open_runtime(fake)
            await fake.emit('GROUP_AT_MESSAGE_CREATE', {
                'id': 'TEST_BIND_GUIDE', 'group_openid': 'TEST_BIND_GROUP',
                'author': {'member_openid': 'TEST_MEMBER'}, 'content': '/绑定群',
                'timestamp': datetime.now(timezone.utc).isoformat(),
            })
            receipt = (await self.wait_db("SELECT control_decision_code,payload_json FROM qq_official_inbound_events WHERE native_message_id='TEST_BIND_GUIDE'"))[0]
            self.assertEqual(receipt, {'control_decision_code': 'guide', 'payload_json': None})
            await self.eventually(lambda: any(item['kind'] == 'group' for item in fake.received))
            text = next(item['payload']['content'] for item in fake.received if item['kind'] == 'group')
            self.assertIn('私聊', text)
            self.assertIsNone(re.search(r'(?<!\d)\d{6}(?!\d)', text))
            await runtime.stop()

    async def test_shutdown_during_second_part_receipt_recovers_without_resending_prefix(self):
        async with verified_ready_platform() as fake:
            first = await self.open_runtime(fake)
            source = await self.incoming(fake)
            await self.wait_db("SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id=? AND status='processed'", (source,))
            entered, release = threading.Event(), threading.Event()
            self.addCleanup(release.set)
            original = first.outbox.capture_receipt
            count = 0

            def capture(reservation, body):
                nonlocal count
                count += 1
                if count == 2:
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError("test receipt barrier timed out")
                return original(reservation, body)

            with patch.object(first.outbox, "capture_receipt", side_effect=capture):
                plan = await self.enqueue(first, source, text="Part one\n\nPart two\n\nPart three", max_chars=12)
                self.assertEqual(len(plan.delivery_ids), 3)
                await self.eventually(entered.is_set)
                self.assertEqual(len(fake.received), 2)
                stopping = asyncio.create_task(first.stop())
                await asyncio.sleep(0.02)
                self.assertFalse(stopping.done())
                release.set()
                await asyncio.wait_for(stopping, 5)
            receipts = await asyncio.to_thread(self.query, "SELECT applied_at FROM delivery_receipts ORDER BY observed_at,receipt_id")
            self.assertEqual(len(receipts), 2)
            self.assertEqual(sum(row["applied_at"] is None for row in receipts), 0)
            replacement = await self.open_runtime(fake)
            await self.wait_db("SELECT delivery_id FROM deliveries WHERE plan_id=? AND status='committed'", (plan.plan_id,), lambda rows: len(rows) == 3)
            self.assertEqual(len(fake.received), 3)
            self.assertEqual([item["payload"]["msg_seq"] for item in fake.received], [1, 2, 3])
            rows = await asyncio.to_thread(self.query, "SELECT state FROM delivery_attempts ORDER BY attempt_id")
            self.assertEqual([row["state"] for row in rows].count("committed"), 3)
            self.assertEqual([row["state"] for row in rows].count("confirmed_unaccepted"), 1)
            self.assertEqual(len(await asyncio.to_thread(self.query, "SELECT * FROM messages WHERE direction='outbound'")), 3)
            await replacement.stop()

    async def test_authenticated_ready_without_optional_bot_flag_can_send(self):
        async with FakePlatform() as fake:
            runtime = self.runtime(loaded=runtime_fixture.loaded_config(fake.app_id, base=fake.base_url))
            self.assertTrue(await runtime.start())
            await asyncio.wait_for(fake.wait_ready(),5)
            source=await self.incoming(fake)
            await self.wait_db("SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id=? AND status='processed'",(source,))
            plan=await self.enqueue(runtime,source)
            await self.wait_db("SELECT delivery_id FROM deliveries WHERE delivery_id=? AND status='committed'",(plan.delivery_ids[0],))
            self.assertEqual(len(fake.received),1)
            await runtime.stop()
