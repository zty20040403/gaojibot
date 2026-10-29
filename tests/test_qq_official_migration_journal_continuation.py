"""Actual draft journal/proofs + original domain + M3 fake transport.

Control-plane attestation and verification sources use peer's host fixtures;
all session, baseline, journal, job, receipt and domain rows are real PostgreSQL.
This does not claim a new live operator migration/file acceptance.
"""
import hashlib
import secrets
import unittest

import psycopg
from tests import test_qq_official_migration_continuation as fixture
from src.plugins.ai_chat.adapters.qq_official.migration_journal import Journal
from src.plugins.ai_chat.adapters.qq_official.migration_types import BaselineEvidence, ExportEvidence, MigrationDenied, encoded
from src.plugins.ai_chat.adapters.qq_official.migration_routes import RouteService
from tests import qq_official_migration_journal_fixture as journal_fixture
from src.plugins.ai_chat.adapters.qq_official.migration_preview import SourceRef
from src.plugins.ai_chat.platform_types import ActorKey
from tests.qq_official_outbox_fixture import OutboxFixture
from tests.qq_official_migration_mapping_fixture import install_mapping,rotate_mapping


class DomainPorts(journal_fixture.HostPorts):
    def export(self,cursor,source_endpoint,source_scope,target_endpoint,kind,source_ref,now):
        f=self.test
        value=f.preview.preview(cursor,f.selection,SourceRef(kind,source_ref),now=now)
        data=value.export or {}
        revision=1
        if kind=='routine':
            if source_ref.startswith('spec:'):
                revision=cursor.execute('SELECT version FROM agent_routines WHERE routine_id=?',(int(source_ref[5:]),)).fetchone()[0]
            else:
                revision=cursor.execute('SELECT version FROM routine_runs WHERE run_id=?',(source_ref[4:],)).fetchone()[0]
        return ExportEvidence(kind,source_ref,revision,encoded(data),value.disposition,
            f.source_actor if value.disposition=='unsent' else None,value.safe_digest,
            f.execution_deadline,f.result_deadline,schedule_json=encoded(self.schedule) if self.schedule else None)

    def mapping(self,cursor,source_endpoint,source_scope,source_actor,target_endpoint,now):
        if (source_endpoint!=self.test.source or source_scope.key!=self.test.source_scope.key
                or source_actor!=self.test.source_actor or target_endpoint!=self.test.endpoint):
            raise MigrationDenied()
        return self.test.mapping_store.lookup(cursor,source_endpoint,source_scope,source_actor,target_endpoint,now)

    def bound_job(self,cursor,item,job,**kwargs):
        return self.test.engine.validate_bound_job(cursor,item,job,**kwargs)

    def bound_plan(self,cursor,item,plan_id,**kwargs):
        return self.test.engine.validate_bound_plan(cursor,item,plan_id,**kwargs)

    def bound_schedule(self,cursor,item,routine_id,**kwargs):
        return self.test.engine.validate_bound_schedule(cursor,item,routine_id,**kwargs)

    def occurrence(self,cursor,parent,**kwargs):
        return self.test.engine.validate_occurrence(cursor,parent,**kwargs)

    def revoke(self,cursor,**kwargs):
        # No public history grant in this domain integration fixture.
        return None


class JournalContinuationTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    owned=fixture.ContinuationTests.owned
    new_engine=fixture.ContinuationTests.new_engine
    ensure_current=fixture.ContinuationTests.ensure_current
    reminder=fixture.ContinuationTests.reminder
    routine=fixture.ContinuationTests.routine
    tick=fixture.ContinuationTests.tick
    asyncTearDown=fixture.ContinuationTests.asyncTearDown

    async def asyncSetUp(self):
        await fixture.ContinuationTests.asyncSetUp(self)
        with self.tx() as cursor:
            cursor.execute("INSERT INTO admin_accounts(account_id,username,password_hash,role,qq_id,enabled,version,created_at) VALUES ('admin','test-admin','TEST_HASH','admin','9',1,1,?)",(self.now,))
            cursor.execute("INSERT INTO admin_sessions VALUES (?, 'admin',1,?,?,?)",('a'*64,'b'*64,self.now+1000,self.now))
        self.authorization,self.baseline_source,self.effect_source,self.artifact_source=object(),object(),object(),object()
        self.source_actor=ActorKey(self.source.account,'user','','201')
        self.source_connection=psycopg.connect(self.database.dsn,autocommit=True)
        self.addCleanup(self.source_connection.close)
        key=int.from_bytes(hashlib.sha256(self.source.account.key.encode()).digest()[:8],'big',signed=True)
        self.source_connection.execute('SELECT pg_advisory_lock(%s)',(key,))
        pid=self.source_connection.execute('SELECT pg_backend_pid()').fetchone()[0]
        self.baseline=BaselineEvidence(self.source.account.key,secrets.token_hex(16),pid,key,'d'*64,self.now+1000)
        self.mapping_expiry=self.now+1000
        self.execution_deadline,self.result_deadline=self.now+100,self.now+200
        self.ports=DomainPorts(self)
        self.routes=RouteService(tokens={self.account.key:self.token},ports=self.ports)
        self.migration=Journal(self.routes)
        self.journal=self.migration
        self.engine.journal=self.migration
        self.pepper=b'TEST_PEPPER_0123456789012345678901234'
        self.verifications={}
        self.verification_request_source=object()
        self.verification_plans={}
        with self.tx() as cursor:
            self.routes.register_baseline(cursor,account=self.source.account,source=self.baseline_source,now=self.now)
        install_mapping(self)

    def rotate_mapping(self):
        return rotate_mapping(self)

    def start(self,source):
        with self.tx() as cursor:
            plan=self.migration.plan(cursor,source_endpoint=self.source,source_scope=self.source_scope,
                target_endpoint=self.endpoint,items=((source.kind,source.key),),authorization=self.authorization,
                baseline_source=self.baseline_source,deadline=self.now+500,now=self.now)
        journal_fixture.JournalFixture.switch(self,plan)
        return plan

    freeze=journal_fixture.JournalFixture.freeze

    def convert(self,plan):
        item_id=self.query('SELECT item_id FROM migration_items WHERE migration_id=? AND parent_item_id IS NULL',(plan.migration_id,))[0][0]
        with self.tx() as cursor:
            proof=self.migration.issue_item(cursor,item_id=item_id,now=self.now)
            binding=self.engine.prepare(cursor,proof,now=self.now)
        return item_id,binding

    def complete(self,plan,item_id):
        journal_fixture.JournalFixture.complete(self,plan)
        self.query("UPDATE qq_official_reply_windows SET state='expired' WHERE credential_value<>'TEST_SOURCE'")
        with self.tx() as cursor:
            proof=self.migration.issue_item(cursor,item_id=item_id,operation='activate',now=self.now)
            self.engine.activate(cursor,proof,now=self.now)
        return proof

    async def test_actual_journal_parked_activation_send_receipt_and_rollback_guard(self):
        plan=self.start(self.reminder())
        item_id,binding=self.convert(plan)
        self.now+=2
        self.assertEqual(await self.tick(),0)
        self.assertEqual(self.jobs.get(binding['job_id']).attempts,0)
        with self.tx() as cursor:
            proof=self.migration.issue_item(cursor,item_id=item_id,operation='prepare',now=self.now)
            self.assertEqual(self.engine.prepare(cursor,proof,now=self.now)['job_id'],binding['job_id'])
        self.complete(plan,item_id)
        self.assertEqual(await self.tick(),1)
        saved=self.query('SELECT * FROM qq_official_schedule_bindings WHERE item_id=?',(item_id,))[0]
        self.assertIsNotNone(saved['plan_id'])
        with self.tx() as cursor:
            self.assertTrue(self.engine.current_plan_source(cursor,saved['plan_id'],self.now))
        delivery=self.query('SELECT delivery_id FROM deliveries WHERE plan_id=?',(saved['plan_id'],))[0][0]
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertEqual(len(self.fake.received),1)
        with self.tx() as cursor:
            self.migration.rollback(cursor,migration_id=plan.migration_id,authorization=self.authorization,now=self.now)
        with self.tx() as cursor:
            with self.assertRaises(MigrationDenied):
                self.engine.current_plan_source(cursor,saved['plan_id'],self.now)
        # Current route revocation has no role in replaying old accepted evidence.
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertEqual(len(self.fake.received),1)

    async def test_actual_parent_child_proof_and_original_slot_deadlines(self):
        spec=self.routines.set(self.source_scope,name='TEST actual scheduled',prompt='TEST recurring',cron='* * * * *',now=self.now)
        self.execution_deadline=self.result_deadline=self.now+400
        self.ports.schedule=dict(cron=spec.cron,timezone=spec.timezone,version=spec.version,cutover=self.now,
            schedule_until=self.now+400,max_occurrences=2,execution_seconds=90,result_seconds=200,
            capability_grant={'profile':'official-scheduled-text-v1','tools':[]})
        plan=self.start(SourceRef('routine','spec:'+str(spec.routine_id)))
        item_id,parent=self.convert(plan)
        proof=self.complete(plan,item_id)
        self.now=spec.next_due_at
        with self.tx() as cursor:
            child=self.engine.materialize_due(cursor,proof,now=self.now)
        self.assertEqual(child['execution_deadline'],spec.next_due_at+90)
        self.assertEqual(await self.tick(),1)
        self.assertEqual(self.model.await_count,1)
        children=self.query('SELECT * FROM migration_items WHERE parent_item_id=?',(item_id,))
        self.assertEqual((len(children),children[0]['state']),(1,'active'))
        saved=self.query('SELECT * FROM qq_official_schedule_bindings WHERE item_id=?',(child['item_id'],))[0]
        self.assertIsNotNone(saved['plan_id'])
        self.rotate_mapping()
        self.now+=60
        with self.tx() as cursor:
            with self.assertRaises(MigrationDenied):
                self.engine.materialize_due(cursor,proof,now=self.now)
        self.assertEqual(len(self.query('SELECT * FROM migration_items WHERE parent_item_id=?',(item_id,))),1)

    async def test_real_journal_generated_result_after_execution_deadline(self):
        plan=self.start(self.routine(generated=True))
        item_id,binding=self.convert(plan)
        self.complete(plan,item_id)
        self.now+=110
        self.assertEqual(await self.tick(),1)
        self.model.assert_not_awaited()
        saved=self.query('SELECT * FROM qq_official_schedule_bindings WHERE item_id=?',(item_id,))[0]
        self.assertIsNotNone(saved['plan_id'])
        delivery=self.query('SELECT delivery_id FROM deliveries WHERE plan_id=?',(saved['plan_id'],))[0][0]
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')

    async def test_expired_parked_real_item_cancels_without_claim(self):
        plan=self.start(self.reminder())
        item_id,binding=self.convert(plan)
        journal_fixture.JournalFixture.complete(self,plan)
        self.now=self.result_deadline
        with self.tx() as cursor:
            self.engine.expire(cursor,item_id,now=self.now)
        self.assertEqual(await self.tick(),0)
        job=self.jobs.get(binding['job_id'])
        self.assertEqual((job.status,job.attempts),('cancelled',0))
        self.assertEqual(self.query('SELECT state FROM migration_items WHERE item_id=?',(item_id,))[0][0],'expired')

    async def test_queued_real_occurrence_past_execution_deadline_never_calls_model(self):
        plan=self.start(self.routine())
        item_id,binding=self.convert(plan)
        self.complete(plan,item_id)
        self.now=self.execution_deadline+1
        await self.tick()
        self.model.assert_not_awaited()
        self.assertEqual(self.routines.get_run(binding['target_run_id']).last_error,'migration_deadline_expired')

    async def test_owned_real_job_past_result_deadline_can_settle_without_reexecution(self):
        plan=self.start(self.reminder())
        item_id,binding=self.convert(plan)
        self.complete(plan,item_id)
        self.now=self.result_deadline
        self.assertEqual(await self.tick(),1)
        self.assertEqual(self.jobs.get(binding['job_id']).result['status'],'expired')
        self.assertEqual(self.query('SELECT status FROM reminders WHERE reminder_id=?',(binding['target_reminder_id'],))[0][0],'cancelled')
        with self.tx() as cursor:
            self.engine.expire(cursor,item_id,now=self.now)
