from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock

from src.plugins.ai_chat.adapters.qq_official.binding import BindingEffect
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.outbox import OfficialOutbox
from src.plugins.ai_chat.adapters.qq_official.rate_policy import LocalRatePermit
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.application.commands import CommandPolicy, CommandServices
from src.plugins.ai_chat.conversation_keys import conversation_key
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode, body_to_json
from src.plugins.ai_chat.model_preferences import ModelPreferenceStore
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey, InboundMessage
from src.plugins.ai_chat.tasks import RunningTaskRegistry
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.turn_journal import TurnJournal
from tests.qq_official_outbox_fixture import OutboxFixture

from src.plugins.ai_chat.adapters.qq_official.commands import route_command, usage_for_actor
from src.plugins.ai_chat.adapters.qq_official.control_receipts import (
    ControlEvidenceError, assert_control_attempt, bind_control_plan,
    claim_control_attempt, load_authority, record_decision,
)

def require_control_schema(fixture):
    fixture.assertIsNotNone(fixture.query("SELECT to_regclass(?) AS name",
        (fixture.database.schema + ".qq_official_control_decisions",))[0]["name"])


def inbound(account=None, *, private=False, user="TEST_USER", group="TEST_GROUP"):
    account = account or AccountKey("qq-official", "TEST_APP", "test")
    endpoint = EndpointKey(account, "private" if private else "group", user if private else group)
    actor = ActorKey(account, "c2c_user" if private else "group_member", "" if private else group, user)
    return InboundMessage(endpoint, actor, "TEST_EVENT", "TEST_NATIVE", "test", "member",
        MessageBody((TextNode(0, "test"),)), True, None, 100, None)


class RouterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.models = ModelPreferenceStore(Path(self.temp.name) / "models.json")
        self.efforts = ModelPreferenceStore(Path(self.temp.name) / "efforts.json")
        self.registry = RunningTaskRegistry()
        self.begin = AsyncMock()
        self.admission = lambda _: True
        self.policy = CommandPolicy(models=("test-model",), efforts=("low", "high"),
            default_model="test-model", default_effort="low", allow_model_read=True,
            allow_model_change=True, allow_effort_read=True, allow_effort_change=True)
        self.services = CommandServices(self.registry, self.models, self.efforts, lambda _: self.policy)
        self.workers = []

    async def asyncTearDown(self):
        for task in self.workers:
            task.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)

    async def route(self, text, message=None, services=None):
        return await route_command(text, message or inbound(), services=services or self.services,
            admission=self.admission, before_effect=self.begin)

    async def test_help_and_unknown_are_explicit_shared_results(self):
        self.assertIn("/model", (await self.route("/help")).result.text)
        result = await self.route("/not-supported TEST_PRIVATE_ARGUMENT")
        self.assertEqual((result.kind, result.result.status), ("reply", "unsupported"))
        self.assertNotIn("TEST_PRIVATE_ARGUMENT", result.result.text + repr(result))
        self.assertEqual((await self.route("plain text")).kind, "not_command")

    async def test_binding_never_reexecutes_or_enters_generic_effects(self):
        callback = AsyncMock(side_effect=AssertionError("second reducer must not execute"))
        services = replace(self.services, binding_handler=callback)
        for text in ("/bind create", "/bind submit BC-FAKE 123456", "/bind confirm BN-FAKE 1", "/whoami"):
            self.assertEqual((await self.route(text, services=services)).kind, "control")
        callback.assert_not_called()
        self.begin.assert_not_called()

    async def test_unbound_unknown_slash_and_normal_commands_do_not_reply(self):
        self.admission = lambda _: False
        for text in ("/help", "/model test-model", "/not-supported"):
            self.assertEqual((await self.route(text)).kind, "suppressed")
        self.assertEqual((await self.route("/whoami")).kind, "control")
        self.begin.assert_not_called()

    async def test_command_must_durably_begin_before_setting_change(self):
        self.begin.side_effect = RuntimeError("TEST_EXECUTION_NOT_BEGUN")
        with self.assertRaises(RuntimeError):
            await self.route("/model test-model")
        self.assertEqual(self.models.items(), [])
        self.begin.side_effect = None
        self.assertEqual((await self.route("/model test-model")).result.status, "ok")
        self.assertEqual((await self.route("/effort high")).result.status, "ok")
        self.assertEqual(self.models.get_explicit(conversation_key(inbound())), "test-model")

    async def test_model_permissions_do_not_come_from_platform_owner(self):
        services = replace(self.services, host_policy=None)
        result = await self.route("/model test-model", replace(inbound(), platform_role="owner"), services)
        self.assertEqual(result.result.status, "denied")
        self.assertEqual((await self.route("/model group test-model")).result.status, "denied")
        self.assertEqual(self.models.items(), [])

    async def start_task(self, message):
        ready = asyncio.get_running_loop().create_future()
        async def run():
            info = self.registry.register_inbound(conversation_id=conversation_key(message), inbound=message, summary="TEST_TASK")
            ready.set_result(info)
            try:
                await asyncio.Event().wait()
            finally:
                self.registry.finish(info.task_id)
        worker = asyncio.create_task(run())
        self.workers.append(worker)
        return await ready, worker

    async def test_task_cancel_uses_entire_actor_endpoint_and_bad_id_has_no_fallback(self):
        owner = inbound()
        info, worker = await self.start_task(owner)
        outsiders = (inbound(user="TEST_OTHER"), inbound(private=True),
            inbound(group="TEST_OTHER_GROUP"), inbound(AccountKey("qq-official", "TEST_OTHER_APP", "test")),
            inbound(AccountKey("qq-official", "TEST_APP", "sandbox")))
        for other in outsiders:
            self.assertEqual((await self.route("/cancel " + info.task_id, other)).result.status, "not_found")
        self.assertEqual((await self.route("/cancel DOES_NOT_EXIST", owner)).result.status, "not_found")
        self.assertFalse(worker.done())
        self.assertEqual((await self.route("/task " + info.task_id, owner)).result.status, "ok")
        self.assertEqual((await self.route("/cancel " + info.task_id, owner)).result.status, "ok")
        await asyncio.sleep(0)
        self.assertTrue(worker.done())

    async def test_usage_without_scoped_host_port_is_explicitly_unavailable(self):
        result = await self.route("/usage")
        self.assertEqual((result.kind, result.result.status), ("reply", "unavailable"))


class ControlReceiptTests(OutboxFixture):
    def setUp(self):
        super().setUp()
        require_control_schema(self)
        self.private = EndpointKey(self.account, "private", "TEST_CALLER_OPENID")
        self.private_actor = ActorKey(self.account, "c2c_user", "", "TEST_CALLER_OPENID")
        self.provision(self.private)
        self.query("UPDATE qq_official_endpoints SET binding_state='unbound' WHERE endpoint_key=?", (self.private.key,))
        self.seq = 0

    def seed(self, *, action="whoami", code="identity", window=True, metadata=True, receipt=True,
             endpoint=None, actor=None):
        self.seq += 1
        endpoint, actor = endpoint or self.private, actor or self.private_actor
        inbox_id, native = secrets.token_hex(16), "TEST_CONTROL_" + str(self.seq)
        window_id = self.window(endpoint=endpoint, source=native) if window else None
        with self.tx() as cursor:
            cursor.execute("""INSERT INTO qq_official_inbound_events
                (inbox_id,account_key,endpoint_key,endpoint_generation,route_epoch,event_type,native_message_id,
                status,reason_code,echo_state,received_at,created_at,updated_at,
                control_actor_key,control_action,control_decision_code)
                VALUES (?,?,?,1,1,'C2C_MESSAGE_CREATE',?,'processed','binding_control','not_echo',?,?,?,?,?,?)""",
                (inbox_id,self.account.key,endpoint.key,native,self.now,self.now,self.now,
                 actor.key if metadata else None,action if metadata else None,code if metadata else None))
            if receipt:
                cursor.execute("""INSERT INTO qq_official_gateway_receipts
                    (account_key,session_id,seq,event_type,disposition,inbox_id,received_at)
                    VALUES (?,'TEST_SESSION',?,'C2C_MESSAGE_CREATE','control',?,?)""",
                    (self.account.key,self.seq,inbox_id,self.now))
        return dict(inbox_id=inbox_id, source_session_id="TEST_SESSION", source_seq=self.seq), window_id

    def record(self, source, *, now=None):
        with self.tx() as cursor:
            return record_decision(cursor, self.token, **source, now=self.now if now is None else now)

    def load(self, control_id, *, now=None):
        with self.tx() as cursor:
            return load_authority(cursor, self.token, control_id=control_id, now=self.now if now is None else now)

    def test_first_contact_whoami_does_not_require_or_create_owner(self):
        source, _ = self.seed()
        control_id = self.record(source)
        view = self.load(control_id)
        self.assertEqual((view.endpoint, view.actor), (self.private, self.private_actor))
        self.assertIn("TEST_CALLER_OPENID", body_to_json(view.body))
        self.assertNotIn("TEST_CALLER_OPENID", repr(view))
        self.assertIn("不授予 owner", body_to_json(view.body))
        self.assertEqual(view.expires_at, self.now + 30)
        self.assertFalse(self.query("SELECT * FROM qq_official_owner_grants"))

    def test_effect_object_or_preexisting_body_free_row_cannot_mint_authority(self):
        effect = BindingEffect("identity", self.private, actual_submitter=self.private_actor)
        with self.tx() as cursor, self.assertRaises(TypeError):
            record_decision(cursor, self.token, effect=effect, now=self.now)
        for kwargs in ({"metadata": False}, {"receipt": False}):
            source, _ = self.seed(**kwargs)
            with self.assertRaisesRegex(ControlEvidenceError, "authenticated_control_source_required"):
                self.record(source)
        self.assertFalse(self.query("SELECT * FROM qq_official_control_decisions"))

    def test_wrong_receipt_account_or_endpoint_actor_fails_closed(self):
        source, _ = self.seed()
        with self.assertRaises(ControlEvidenceError):
            self.record({**source, "source_session_id": "OTHER_SESSION"})
        source2, _ = self.seed(actor=ActorKey(self.account, "group_member", self.endpoint.native_conversation_id, "TEST_CALLER_OPENID"))
        with self.assertRaisesRegex(ControlEvidenceError, "invalid_control_identity"):
            self.record(source2)
        self.assertFalse(self.query("SELECT * FROM qq_official_control_decisions"))

    def test_duplicate_and_delayed_processing_never_extend_original_deadline(self):
        source, _ = self.seed()
        control_id = self.record(source)
        self.assertEqual(self.record(source, now=self.now + 20), control_id)
        self.assertEqual(self.load(control_id, now=self.now + 29).expires_at, self.now + 30)
        self.assertIsNone(self.load(control_id, now=self.now + 30))
        self.assertEqual(len(self.query("SELECT * FROM qq_official_control_decisions")), 1)

    def test_missing_window_is_terminal_and_does_not_construct_one(self):
        source, _ = self.seed(window=False)
        control_id = self.record(source)
        self.assertIsNone(self.load(control_id))
        self.assertFalse(self.query("SELECT * FROM qq_official_reply_windows"))
        self.assertEqual(self.query("SELECT state FROM qq_official_control_decisions")[0]["state"], "terminal")

    def test_window_route_revocation_or_replacement_does_not_transfer_authority(self):
        for change in ("route_epoch=route_epoch+1", "membership_state='absent'", "binding_state='revoked'"):
            source, _ = self.seed()
            control_id = self.record(source)
            self.query("UPDATE qq_official_endpoints SET " + change + " WHERE endpoint_key=?", (self.private.key,))
            self.assertIsNone(self.load(control_id))
            self.query("UPDATE qq_official_endpoints SET route_epoch=1,membership_state='present',binding_state='unbound' WHERE endpoint_key=?", (self.private.key,))

    def test_suppressed_otp_rate_limit_and_preview_do_not_gain_replay_authority(self):
        for action, code in (("create", "sending"), ("whoami", "limited"), ("status", "candidate")):
            source, _ = self.seed(action=action, code=code)
            control_id = self.record(source)
            self.assertIsNone(self.load(control_id))
        self.assertFalse(self.query("SELECT * FROM qq_official_control_decisions WHERE state<>'terminal'"))

    def test_confirm_and_reject_replies_are_fixed_not_another_reducer(self):
        for action, code in (("confirm", "confirmed"), ("reject", "rejected"), ("status", "issued")):
            source, _ = self.seed(action=action, code=code)
            view = self.load(self.record(source))
            self.assertEqual(len(view.body.nodes), 1)
            self.assertNotIn("TEST_CALLER", body_to_json(view.body))
        self.assertFalse(self.query("SELECT * FROM qq_official_binding_challenges"))

    def test_tampered_body_digest_cannot_authorize_arbitrary_output(self):
        source, _ = self.seed()
        control_id = self.record(source)
        self.query("UPDATE qq_official_control_decisions SET body_digest=? WHERE control_id=?", ("0" * 64,control_id))
        with self.assertRaisesRegex(ControlEvidenceError, "control_body_conflict"):
            self.load(control_id)

    def test_usage_query_is_exact_actor_and_endpoint_scoped(self):
        ledger, journal, jobs = MessageLedger(self.database), TurnJournal(self.database), DurableJobStore(self.database)
        for store in (ledger,journal,jobs):
            self.addCleanup(store.close)
        own = inbound(self.account,user=self.actor.native_user_id,group=self.endpoint.native_conversation_id)
        identities = (own, inbound(self.account,user="TEST_OTHER",group=self.endpoint.native_conversation_id),
            inbound(self.account,private=True,user=self.actor.native_user_id),
            inbound(AccountKey("qq-official","TEST_OTHER_APP","test"),user=self.actor.native_user_id),
            inbound(AccountKey("qq-official",self.account.app_id,"sandbox"),user=self.actor.native_user_id))
        for index,message in enumerate(identities):
            if message.endpoint != self.endpoint:
                self.provision(message.endpoint)
            native = "TEST_USAGE_"+secrets.token_hex(8)
            stored = ledger.record_message(message.scope,native_message_id=native,
                sender_native_user_id=message.actor.native_user_id,sender_display="test",
                body=message.body,occurred_at=self.now,actor=message.actor,source_endpoint=message.endpoint)
            turn = journal.start_turn(message.scope,trigger_canonical_message_id=stored.canonical_message_id,
                objective="TEST_USAGE",provider="fake",model="fake",started_at=self.now)
            journal.finish_turn(turn.turn_id,status="succeeded",input_tokens=7 if index==0 else 99,
                output_tokens=2,total_tokens=9 if index==0 else 101,finished_at=self.now)
            inbox_id=secrets.token_hex(16)
            job,_ = jobs.enqueue(kind="TEST_USAGE",idempotency_key=inbox_id,payload={"inbox_id":inbox_id},now=self.now)
            with self.tx() as cursor:
                cursor.execute("""INSERT INTO qq_official_inbound_events
                    (inbox_id,account_key,endpoint_key,endpoint_generation,route_epoch,event_type,native_message_id,
                     status,echo_state,canonical_message_id,dispatch_job_id,received_at,created_at,updated_at)
                    VALUES (?,?,?,1,1,'GROUP_AT_MESSAGE_CREATE',?,'processed','not_echo',?,?,?,?,?)""",
                    (inbox_id,message.endpoint.account.key,message.endpoint.key,native,stored.canonical_message_id,
                     job.job_id,self.now,self.now,self.now))
                values = dict(execution_id=secrets.token_hex(16),account_key=message.endpoint.account.key,
                    endpoint_key=message.endpoint.key,requester_actor_key=message.actor.key,inbox_id=inbox_id,
                    job_id=job.job_id,canonical_message_id=stored.canonical_message_id,source_native_message_id=native,
                    scope_key=message.scope.key,endpoint_generation=1,route_epoch=1,account_fencing_epoch=1,
                    worker_owner="TEST_WORKER",job_lease_generation=1,state="executing",input_digest="a"*64,
                    result_purpose="final",execution_kind="chat",journal_turn_id=turn.turn_id,
                    deadline_at=self.now+60,created_at=self.now,updated_at=self.now,started_at=self.now)
                cursor.execute("INSERT INTO qq_official_turn_executions ("+",".join(values)+") VALUES ("+
                    ",".join("?" for _ in values)+")",tuple(values.values()))
        with self.tx() as cursor:
            totals = usage_for_actor(cursor, own)
        self.assertEqual(totals, {"turns": 1,"input_tokens": 7,"output_tokens": 2,"total_tokens": 9})
        self.query("UPDATE turn_visibility SET min_turn_ordinal=2 WHERE scope_key=?",(own.scope.key,))
        with self.tx() as cursor:
            self.assertEqual(usage_for_actor(cursor,own)["turns"],0)

    def make_plan(self, control_id, *, wrong_body=False, bind=True):
        view = self.load(control_id)
        deliveries = DeliveryStore(self.database)
        self.addCleanup(deliveries.close)
        # Unit-test the exact plan binder independently of ingress admission.
        # The complete source-authorized sender path has separate loopback tests.
        outbox = OfficialOutbox(deliveries, admission=lambda c,e,a: True,
            pepper=b"TEST_CONTROL_PEPPER" * 3, clock=lambda: self.now)
        body = MessageBody((TextNode(0,"TEST_ARBITRARY_BODY"),)) if wrong_body else view.body
        source = self.query("SELECT source_native_message_id FROM qq_official_control_decisions WHERE control_id=?", (control_id,))[0]["source_native_message_id"]
        with self.tx() as cursor:
            observe_ready(cursor, self.token, {"op":0,"t":"READY","d":{"session_id":"TEST_SESSION",
                "user":{"id":"TEST_BOT_ID","bot":True}}}, self.now)
            plan = outbox.enqueue(self.token, rendered=render_plan(body,view.endpoint,markdown=False),
                requester=view.actor, logical_key="TEST_CONTROL_PLAN:"+secrets.token_hex(8),
                source_scope_key=view.endpoint.scope.key, expires_at=view.expires_at,
                source_native_message_id=source, transaction_cursor=cursor)
            if bind:
                bind_control_plan(cursor,self.token,control_id=control_id,plan_id=plan.plan_id,
                    delivery_id=plan.delivery_ids[0],now=self.now)
        return outbox,plan

    def test_control_plan_binds_only_exact_safe_body_and_one_delivery(self):
        source,_ = self.seed()
        control_id = self.record(source)
        with self.assertRaisesRegex(ControlEvidenceError,"control_plan_not_exact"):
            self.make_plan(control_id,wrong_body=True)
        self.assertFalse(self.query("SELECT * FROM delivery_plans"))
        _,plan = self.make_plan(control_id)
        row = self.query("SELECT * FROM qq_official_control_decisions WHERE control_id=?",(control_id,))[0]
        self.assertEqual((row["plan_id"],row["delivery_id"],row["state"]),
                         (plan.plan_id,plan.delivery_ids[0],"handed_off"))
        with self.assertRaisesRegex(ControlEvidenceError,"control_plan_already_bound"):
            self.make_plan(control_id)

    def test_first_attempt_cas_is_idempotent_and_cannot_authorize_another(self):
        source,_ = self.seed()
        control_id = self.record(source)
        outbox,plan = self.make_plan(control_id)
        permit = LocalRatePermit(self.account.key,self.private.key,"c2c","passive","unverified",0.0)
        reservation = outbox.prepare(self.token,plan.delivery_ids[0],permit)
        with self.tx() as cursor:
            claim_control_attempt(cursor,self.token,control_id=control_id,attempt_id=reservation.attempt_id,now=self.now)
            claim_control_attempt(cursor,self.token,control_id=control_id,attempt_id=reservation.attempt_id,now=self.now)
            assert_control_attempt(cursor,self.token,control_id=control_id,attempt_id=reservation.attempt_id,now=self.now)
        self.assertIsNone(self.load(control_id))
        with self.tx() as cursor,self.assertRaises(ControlEvidenceError):
            claim_control_attempt(cursor,self.token,control_id=control_id,attempt_id=reservation.attempt_id+1,now=self.now)
        with self.tx() as cursor,self.assertRaises(ControlEvidenceError):
            assert_control_attempt(cursor,self.token,control_id=control_id,attempt_id=reservation.attempt_id,now=self.now+30)

    def test_unknown_attempt_keeps_one_attempt_binding_and_no_replay_authority(self):
        source,_ = self.seed()
        control_id = self.record(source)
        outbox,plan = self.make_plan(control_id)
        permit = LocalRatePermit(self.account.key,self.private.key,"c2c","passive","unverified",0.0)
        reservation = outbox.prepare(self.token,plan.delivery_ids[0],permit)
        with self.tx() as cursor:
            claim_control_attempt(cursor,self.token,control_id=control_id,attempt_id=reservation.attempt_id,now=self.now)
            cursor.execute("UPDATE delivery_attempts SET state='ambiguous' WHERE attempt_id=?",(reservation.attempt_id,))
        self.assertIsNone(self.load(control_id))
        with self.tx() as cursor,self.assertRaises(ControlEvidenceError):
            assert_control_attempt(cursor,self.token,control_id=control_id,attempt_id=reservation.attempt_id,now=self.now)


if __name__ == "__main__":
    unittest.main()
