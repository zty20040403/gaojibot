"""Adapter tests against real isolated PG, turn execution binding, and outbox."""
from dataclasses import replace
import json
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
from src.plugins.ai_chat.adapters.qq_official.turn_platform import (
    ExecutionBinding, OfficialTurnPlatform, PlatformBoundaryError, TransportState,
)
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.message_ir import MediaNode, MentionNode, MessageBody, TextNode
from src.plugins.ai_chat.platform_types import (
    ActorKey, EndpointKey, InboundMessage, SendRequest,
)
from tests import qq_official_turn_fixture as execution_tests


class PlatformTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = execution_tests.ExecutionFixture("runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.deliveries = DeliveryStore(self.f.database, recover_interrupted=False)
        self.addCleanup(self.deliveries.close)
        self.handle = self.f.claim().handle
        self.body = MessageBody((TextNode(0, "TEST final reply"),))
        self.inbound = InboundMessage(
            self.f.endpoint, self.f.actor, None, "TEST_INPUT", "TEST display", "member",
            MessageBody((TextNode(0, "TEST question"),)), True, None, self.f.now - 5,
            None, route_epoch=1,
        )
        self.mention_actors = frozenset()
        self.outbox = OfficialSendStore(
            self.deliveries, admission=self.f.admission, clock=lambda: self.f.now,
            mention_authority=lambda *_: self.mention_actors,
        )
        self.transport = TransportState(self.f.account, True, True)
        self.wake = Mock()
        with self.f.tx() as cursor:
            observe_ready(cursor, self.f.token, {"op": 0, "t": "READY", "d": {
                "session_id": "TEST_SESSION", "user": {"id": "TEST_BOT", "bot": True}}}, self.f.now)

    async def asyncSetUp(self):
        self.calls = DatabaseCalls()
        self.platform = self.make_platform()

    async def asyncTearDown(self):
        await self.calls.close()

    def make_platform(self, **kwargs):
        binding = ExecutionBinding(self.f.token, self.handle, self.f.admission)
        args = dict(
            inbound=self.inbound, execution=binding, outbox=self.outbox,
            database_calls=self.calls, transport_state=lambda: self.transport,
            sender_wake=self.wake, clock=lambda: self.f.now,
        )
        args.update(kwargs)
        return OfficialTurnPlatform(**args)

    def request(self, **kwargs):
        args = dict(
            endpoint=self.f.endpoint, actor=self.f.actor, body=self.body,
            delivery_id="qq-turn-result:" + self.handle.execution_id, route_epoch=1,
            reply_credential=None, reply_to_native_message_id="TEST_INPUT",
        )
        args.update(kwargs)
        return SendRequest(**args)

    def manifest(self):
        row = self.f.query("SELECT manifest_json FROM delivery_plans")[0]
        return json.loads(json.loads(row["manifest_json"])["rendered"])

    async def test_other_native_message_from_same_actor_cannot_replace_source(self):
        other = replace(self.inbound, native_message_id="TEST_OTHER_MESSAGE")
        with self.assertRaisesRegex(PlatformBoundaryError, "turn_binding_mismatch"):
            self.make_platform(inbound=other)
        self.assertEqual(self.f.query("SELECT COUNT(*) AS n FROM delivery_plans")[0]["n"], 0)

    async def test_ready_separates_credentials_auth_identity_binding_and_window(self):
        self.assertIsNone(await self.platform.ready(self.f.endpoint))
        self.assertEqual((await self.platform.readiness(self.f.endpoint)).delivery_condition, "passive_candidate")
        for value, expected in ((TransportState(self.f.account, False, True), "credentials_unavailable"),
                                (TransportState(self.f.account, True, False), "transport_not_authenticated"),
                                (TransportState(self.f.account, 1, True), "credentials_unavailable")):
            self.transport = value
            self.assertEqual(await self.platform.ready(self.f.endpoint), expected)
        self.transport = TransportState(self.f.account, True, True)
        self.f.admitted = False
        self.assertEqual(await self.platform.ready(self.f.endpoint), "actor_not_admitted")
        self.f.admitted = True
        self.f.query("UPDATE qq_official_endpoints SET binding_state='revoked'")
        self.assertEqual(await self.platform.ready(self.f.endpoint), "binding_unavailable")

    async def test_unverified_identity_and_lost_lease_have_distinct_blockers(self):
        self.f.query("UPDATE qq_official_accounts SET bot_native_id=NULL,bot_identity_source=NULL,bot_identity_verified_at=NULL,bot_identity_contract=NULL,bot_identity_revision=0")
        self.assertEqual(await self.platform.ready(self.f.endpoint), "bot_identity_unverified")
        self.f.account_lease.close()
        self.assertEqual(await self.platform.ready(self.f.endpoint), "account_lease_unavailable")

    async def test_result_handoff_is_atomic_and_duplicate_never_reenqueues(self):
        with patch.object(self.outbox, "enqueue", wraps=self.outbox.enqueue) as enqueue:
            first = await self.platform.send(self.request())
            second = await self.platform.send(self.request())
        self.assertEqual((first.status, second.status), ("pending", "pending"))
        self.assertEqual(first.receipt, second.receipt)
        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(self.f.query("SELECT state FROM qq_official_turn_executions")[0]["state"], "handed_off")
        self.assertEqual(len(self.f.query("SELECT * FROM delivery_plans")), 1)

    async def test_identity_and_route_cannot_be_substituted(self):
        other_actor = ActorKey(self.f.account, "group_member", self.f.endpoint.native_conversation_id, "TEST_OTHER")
        variants = ({"actor": other_actor}, {"route_epoch": 2}, {"delivery_id": "TEST_ARBITRARY"},
                    {"fragment_id": "1"}, {"reply_to_native_message_id": "TEST_UNPROVEN"})
        for change in variants:
            with self.subTest(field=next(iter(change))):
                self.assertEqual((await self.platform.send(self.request(**change))).status, "failed")
        self.assertFalse(self.f.query("SELECT * FROM deliveries"))

    async def test_same_native_ids_other_account_environment_and_scene_are_rejected(self):
        for account in (replace(self.f.account, app_id="TEST_OTHER_APP"), replace(self.f.account, environment="sandbox")):
            endpoint = EndpointKey(account, "group", self.f.endpoint.native_conversation_id)
            actor = ActorKey(account, "group_member", endpoint.native_conversation_id, self.f.actor.native_user_id)
            with self.assertRaises(PlatformBoundaryError):
                await self.platform.send(self.request(endpoint=endpoint, actor=actor))
        private = EndpointKey(self.f.account, "private", self.f.endpoint.native_conversation_id)
        actor = ActorKey(self.f.account, "c2c_user", "", private.native_conversation_id)
        with self.assertRaises(PlatformBoundaryError):
            await self.platform.send(self.request(endpoint=private, actor=actor))
        self.assertFalse(self.f.query("SELECT * FROM deliveries"))

    async def test_native_mentions_respect_outbox_mention_authority(self):
        self.mention_actors = frozenset({self.f.actor})
        body = MessageBody((MentionNode(0, self.f.actor.native_user_id, "TEST member"),))
        self.assertEqual((await self.platform.send(self.request(body=body))).status, "pending")
        self.assertIn("<qqbot-at-user", json.dumps(self.manifest()))

    async def test_media_requires_attachment_and_does_not_create_a_fake_delivery(self):
        body = MessageBody((MediaNode(0, "file", source="TEST_LOCAL_SOURCE", name="TEST_REPORT.pdf"),))
        result = await self.platform.send(self.request(body=body))
        self.assertEqual((result.status, result.reason), ("failed", "needs_attachment"))
        self.assertFalse(self.f.query("SELECT * FROM delivery_plans"))
        self.assertFalse(self.platform.features.native_files)

    async def test_sdk_free_module_import_does_not_open_network_or_credentials(self):
        script = r"""
import importlib.abc, sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'nonebot' or fullname.startswith('nonebot.'):
            raise AssertionError('legacy SDK import')
def audit(event, args):
    if event in {'socket.connect','socket.bind','socket.getaddrinfo','subprocess.Popen'}:
        raise AssertionError('network or subprocess during import')
    if event == 'open' and args and isinstance(args[0],str) and args[0].endswith(('/app-id','/app-secret')):
        raise AssertionError('credential read during import')
sys.meta_path.insert(0,Guard())
sys.addaudithook(audit)
from src.plugins.ai_chat.adapters.qq_official import turn_platform
assert not any(name == 'nonebot' or name.startswith('nonebot.') for name in sys.modules)
"""
        result = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
