"""Real isolated PostgreSQL tests for the official execution boundary."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import secrets


from tests.qq_official_outbox_fixture import OutboxFixture
from src.plugins.ai_chat.adapters.qq_official.ingest import turn_dispatch_key, turn_dispatch_lane
from src.plugins.ai_chat.adapters.qq_official.ingest import process_inbox
from src.plugins.ai_chat.adapters.qq_official.authority import OwnerAuthority
from src.plugins.ai_chat.adapters.qq_official.binding import BindingReducer
from src.plugins.ai_chat.adapters.qq_official.inbox import OfficialInboxStore
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.adapters.qq_official.plans import enqueue_plan
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode, body_to_json
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.turn_journal import TurnJournal

from src.plugins.ai_chat.adapters.qq_official.execution import (
    ExecutionError, ExecutionFenceLost, assert_execution_current, attach_plan,
    begin_execution, begin_command_execution, capture_result, claim_execution, complete_without_plan,
    input_text, interrupt_execution, load_result, logical_result_key,
)


def install_migration(fixture):
    """The ordinary disposable fixture must already be at the declared head."""
    row = fixture.query("SELECT to_regclass(?) AS name", (fixture.database.schema + ".qq_official_turn_executions",))[0]
    if row["name"] is None:
        raise RuntimeError("official_execution_schema_not_migrated")


class ExecutionTests(OutboxFixture):
    def setUp(self):
        super().setUp()
        install_migration(self)
        self.ledger, self.journal, self.jobs = MessageLedger(self.database), TurnJournal(self.database), DurableJobStore(self.database)
        for store in (self.ledger, self.journal, self.jobs):
            self.addCleanup(store.close)
        self.message = self.ledger.record_message(self.endpoint.scope, native_message_id="TEST_INPUT",
            sender_native_user_id=self.actor.native_user_id, sender_display="test",
            body=MessageBody((TextNode(0, "TEST question"),)), occurred_at=self.now - 5,
            actor=self.actor, source_endpoint=self.endpoint)
        self.inbox = "TEST_INBOX"
        self.kind = account_job_kind("turn", self.account.key)
        with self.tx() as cursor:
            cursor.execute("""INSERT INTO qq_official_inbound_events
                (inbox_id,account_key,endpoint_key,endpoint_generation,route_epoch,event_type,native_message_id,
                 status,echo_state,trigger_requested,canonical_message_id,received_at,created_at,updated_at,turn_text,turn_text_version)
                VALUES (?,?,?,1,1,'GROUP_AT_MESSAGE_CREATE','TEST_INPUT','processed','not_echo',TRUE,?,?,?,?,'TEST question',1)""",
                (self.inbox, self.account.key, self.endpoint.key, self.message.canonical_message_id,
                 self.now - 3, self.now - 3, self.now - 3))
            pending, _ = self.jobs.enqueue(kind=self.kind, payload={"inbox_id": self.inbox},
                scope_key=turn_dispatch_lane(self.account.key, self.endpoint.key),
                idempotency_key=turn_dispatch_key(self.account.key, self.endpoint.key, self.inbox),
                now=self.now, transaction_cursor=cursor)
            cursor.execute("UPDATE qq_official_inbound_events SET dispatch_job_id=? WHERE inbox_id=?", (pending.job_id, self.inbox))
        self.job = self.jobs.claim_due("TEST_WORKER", kinds=(self.kind,), now=self.now)[0]
        self.deadline = self.now + 3600
        self.admitted = True

    def admission(self, *_):
        return self.admitted

    def args(self, handle):
        return dict(token=self.token, handle=handle, job=self.job, now=self.now, admission=self.admission)

    def claim(self, purpose="final", kind="chat"):
        with self.tx() as cursor:
            return claim_execution(cursor, token=self.token, job=self.job, now=self.now,
                deadline_at=self.deadline, admission=self.admission, result_purpose=purpose, execution_kind=kind)

    def begin(self, handle):
        turn = self.journal.start_turn(self.endpoint.scope, trigger_canonical_message_id=self.message.canonical_message_id,
            objective="test", provider="test", model="test", started_at=self.now)
        with self.tx() as cursor:
            begin_execution(cursor, **self.args(handle), journal_turn_id=turn.turn_id)
        return turn.turn_id

    def result(self, handle, turn_id=None, text="TEST final"):
        body = MessageBody((TextNode(0, text),))
        with self.tx() as cursor:
            capture_result(cursor, **self.args(handle), body=body, turn_id=turn_id, status="succeeded")
        return body

    def reclaim_job(self):
        self.now += 301
        self.job = self.jobs.claim_due("TEST_NEW_WORKER", kinds=(self.kind,), now=self.now)[0]

    def test_prepared_can_handoff_without_reexecuting_a_started_turn(self):
        first = self.claim().handle
        self.reclaim_job()
        second = self.claim()
        self.assertEqual(second.state, "prepared")
        self.assertEqual(first.execution_id, second.handle.execution_id)
        self.assertEqual(second.handle.execution_revision, first.execution_revision + 1)
        with self.assertRaises(ExecutionFenceLost), self.tx() as cursor:
            assert_execution_current(cursor, **self.args(first))

    def test_duplicate_executing_claim_never_allows_second_begin(self):
        handle = self.claim().handle
        turn = self.begin(handle)
        self.assertEqual(self.claim().state, "already_running")
        with self.assertRaisesRegex(ExecutionError, "already_started"), self.tx() as cursor:
            begin_execution(cursor, **self.args(handle), journal_turn_id=turn)

    def test_lost_job_owner_executing_is_needs_attention_not_replayable(self):
        handle = self.claim().handle
        self.begin(handle)
        self.reclaim_job()
        result = self.claim()
        self.assertEqual((result.state, result.reason, result.handle), ("needs_attention", "execution_owner_lost", None))
        self.assertEqual(self.claim().state, "needs_attention")

    def test_new_account_epoch_cannot_resume_executing_even_same_worker(self):
        handle = self.claim().handle
        self.begin(handle)
        self.account_lease.close()
        self.token = self.lease().acquire()
        self.assertEqual(self.claim().state, "needs_attention")

    def test_result_ready_reclaims_only_frozen_result_and_plan_identity(self):
        original = self.claim().handle
        turn = self.begin(original)
        body = self.result(original, turn)
        self.reclaim_job()
        decision = self.claim()
        self.assertEqual(decision.state, "result_ready")
        handle = decision.handle
        with self.tx() as cursor:
            self.assertEqual(body_to_json(load_result(cursor, **self.args(handle))), body_to_json(body))
            plan = enqueue_plan(cursor, token=self.token, rendered=render_plan(body, self.endpoint), requester=self.actor,
                logical_key=logical_result_key(handle), source_scope_key=handle.scope_key,
                source_canonical_message_id=handle.canonical_message_id, source_native_message_id="TEST_INPUT",
                turn_id=turn, expires_at=handle.deadline_at, now=self.now, admission=self.admission)
            self.assertTrue(attach_plan(cursor, **self.args(handle), plan_id=plan.plan_id))
            self.assertFalse(attach_plan(cursor, **self.args(handle), plan_id=plan.plan_id))
        self.assertEqual((self.claim().state, self.claim().plan_id), ("handed_off", plan.plan_id))
        self.assertEqual(len(self.query("SELECT * FROM delivery_plans")), 1)

    def test_result_and_plan_rollback_share_caller_transaction(self):
        handle = self.claim().handle
        turn = self.begin(handle)
        body = MessageBody((TextNode(0, "TEST result"),))
        with self.assertRaisesRegex(RuntimeError, "rollback"), self.tx() as cursor:
            capture_result(cursor, **self.args(handle), body=body, turn_id=turn, status="succeeded")
            raise RuntimeError("rollback")
        self.assertEqual(self.query("SELECT state FROM qq_official_turn_executions")[0]["state"], "executing")

    def test_source_native_message_cannot_be_swapped_for_a_different_window(self):
        handle = self.claim().handle
        body = self.result(handle)
        self.assertEqual(handle.source_native_message_id, "TEST_INPUT")
        self.assertNotIn("TEST_INPUT", repr(handle))
        with self.assertRaisesRegex(ExecutionError, "plan_identity_conflict"), self.tx() as cursor:
            plan = enqueue_plan(cursor, token=self.token, rendered=render_plan(body, self.endpoint), requester=self.actor,
                logical_key=logical_result_key(handle), source_scope_key=handle.scope_key,
                source_canonical_message_id=handle.canonical_message_id, source_native_message_id="TEST_OTHER_WINDOW_SOURCE",
                expires_at=handle.deadline_at, now=self.now, admission=self.admission)
            attach_plan(cursor, **self.args(handle), plan_id=plan.plan_id)
        self.assertEqual(self.query("SELECT * FROM delivery_plans"), [])

    def test_result_capture_is_exactly_idempotent_and_rejects_changed_body(self):
        handle = self.claim().handle
        body = self.result(handle)
        with self.tx() as cursor:
            self.assertFalse(capture_result(cursor, **self.args(handle), body=body, turn_id=None, status="succeeded"))
        with self.assertRaisesRegex(ExecutionError, "result_conflict"):
            self.result(handle, text="DIFFERENT")

    def test_expired_job_route_revoke_and_admission_revoke_block_result_commit(self):
        handle = self.claim().handle
        turn = self.begin(handle)
        self.admitted = False
        with self.assertRaisesRegex(ExecutionFenceLost, "admission"):
            self.result(handle, turn)
        self.admitted = True
        self.query("UPDATE qq_official_endpoints SET route_epoch=route_epoch+1")
        with self.assertRaisesRegex(ExecutionFenceLost, "admission"):
            self.result(handle, turn)
        self.query("UPDATE qq_official_endpoints SET route_epoch=route_epoch-1")
        self.now += 301
        with self.assertRaisesRegex(ExecutionFenceLost, "job_lease"):
            self.result(handle, turn)

    def test_unissued_copy_wrong_job_generation_and_same_id_wrong_account_fail_closed(self):
        handle = self.claim().handle
        with self.assertRaisesRegex(ExecutionFenceLost, "unissued"), self.tx() as cursor:
            assert_execution_current(cursor, **self.args(replace(handle)))
        with self.assertRaisesRegex(ExecutionFenceLost, "job_lease"), self.tx() as cursor:
            assert_execution_current(cursor, **{**self.args(handle), "job": replace(self.job, lease_generation=self.job.lease_generation + 1)})
        other = AccountKey("qq-official", "TEST_OTHER_APP", "test")
        self.provision(EndpointKey(other, "group", "TEST_GROUP"))
        other_token = self.lease(other).acquire()
        with self.assertRaisesRegex(ExecutionFenceLost, "namespace_conflict"), self.tx() as cursor:
            assert_execution_current(cursor, **{**self.args(handle), "token": other_token})

    def test_nontrigger_history_cannot_be_upgraded_by_fabricated_dispatch_pointer(self):
        self.query("UPDATE qq_official_inbound_events SET trigger_requested=FALSE")
        with self.assertRaisesRegex(ExecutionError, "trigger_unavailable"):
            self.claim()

    def test_command_begins_without_model_journal_and_is_never_replayed(self):
        handle = self.claim(kind="command").handle
        with self.assertRaisesRegex(ExecutionError, "identity_conflict"):
            self.claim(kind="chat")
        with self.tx() as cursor:
            begin_command_execution(cursor, **self.args(handle))
        self.assertEqual(self.query("SELECT * FROM agent_turns"), [])
        self.assertEqual(self.claim(kind="command").state, "already_running")
        with self.assertRaisesRegex(ExecutionError, "already_started"), self.tx() as cursor:
            begin_command_execution(cursor, **self.args(handle))
        self.reclaim_job()
        self.assertEqual(self.claim(kind="command").state, "needs_attention")

    def test_command_result_has_no_fabricated_model_journal(self):
        handle = self.claim(kind="command").handle
        with self.tx() as cursor:
            begin_command_execution(cursor, **self.args(handle))
        self.result(handle)
        self.assertEqual(self.claim(kind="command").state, "result_ready")
        self.assertEqual(self.query("SELECT * FROM agent_turns"), [])

    def test_prepared_and_ready_deadlines_cancel_without_renewal(self):
        self.deadline = self.now + 1
        handle = self.claim().handle
        self.result(handle)
        self.now += 1
        self.assertEqual(self.claim().state, "cancelled")
        stored = self.query("SELECT * FROM qq_official_turn_executions")[0]
        self.assertEqual(stored["result_body_json"], body_to_json(MessageBody((TextNode(0, "TEST final"),))))
        self.assertIsNone(self.claim().handle)

    def test_already_expired_first_claim_records_terminal_evidence(self):
        self.deadline = self.now
        self.assertEqual(self.claim().state, "cancelled")
        self.assertEqual(self.query("SELECT state FROM qq_official_turn_executions")[0]["state"], "cancelled")

    def test_frozen_background_purpose_cannot_attach_foreground_plan(self):
        handle = self.claim("background_result").handle
        body = self.result(handle)
        self.assertEqual(handle.result_purpose, "background_result")
        with self.assertRaisesRegex(ExecutionError, "identity_conflict"):
            self.claim("final")
        with self.assertRaisesRegex(ExecutionError, "plan_identity_conflict"), self.tx() as cursor:
            plan = enqueue_plan(cursor, token=self.token, rendered=render_plan(body, self.endpoint), requester=self.actor,
                logical_key=logical_result_key(handle), source_scope_key=handle.scope_key,
                source_canonical_message_id=handle.canonical_message_id, source_native_message_id="TEST_INPUT",
                expires_at=handle.deadline_at, now=self.now, admission=self.admission)
            attach_plan(cursor, **self.args(handle), plan_id=plan.plan_id)
        self.assertEqual(self.query("SELECT * FROM delivery_plans"), [])
        with self.tx() as cursor:
            plan = enqueue_plan(cursor, token=self.token, rendered=render_plan(body, self.endpoint), requester=self.actor,
                logical_key=logical_result_key(handle), source_scope_key=handle.scope_key,
                source_canonical_message_id=handle.canonical_message_id, source_native_message_id="TEST_INPUT",
                purpose="background_result", expires_at=handle.deadline_at, now=self.now, admission=self.admission)
            attach_plan(cursor, **self.args(handle), plan_id=plan.plan_id)
        self.assertEqual(self.claim("background_result").state, "handed_off")

    def test_invalid_result_encoding_never_reaches_database(self):
        handle = self.claim().handle
        for text in ("TEST_SECRET\x00tail", "TEST_SECRET\ud800tail", "x" * 262145):
            with self.subTest(kind=len(text)), self.assertRaisesRegex(ExecutionError, "^invalid_execution_result$"):
                self.result(handle, text=text)
        self.assertEqual(self.query("SELECT state FROM qq_official_turn_executions")[0]["state"], "prepared")
        body = self.result(handle, text=r"literal \u0000 is text")
        with self.tx() as cursor:
            self.assertEqual(load_result(cursor, **self.args(handle)), body)

    def test_canonical_body_mutation_cannot_change_a_prepared_execution(self):
        self.claim()
        self.query("UPDATE messages SET body_json=? WHERE canonical_message_id=?",
                   (body_to_json(MessageBody((TextNode(0, "changed"),))), self.message.canonical_message_id))
        with self.assertRaisesRegex(ExecutionError, "identity_conflict"):
            self.claim()

    def test_missing_or_wrong_journal_cannot_mark_model_started(self):
        handle = self.claim().handle
        with self.assertRaisesRegex(ExecutionError, "journal_required"), self.tx() as cursor:
            begin_execution(cursor, **self.args(handle), journal_turn_id=None)
        wrong = self.journal.start_turn(self.endpoint.scope, trigger_canonical_message_id=None,
            objective="test", provider="test", model="test", started_at=self.now)
        with self.assertRaisesRegex(ExecutionError, "journal_identity"), self.tx() as cursor:
            begin_execution(cursor, **self.args(handle), journal_turn_id=wrong.turn_id)

    def test_normalized_source_text_is_frozen_and_old_missing_text_is_rejected(self):
        handle = self.claim().handle
        with self.tx() as cursor:
            self.assertEqual(input_text(cursor, **self.args(handle)), "TEST question")
        self.query("UPDATE qq_official_inbound_events SET turn_text='TEST changed command'")
        with self.assertRaisesRegex(ExecutionFenceLost, "owner_changed"), self.tx() as cursor:
            input_text(cursor, **self.args(handle))
        self.query("UPDATE qq_official_inbound_events SET turn_text=NULL,turn_text_version=NULL")
        with self.assertRaisesRegex(ExecutionError, "turn_text_unavailable"):
            self.claim()

    def test_pre_model_quota_denial_and_silent_result_need_no_fake_journal_or_send(self):
        handle = self.claim().handle
        with self.tx() as cursor:
            capture_result(cursor, **self.args(handle), body=MessageBody(), turn_id=None, status="silence")
            self.assertTrue(complete_without_plan(cursor, **self.args(handle)))
            self.assertFalse(complete_without_plan(cursor, **self.args(handle)))
        self.assertEqual(self.claim().state, "handed_off")
        self.assertEqual(self.query("SELECT * FROM agent_turns"), [])
        self.assertEqual(self.query("SELECT * FROM deliveries"), [])

    def test_interrupt_never_discards_result_or_reopens_execution(self):
        handle = self.claim().handle
        self.begin(handle)
        with self.tx() as cursor:
            self.assertEqual(interrupt_execution(cursor, **self.args(handle), reason="worker_stopped"), "needs_attention")
        self.assertEqual(self.claim().state, "needs_attention")
        with self.assertRaisesRegex(ExecutionFenceLost, "not_current"), self.tx() as cursor:
            assert_execution_current(cursor, **self.args(handle))

    def test_deadline_not_refreshed_on_reclaim(self):
        self.claim()
        self.deadline += 1
        with self.assertRaisesRegex(ExecutionError, "identity_conflict"):
            self.claim()


class RealIngressExecutionTests(OutboxFixture):
    """Exercise actual Tx A and Tx B trigger facts, not a synthetic AT flag."""

    def setUp(self):
        super().setUp()
        install_migration(self)
        self.ledger, self.jobs = MessageLedger(self.database), DurableJobStore(self.database)
        self.addCleanup(self.ledger.close)
        self.addCleanup(self.jobs.close)
        authority = OwnerAuthority(clock=lambda: self.now)
        binding = BindingReducer(b"TEST_M4_PEPPER_LOCAL_ONLY_1234567890", epoch_guard=assert_epoch,
                                 authority=authority, clock=lambda: self.now)
        self.inbox_store = OfficialInboxStore(self.database, jobs=self.jobs, authority=authority,
                                              binding=binding, clock=lambda: self.now)
        self.addCleanup(self.inbox_store.close)
        self.owner = "TEST_OWNER_PRIVATE"
        self.inbox_store.apply_configuration(self.token, (self.owner,))
        self.session, self.seq = "TEST_SESSION_M4", 0
        self.inbox_store.accept({"op": 0, "s": 0, "t": "READY", "d": {"session_id": self.session}},
                                token=self.token, session_id=self.session)
        self.kind = account_job_kind("turn", self.account.key)

    def ingest(self, *, private=False, reference=None, own_index=None, mention=False):
        self.seq += 1
        data = {"id": "TEST_REAL_INPUT_" + str(self.seq), "content": "TEST question",
                "timestamp": datetime.fromtimestamp(self.now - 1, timezone.utc).isoformat(),
                "author": {"user_openid" if private else "member_openid": self.owner if private else "TEST_MEMBER"}}
        if not private:
            data["group_openid"] = self.endpoint.native_conversation_id
        ext = {}
        if reference is not None:
            ext["ref_msg_idx"] = reference
        if own_index is not None:
            ext["msg_idx"] = own_index
        data["message_scene"] = {"ext": ext}
        event = "C2C_MESSAGE_CREATE" if private else "GROUP_AT_MESSAGE_CREATE" if mention else "GROUP_MESSAGE_CREATE"
        frame = {"op": 0, "s": self.seq, "t": event, "id": "TEST_EVENT_M4_" + str(self.seq), "d": data}
        accepted = self.inbox_store.accept(frame, token=self.token, session_id=self.session)
        self.assertEqual(accepted.disposition, "inbox")
        with self.tx() as cursor:
            result = process_inbox(cursor, inbox_id=accepted.inbox_id, lease_token=self.token,
                ledger=self.ledger, jobs=self.jobs, epoch_guard=assert_epoch, admission=self.inbox_store.admitted, now=self.now)
            # The frozen M2 module predates this M4 column. This is the exact
            # same-Tx B persistence delta for the fixture's plain trusted text;
            # mention normalization itself has separate HTTP-agent tests.
            cursor.execute("UPDATE qq_official_inbound_events SET turn_text=?,turn_text_version=1 WHERE inbox_id=?",
                           (data["content"], accepted.inbox_id))
        self.assertEqual(result.status, "processed")
        return accepted.inbox_id, result

    def claim_ingested(self):
        job = self.jobs.claim_due("TEST_REAL_WORKER", kinds=(self.kind,), now=self.now)[0]
        with self.tx() as cursor:
            return claim_execution(cursor, token=self.token, job=job, now=self.now,
                deadline_at=self.now + 60, admission=self.inbox_store.admitted)

    def bot_receipt(self, *, endpoint=None, index="TEST_BOT_REF"):
        endpoint = endpoint or self.endpoint
        bot = ActorKey(self.account, "bot", "", "TEST_VERIFIED_BOT")
        native = "TEST_BOT_SENT_" + secrets.token_hex(4)
        message = self.ledger.record_message(endpoint.scope, native_message_id=native,
            sender_native_user_id=bot.native_user_id, sender_display="bot", body=MessageBody((TextNode(0, "TEST answer"),)),
            occurred_at=self.now - 5, actor=bot, source_endpoint=endpoint, direction="outbound")
        with self.tx() as cursor:
            cursor.execute("""INSERT INTO qq_official_messages
                (message_id,account_key,endpoint_key,native_message_id,canonical_message_id,direction,created_at)
                VALUES (?,?,?,?,?,'outbound',?)""",
                (secrets.token_hex(16), self.account.key, endpoint.key, native, message.canonical_message_id, self.now - 5))
            cursor.execute("""INSERT INTO qq_official_message_indexes
                (endpoint_key,account_key,index_kind,index_value,native_message_id,canonical_message_id,ambiguous,created_at,updated_at)
                VALUES (?,?,'ref_idx',?,?,?,FALSE,?,?)""",
                (endpoint.key, self.account.key, index, native, message.canonical_message_id, self.now - 5, self.now - 5))
        return native

    def test_real_c2c_without_at_flag_can_claim(self):
        inbox, ingested = self.ingest(private=True)
        self.assertIsNotNone(ingested.dispatch_job_id)
        self.assertFalse(self.query("SELECT trigger_requested FROM qq_official_inbound_events WHERE inbox_id=?", (inbox,))[0][0])
        claimed = self.claim_ingested()
        self.assertEqual(claimed.state, "prepared")
        self.assertEqual(claimed.handle.endpoint.kind, "private")
        self.assertEqual(claimed.handle.actor.native_user_id, self.owner)

    def test_real_group_quote_to_bot_can_claim_without_at(self):
        native = self.bot_receipt()
        inbox, ingested = self.ingest(reference="TEST_BOT_REF")
        self.assertIsNotNone(ingested.dispatch_job_id)
        row = self.query("SELECT trigger_requested,canonical_message_id FROM qq_official_inbound_events WHERE inbox_id=?", (inbox,))[0]
        self.assertFalse(row["trigger_requested"])
        self.assertEqual(self.ledger.get_in_scope(self.endpoint.scope, row["canonical_message_id"]).reply_to_native_message_id, native)
        self.assertEqual(self.claim_ingested().state, "prepared")

    def test_real_group_quote_to_human_is_not_an_agent_trigger(self):
        _, first = self.ingest(own_index="TEST_HUMAN_REF")
        self.assertIsNone(first.dispatch_job_id)
        _, reply = self.ingest(reference="TEST_HUMAN_REF")
        # Existing M2 queues any resolved reply; M4 rejects human-only quoting.
        self.assertIsNotNone(reply.dispatch_job_id)
        with self.assertRaisesRegex(ExecutionError, "trigger_unavailable"):
            self.claim_ingested()

    def test_real_quote_never_crosses_endpoint_and_ambiguous_mapping_is_rejected(self):
        other = EndpointKey(self.account, "group", "TEST_OTHER_GROUP")
        self.provision(other)
        self.bot_receipt(endpoint=other)
        _, unrelated = self.ingest(reference="TEST_BOT_REF")
        self.assertIsNone(unrelated.dispatch_job_id)
        self.bot_receipt(index="TEST_LOCAL_REF")
        _, local = self.ingest(reference="TEST_LOCAL_REF")
        self.assertIsNotNone(local.dispatch_job_id)
        self.query("UPDATE qq_official_message_indexes SET ambiguous=TRUE WHERE endpoint_key=?", (self.endpoint.key,))
        with self.assertRaisesRegex(ExecutionError, "trigger_unavailable"):
            self.claim_ingested()
