"""Real TxA receipts -> immutable source -> TxB/execution/M3 route fences."""
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official.binding import BindingReducer
from src.plugins.ai_chat.adapters.qq_official.inbox import OfficialInboxStore
from src.plugins.ai_chat.adapters.qq_official.ingest import process_inbox
from src.plugins.ai_chat.adapters.qq_official.execution import claim_execution
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.migration_native_sources import NativeSources, SourceAdmission
from src.plugins.ai_chat.adapters.qq_official.migration_host_ports import MigrationPlanGuard
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from tests.qq_official_migration_journal_fixture import JournalFixture


class NativeSourcesTests(JournalFixture):
    def setUp(self):
        super().setUp()
        self.sources = NativeSources(self.routes)
        self.inbox_store = OfficialInboxStore(self.database,jobs=self.jobs,
            binding=BindingReducer(self.pepper,epoch_guard=assert_epoch,clock=lambda:self.now),clock=lambda:self.now)
        self.addCleanup(self.inbox_store.close)
        self.inbox_store.source_authority = self.sources
        self.inbox_store.admitted = SourceAdmission(self.inbox_store.admitted,self.sources)
        self.outbox = OfficialSendStore(self.deliveries,admission=self.inbox_store.admitted,
            pepper=self.pepper,clock=lambda:self.now,
            source_guard=MigrationPlanGuard(native=self.sources,journal=self.migration))
        self.seq, self.session = 0, 'TEST_NATIVE_SOURCE_SESSION'
        self.inbox_store.accept({'op':0,'s':0,'t':'READY','d':{'session_id':self.session}},
            token=self.token,session_id=self.session)

    def frame(self, *, native=None, content='TEST ordinary question', actor=None):
        self.seq += 1
        return {'op':0,'s':self.seq,'t':'GROUP_AT_MESSAGE_CREATE','id':'TEST_EVENT_'+str(self.seq),
            'd':{'id':native or 'TEST_MESSAGE_'+str(self.seq),'group_openid':self.endpoint.native_conversation_id,
                'author':{'member_openid':actor or self.actor.native_user_id},'content':content,
                'timestamp':datetime.fromtimestamp(self.now,timezone.utc).isoformat()}}

    def accept(self, **kwargs):
        frame = self.frame(**kwargs)
        return self.inbox_store.accept(frame,token=self.token,session_id=self.session)

    def ingest(self, received):
        with self.tx() as cursor:
            return process_inbox(cursor,inbox_id=received.inbox_id,lease_token=self.token,
                ledger=self.ledger,jobs=self.jobs,epoch_guard=assert_epoch,
                admission=self.inbox_store.admitted,now=self.now)

    def enqueue(self, received, **overrides):
        row = self.query('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?',(received.inbox_id,))[0]
        args = dict(rendered=render_plan(MessageBody((TextNode(0,'TEST result'),)),self.endpoint),
            requester=self.actor,logical_key='TEST_RESULT:'+received.inbox_id,source_scope_key=self.endpoint.scope.key,
            source_native_message_id=row['native_message_id'],source_canonical_message_id=row['canonical_message_id'],
            expires_at=self.now+100)
        return self.outbox.enqueue(self.token,**{**args,**overrides})

    def test_authenticated_source_survives_payload_expiry_and_inherits_original_epoch(self):
        accepted=self.accept();result=self.ingest(accepted)
        self.assertEqual(result.status,'processed')
        source=self.query('SELECT * FROM qq_migration_native_sources')[0]
        self.assertEqual((source['inbox_id'],source['source_seq'],source['logical_route_epoch']),
            (accepted.inbox_id,1,0))
        job=self.jobs.claim_due('TEST_SOURCE_WORKER',kinds=(account_job_kind('turn',self.account.key),),now=self.now)[0]
        with self.tx() as c:
            decision=claim_execution(c,token=self.token,job=job,now=self.now,deadline_at=self.now+200,
                admission=self.inbox_store.admitted)
            self.assertIsNotNone(decision.handle)
            c.execute('UPDATE qq_official_inbound_events SET payload_json=NULL WHERE inbox_id=?',(accepted.inbox_id,))
        plan=self.enqueue(accepted)
        self.assertEqual(self.query('SELECT inbox_id FROM qq_migration_native_plan_sources WHERE plan_id=?',(plan.plan_id,))[0]['inbox_id'],accepted.inbox_id)
        # Reconstructing the service after a restart reads durable evidence; it
        # never refreshes the original logical epoch from the active route.
        self.outbox.source_guard=MigrationPlanGuard(native=NativeSources(self.routes),journal=self.migration)
        with self.tx() as c:
            stored=c.execute('SELECT * FROM delivery_plans WHERE plan_id=?',(plan.plan_id,)).fetchone()
            self.assertTrue(self.outbox.source_guard(c,token=self.token,plan=stored,operation='send',now=self.now))

    def test_pending_input_does_not_cross_freeze_and_duplicate_cannot_resign(self):
        accepted=self.accept();plan=self.plan();self.freeze(plan)
        result=self.ingest(accepted)
        self.assertEqual((result.status,result.reason_code),('revoked','migration_source_unavailable'))
        duplicate=self.accept(native='TEST_MESSAGE_1')
        self.assertEqual(duplicate.disposition,'duplicate')
        self.assertEqual(len(self.query('SELECT * FROM qq_migration_native_sources')),1)
        self.assertFalse(self.query('SELECT * FROM messages WHERE native_message_id=?',('TEST_MESSAGE_1',)))


    def test_old_unregistered_source_and_wrong_actor_cannot_create_plan(self):
        self.inbox_store.source_authority=None
        accepted=self.accept()
        with patch.object(self.inbox_store.admitted,'require_inbox',return_value=None):
            self.ingest(accepted)
        with self.assertRaises(MigrationDenied):self.enqueue(accepted)
        self.assertFalse(self.query('SELECT * FROM delivery_plans'))
        self.inbox_store.source_authority=self.sources
        accepted=self.accept();self.ingest(accepted)
        with self.assertRaises(MigrationDenied):
            self.enqueue(accepted,requester=replace(self.actor,native_user_id='TEST_OTHER_ACTOR'))
        self.assertFalse(self.query('SELECT * FROM delivery_plans'))

    def test_source_and_original_receipt_rollback_together(self):
        before=len(self.query('SELECT * FROM qq_official_inbound_events'))
        actual=self.sources.record
        def fail(*args,**kwargs):
            actual(*args,**kwargs)
            raise RuntimeError('TEST_ROLLBACK')
        with patch.object(self.sources,'record',side_effect=fail):
            with self.assertRaises(RuntimeError):self.accept()
        self.assertFalse(self.query('SELECT * FROM qq_migration_native_sources'))
        self.assertFalse(self.query('SELECT * FROM qq_official_gateway_receipts WHERE seq=1'))
        self.assertEqual(len(self.query('SELECT * FROM qq_official_inbound_events')),before)

    def test_source_evidence_cannot_be_changed_or_deleted(self):
        accepted=self.accept()
        import psycopg
        from src.bot_storage.database import DatabaseError
        for sql in ("UPDATE qq_migration_native_sources SET logical_route_epoch=1 WHERE inbox_id=?",
                    "DELETE FROM qq_migration_native_sources WHERE inbox_id=?"):
            with self.assertRaises(DatabaseError) as error:
                with self.tx() as c:c.execute(sql,(accepted.inbox_id,))
            self.assertIsInstance(error.exception.__cause__,psycopg.errors.RaiseException)

    def test_frozen_route_consumes_secret_control_without_reducer_or_plaintext(self):
        migration=self.plan();self.freeze(migration)
        with patch.object(self.inbox_store.binding,'apply',side_effect=AssertionError('must not reduce')):
            accepted=self.accept(content='/bind submit TEST_CODE_NEVER_STORED')
        self.assertEqual(accepted.reason,'migration_route_unavailable')
        self.assertNotIn('TEST_CODE_NEVER_STORED',repr(self.query('SELECT * FROM qq_official_inbound_events')))
        self.assertFalse(self.query('SELECT * FROM qq_migration_native_sources'))

    def test_physical_generation_change_rejects_original_logical_source(self):
        accepted=self.accept();self.ingest(accepted)
        with self.tx() as c:c.execute('UPDATE qq_official_endpoints SET generation=generation+1 WHERE endpoint_key=?',(self.endpoint.key,))
        with self.assertRaises(MigrationDenied):self.enqueue(accepted)
