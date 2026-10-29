"""Adapter tests against real isolated PG, execution primitives and M3.

Only synthetic TEST identities. No token client, REST client, or websocket is
constructed. The unchanged repository fixture creates its own disposable schema.
"""
from dataclasses import replace
import json
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from src.plugins.ai_chat.adapters.qq_official import execution
from tests import test_qq_official_execution as execution_tests

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.outbox import OfficialOutbox
from src.plugins.ai_chat.adapters.qq_official.rate_policy import LocalRatePermit
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.message_ir import MediaNode, MessageBody, MentionNode, TextNode
from src.plugins.ai_chat.platform_types import (
    ActorKey, CapabilityEvidence, EffectiveCapabilities, EndpointKey, InboundMessage,
    ReplyCredential, SendRequest,
)
from src.plugins.ai_chat.adapters.qq_official.turn_platform import (
    ExecutionBinding, MembershipEvidence, OfficialTurnPlatform, PlatformBoundaryError,
    TransportState, TurnDeliveryPolicy,
)


class PlatformTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = execution_tests.ExecutionTests("test_result_capture_is_exactly_idempotent_and_rejects_changed_body")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.deliveries = DeliveryStore(self.f.database, recover_interrupted=False)
        self.addCleanup(self.deliveries.close)
        self.handle = self.f.claim().handle
        self.body = MessageBody((TextNode(0, "TEST frozen final"),))
        self.f.window(source="TEST_INPUT")
        self.credential = ReplyCredential(self.f.endpoint, "msg_id", "TEST_INPUT", self.f.now,
            self.f.now, self.f.now + 295, 5)
        self.inbound = InboundMessage(self.f.endpoint, self.f.actor, None, "TEST_INPUT", "TEST display", "member",
            MessageBody((TextNode(0, "TEST question"),)), True, None, self.f.now - 5,
            self.credential, route_epoch=1)
        self.memberships = ()
        self.mention_actors = frozenset()
        self.outbox = OfficialOutbox(self.deliveries, admission=self.f.admission,
            pepper=b"TEST_PLATFORM_PEPPER" * 3, clock=lambda: self.f.now,
            mention_authority=lambda *_: self.mention_actors)
        self.transport = TransportState(self.f.account, True, True)
        self.wake = Mock()
        with self.f.tx() as cursor:
            observe_ready(cursor, self.f.token, {"op":0,"t":"READY","d":{
                "session_id":"TEST_SESSION","user":{"id":"TEST_BOT","bot":True}}}, self.f.now)

    async def asyncSetUp(self):
        self.calls = DatabaseCalls()
        self.platform = self.make_platform()

    async def asyncTearDown(self):
        await self.calls.close()

    def make_platform(self, **kwargs):
        binding = ExecutionBinding(execution, self.f.token, self.handle, self.f.job, self.f.admission)
        args = dict(inbound=self.inbound, execution=binding, outbox=self.outbox,
            database_calls=self.calls, transport_state=lambda: self.transport,
            membership_evidence=lambda *_: self.memberships, sender_wake=self.wake, clock=lambda:self.f.now)
        args.update(kwargs)
        return OfficialTurnPlatform(**args)

    def freeze(self, body=None):
        self.body = body or self.body
        with self.f.tx() as cursor:
            execution.capture_result(cursor, **self.f.args(self.handle), body=self.body,
                turn_id=None, status="succeeded")

    def request(self, **kwargs):
        args = dict(endpoint=self.f.endpoint, actor=self.f.actor, body=self.body,
            delivery_id=execution.logical_result_key(self.handle), route_epoch=1,
            reply_credential=self.inbound.reply_credential, reply_to_native_message_id="TEST_INPUT")
        args.update(kwargs)
        return SendRequest(**args)

    def manifest(self):
        row = self.f.query("SELECT manifest_json FROM delivery_plans")[0]
        return json.loads(json.loads(row["manifest_json"])["rendered"])

    async def test_other_native_message_from_same_actor_cannot_replace_source_window(self):
        other = replace(self.inbound, native_message_id="TEST_OTHER_MESSAGE",
            reply_credential=replace(self.credential, value="TEST_OTHER_MESSAGE"))
        with self.assertRaisesRegex(PlatformBoundaryError, "turn_binding_mismatch"):
            self.make_platform(inbound=other)
        self.assertEqual(self.f.query("SELECT COUNT(*) AS n FROM delivery_plans")[0]["n"], 0)

    async def test_ready_separates_credentials_auth_identity_binding_and_window(self):
        self.assertIsNone(await self.platform.ready(self.f.endpoint))
        self.assertEqual((await self.platform.readiness(self.f.endpoint)).delivery_condition, "passive_candidate")
        for value, expected in ((TransportState(self.f.account,False,True),"credentials_unavailable"),
                                (TransportState(self.f.account,True,False),"transport_not_authenticated"),
                                (TransportState(self.f.account,1,True),"credentials_unavailable")):
            self.transport = value
            self.assertEqual(await self.platform.ready(self.f.endpoint), expected)
        self.transport = TransportState(self.f.account,True,True)
        self.f.query("UPDATE qq_official_reply_windows SET state='expired'")
        readiness = await self.platform.readiness(self.f.endpoint)
        self.assertIsNone(readiness.blocker)
        self.assertEqual(readiness.delivery_condition,"waiting_reply_window")
        self.f.admitted = False
        self.assertEqual(await self.platform.ready(self.f.endpoint),"actor_not_admitted")
        self.f.admitted = True
        self.f.query("UPDATE qq_official_endpoints SET binding_state='revoked'")
        self.assertEqual(await self.platform.ready(self.f.endpoint),"binding_unavailable")

    async def test_unverified_identity_and_lost_lease_have_distinct_blockers(self):
        self.f.query("UPDATE qq_official_accounts SET bot_native_id=NULL,bot_identity_source=NULL,bot_identity_verified_at=NULL,bot_identity_contract=NULL,bot_identity_revision=0")
        self.assertEqual(await self.platform.ready(self.f.endpoint),"bot_identity_unverified")
        self.f.account_lease.close()
        self.assertEqual(await self.platform.ready(self.f.endpoint),"account_lease_unavailable")

    async def test_frozen_result_handoff_is_atomic_and_duplicate_never_reenqueues(self):
        self.freeze()
        with patch.object(self.outbox,"enqueue",wraps=self.outbox.enqueue) as enqueue:
            first = await self.platform.send(self.request())
            second = await self.platform.send(self.request())
        self.assertEqual((first.status,second.status),("pending","pending"))
        self.assertEqual(first.receipt,second.receipt)
        self.assertEqual(enqueue.call_count,1)
        self.assertEqual(self.f.query("SELECT state FROM qq_official_turn_executions")[0]["state"],"handed_off")
        self.assertEqual(len(self.f.query("SELECT * FROM delivery_plans")),1)
        self.assertFalse(self.f.query("SELECT * FROM delivery_attempts"))

    async def test_attach_failure_rolls_back_manifest_and_parts(self):
        self.freeze()
        with patch.object(execution,"attach_plan",side_effect=RuntimeError("TEST_PRIVATE_ERROR")):
            result=await self.platform.send(self.request())
        self.assertEqual(result.status,"failed")
        self.assertNotIn("TEST_PRIVATE_ERROR",repr(result))
        self.assertFalse(self.f.query("SELECT * FROM deliveries"))
        self.assertFalse(self.f.query("SELECT * FROM delivery_plans"))
        self.assertEqual(self.f.query("SELECT state FROM qq_official_turn_executions")[0]["state"],"result_ready")

    async def test_body_identity_route_and_reply_proof_cannot_be_substituted(self):
        self.freeze()
        other_actor=ActorKey(self.f.account,"group_member",self.f.endpoint.native_conversation_id,"TEST_OTHER")
        variants=({"body":MessageBody((TextNode(0,"DIFFERENT"),))},{"actor":other_actor},
            {"route_epoch":2},{"delivery_id":"TEST_ARBITRARY"},{"fragment_id":"1"},
            {"reply_credential":replace(self.credential,value="TEST_FORGED")},
            {"reply_to_native_message_id":"TEST_UNPROVEN"})
        for change in variants:
            with self.subTest(field=next(iter(change))):
                self.assertEqual((await self.platform.send(self.request(**change))).status,"failed")
        self.assertFalse(self.f.query("SELECT * FROM deliveries"))

    async def test_same_native_ids_other_account_environment_and_scene_are_rejected(self):
        self.freeze()
        for account in (replace(self.f.account,app_id="TEST_OTHER_APP"),replace(self.f.account,environment="sandbox")):
            endpoint=EndpointKey(account,"group",self.f.endpoint.native_conversation_id)
            actor=ActorKey(account,"group_member",endpoint.native_conversation_id,self.f.actor.native_user_id)
            with self.assertRaises(PlatformBoundaryError):
                await self.platform.send(self.request(endpoint=endpoint,actor=actor,reply_credential=None))
        private=EndpointKey(self.f.account,"private",self.f.endpoint.native_conversation_id)
        actor=ActorKey(self.f.account,"c2c_user","",private.native_conversation_id)
        with self.assertRaises(PlatformBoundaryError):
            await self.platform.send(self.request(endpoint=private,actor=actor,reply_credential=None))
        self.assertFalse(self.f.query("SELECT * FROM deliveries"))

    async def test_stale_job_or_copied_execution_handle_cannot_enqueue(self):
        self.freeze()
        forged=self.make_platform(execution=ExecutionBinding(execution,self.f.token,replace(self.handle),self.f.job,self.f.admission))
        with self.assertRaises(execution.ExecutionFenceLost):
            await forged.send(self.request())
        self.f.jobs.defer(self.f.job,delay_seconds=1,now=self.f.now)
        self.assertEqual((await self.platform.send(self.request())).status,"failed")
        self.assertFalse(self.f.query("SELECT * FROM deliveries"))

    async def test_expired_original_credential_does_not_discard_frozen_background_result(self):
        # Replace the unused prepared test fixture, then enter the real trusted
        # claim API; request/model fields cannot choose this scheduling purpose.
        self.f.query("DELETE FROM qq_official_turn_executions WHERE state='prepared'")
        self.handle=self.f.claim("background_result").handle
        self.inbound=replace(self.inbound,reply_credential=replace(self.credential,expires_at=self.f.now-1))
        self.f.query("UPDATE qq_official_reply_windows SET expires_at=?,state='expired'",(self.f.now-1,))
        self.platform=self.make_platform()
        self.freeze()
        result=await self.platform.send(self.request())
        self.assertEqual(result.status,"pending")
        row=self.f.query("SELECT purpose,expires_at FROM deliveries")[0]
        self.assertEqual((row["purpose"],row["expires_at"]),("background_result",self.handle.deadline_at))
        self.assertEqual(self.outbox.candidate(self.f.token,result.receipt["delivery_ids"][0]).reason,"waiting_reply_window")

    async def test_accepted_is_pending_and_only_applied_receipt_is_committed(self):
        self.freeze()
        queued=await self.platform.send(self.request())
        delivery=queued.receipt["delivery_ids"][0]
        permit=LocalRatePermit(self.f.account.key,self.f.endpoint.key,"group","passive","unverified",float(self.f.now))
        reservation=self.outbox.prepare(self.f.token,delivery,permit)
        self.assertTrue(self.outbox.mark_dispatching(self.f.token,reservation))
        self.outbox.request_started(reservation)
        receipt=self.outbox.capture_receipt(reservation,{"id":"TEST_PLATFORM_RECEIPT"})
        self.f.query("UPDATE delivery_attempts SET bot_actor_key=NULL,bot_identity_source=NULL,bot_identity_revision=NULL")
        self.assertEqual(self.outbox.finalize(self.f.token,receipt,ledger=self.f.ledger).status,"accepted")
        self.assertEqual((await self.platform.send(self.request())).status,"pending")
        bot=ActorKey(self.f.account,"bot","","TEST_BOT")
        self.f.query("UPDATE delivery_attempts SET bot_actor_key=?,bot_identity_source='ready',bot_identity_revision=1",(bot.key,))
        self.assertEqual(self.outbox.finalize(self.f.token,receipt,ledger=self.f.ledger).status,"committed")
        self.assertEqual((await self.platform.send(self.request())).status,"committed")

    async def test_native_mentions_require_both_current_inbound_evidence_and_m3_revalidation(self):
        self.mention_actors=frozenset({self.f.actor})
        self.memberships=(MembershipEvidence(self.f.actor,self.f.endpoint,1,1,self.f.now-5,self.f.now+20,"admitted_inbound"),)
        self.freeze(MessageBody((MentionNode(0,self.f.actor.native_user_id,"TEST member"),)))
        self.assertEqual((await self.platform.send(self.request())).status,"pending")
        self.assertIn("<qqbot-at-user",json.dumps(self.manifest()))

    async def test_stale_or_unverified_membership_never_emits_native_mention(self):
        self.mention_actors=frozenset({self.f.actor})
        self.memberships=(MembershipEvidence(self.f.actor,self.f.endpoint,1,1,self.f.now-5,self.f.now,"admitted_inbound"),)
        self.freeze(MessageBody((MentionNode(0,self.f.actor.native_user_id,"TEST member"),)))
        await self.platform.send(self.request())
        self.assertNotIn("<qqbot-at-user",json.dumps(self.manifest()))

    async def test_capability_unknown_is_not_implementation_support_or_stale_grant(self):
        self.assertTrue(self.platform.features.markdown)
        caps=await self.platform.effective_capabilities(self.f.endpoint)
        self.assertEqual(dict(caps.grants),{})
        proof=CapabilityEvidence(self.f.endpoint,"available","TEST_VERIFIED_CONTRACT",self.f.now-1,self.f.now+10)
        self.platform=self.make_platform(capability_evidence=lambda *_:EffectiveCapabilities(self.f.endpoint,1,{"markdown":proof}))
        self.assertTrue((await self.platform.effective_capabilities(self.f.endpoint)).allows("markdown",self.f.now))
        self.f.query("UPDATE qq_official_endpoints SET capability_revision=2")
        self.assertFalse((await self.platform.effective_capabilities(self.f.endpoint)).allows("markdown",self.f.now))
        self.assertFalse(self.platform.stream_enabled)
        self.assertFalse(self.platform.progress_enabled)
        self.assertFalse(await self.platform.recall(self.f.endpoint,"TEST_MESSAGE"))

    async def test_unverified_new_window_policy_cannot_be_enabled_implicitly(self):
        self.outbox.allow_newer_windows=True
        with self.assertRaisesRegex(PlatformBoundaryError,"policy_mismatch"):
            self.make_platform()
        platform=self.make_platform(policy=TurnDeliveryPolicy(allow_newer_windows=True))
        self.assertTrue(platform.policy.allow_newer_windows)

    async def test_media_requires_attachment_and_does_not_create_a_fake_delivery(self):
        self.freeze(MessageBody((MediaNode(0,"file",source="TEST_LOCAL_SOURCE",name="TEST_REPORT.pdf"),)))
        result=await self.platform.send(self.request())
        self.assertEqual((result.status,result.reason),("failed","needs_attachment"))
        self.assertFalse(self.f.query("SELECT * FROM delivery_plans"))
        self.assertFalse(self.platform.features.native_files)

    async def test_sdk_free_module_import_does_not_open_network_or_credentials(self):
        script = r'''
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
'''
        result=subprocess.run([sys.executable,"-B","-c",script],
            capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__ == "__main__":
    unittest.main()
