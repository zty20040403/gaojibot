"""Existing domain + queue + renderer/outbox/sender/FakePlatform, isolated PG.

The isolated policy fixture also persists formal route/item foreign keys; actual
Journal integration is covered in test_qq_official_migration_journal_continuation.
It persists phases/versions/jobs;
DTO construction or a stale previously issued proof cannot grant execution.
"""
from concurrent.futures import ThreadPoolExecutor
import threading
import secrets
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import nonebot
nonebot.init()
from src.plugins.ai_chat.adapters.qq_official.migration_continuation import ContinuationDenied, PARKED, SharedScheduledModel
from src.plugins.ai_chat.adapters.qq_official.migration_recurring import ScheduledContinuations,schedule_of,schedule_digest
from src.plugins.ai_chat.adapters.qq_official.migration_preview import MappingEvidence, Previewer, Selection, SourceRef
from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.outbox import OfficialOutbox
from src.plugins.ai_chat.adapters.qq_official.sender import OfficialSender
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode, body_to_json
from src.plugins.ai_chat.model_catalog import ModelProfile
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey
from src.plugins.ai_chat.quota import UsageStore
from src.plugins.ai_chat.reminders import ReminderStore
from src.plugins.ai_chat.routines import RoutineStore
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker
from src.qq_official.credentials import Credentials
from src.qq_official.http import RestClient
from src.qq_official.testing.fake_platform import FakePlatform
from src.qq_official.token import TokenManager
from tests.qq_official_outbox_fixture import OutboxFixture


class JournalFixture:
    def __init__(self, fixture):
        self.fixture = fixture
        self.issued = {}
        self.grants = {}
        self.route_id = secrets.token_hex(16)
        self.migration_id = secrets.token_hex(16)
        with fixture.tx() as cursor:
            cursor.execute('''CREATE TABLE IF NOT EXISTS fixture_migrations(item_id TEXT PRIMARY KEY,
                phase TEXT NOT NULL,revision BIGINT NOT NULL,mapping_revision BIGINT NOT NULL,
                job_id BIGINT REFERENCES durable_jobs(job_id),kind TEXT)''')

    def create(self, source, *, execution_deadline=None, result_deadline=None):
        f = self.fixture
        with f.tx() as cursor:
            export = f.preview.preview(cursor, f.selection, source, now=f.now)
        proof = SimpleNamespace(source_revision=1,item_id=secrets.token_hex(16),item_revision=1,
            export_digest=export.safe_digest, source_kind=source.kind,source_key=source.key,
            source_endpoint=f.source,source_scope=f.source_scope,target_endpoint=f.endpoint,
            target_actor=f.actor,mapping_id='a'*32,mapping_revision=1,
            logical_route_epoch=7,endpoint_route_epoch=1,endpoint_generation=1,
            execution_deadline=execution_deadline or f.now+100,
            result_deadline=result_deadline or f.now+200,
            source_canonical_message_id=None,source_native_message_id=None,schedule_json=None)
        self.issued[id(proof)] = proof
        self.grants[proof.item_id] = proof
        with f.tx() as cursor:
            self.persist_item(cursor, proof, export.export_json or "{}")
            cursor.execute("INSERT INTO fixture_migrations VALUES (?,'switching',1,1,NULL,NULL)", (proof.item_id,))
        return proof

    def persist_item(self, cursor, proof, export_json, *, parent=None, job_id=None, kind=None):
        """Create real FK parents; the fixture's issued-object checks remain explicit."""
        f = self.fixture
        cursor.execute("""INSERT INTO admin_accounts
            (account_id,username,password_hash,role,qq_id,enabled,version,created_at)
            VALUES ('fixture_admin','fixture_admin','TEST_HASH','admin','9',1,1,?)
            ON CONFLICT DO NOTHING""", (f.now,))
        cursor.execute("""INSERT INTO migration_runtime_baselines
            (account_key,runtime_id,revision,backend_pid,advisory_key,deployment_digest,expires_at,ready_at,state)
            VALUES (?, ?,1,pg_backend_pid(),0,?,?,?,'ready') ON CONFLICT DO NOTHING""",
            (f.source.account.key,self.route_id,'d'*64,f.now+86400,f.now))
        cursor.execute("""INSERT INTO migration_routes
            (route_id,source_scope_key,source_endpoint_key,target_scope_key,target_endpoint_key,
             source_account_key,target_account_key,target_generation,target_endpoint_epoch,
             epoch,phase,active_endpoint_key,baseline_runtime_id,baseline_revision)
            VALUES (?,?,?,?,?,?,?,1,1,7,'switching',?,?,1) ON CONFLICT DO NOTHING""",
            (self.route_id,f.source_scope.key,f.source.key,f.endpoint.scope.key,f.endpoint.key,
             f.source.account.key,f.account.key,f.endpoint.key,self.route_id))
        cursor.execute("""INSERT INTO migration_journals
            (migration_id,route_id,preview_digest,preview_json,creator_id,creator_version,deadline,created_at)
            VALUES (?,?,?,'{}','fixture_admin',1,?,?) ON CONFLICT DO NOTHING""",
            (self.migration_id,self.route_id,'d'*64,f.now+86400,f.now))
        cursor.execute("""INSERT INTO migration_items
            (item_id,migration_id,parent_item_id,occurrence_key,kind,source_ref,source_revision,
             classification,export_json,export_digest,execution_deadline,result_deadline,
             job_id,expected_job_kind,created_at)
            VALUES (?,?,?,?,?,?,1,'unsent',?,?,?,?,?,?,?)""",
            (proof.item_id,self.migration_id,parent.item_id if parent else None,
             proof.source_key if parent else None,proof.source_kind,proof.source_key,export_json,
             proof.export_digest,proof.execution_deadline,proof.result_deadline,job_id,kind,f.now))

    def require_item(self, cursor, proof, *, operation, job=None, now, plan_id=None):
        if self.issued.get(id(proof)) is not proof:
            raise ContinuationDenied()
        r = cursor.execute('SELECT * FROM fixture_migrations WHERE item_id=? FOR UPDATE', (proof.item_id,)).fetchone()
        phases = {'prepare':{'switching','verifying'},'activate':{'completed'},'execute':{'completed'},'send':{'completed'},'deliver':{'completed'},'expire':{'completed'}}
        if not r or r['phase'] not in phases[operation] or r['revision']!=proof.item_revision or r['mapping_revision']!=proof.mapping_revision:
            raise ContinuationDenied()
        if job is not None and (r['job_id']!=job.job_id or r['kind']!=job.kind):
            raise ContinuationDenied()

    def issue_item(self, cursor, *, item_id, operation='prepare', job=None, now, plan_id=None):
        proof = self.grants[item_id]
        self.require_item(cursor,proof,operation=operation,job=job,now=now)
        return proof

    def bind_item_job(self, cursor, proof, *, job_id, expected_kind, now):
        self.require_item(cursor,proof,operation='prepare',now=now)
        job = cursor.execute('SELECT * FROM durable_jobs WHERE job_id=?', (job_id,)).fetchone()
        if not job or job['status']!='pending' or job['attempts']!=0 or job['next_attempt_at']!=PARKED or job['kind']!=expected_kind:
            raise ContinuationDenied()
        cursor.execute('UPDATE fixture_migrations SET job_id=?,kind=? WHERE item_id=? AND job_id IS NULL', (job_id,expected_kind,proof.item_id))
        if cursor.rowcount != 1:
            raise ContinuationDenied()
        cursor.execute('UPDATE migration_items SET job_id=?,expected_job_kind=? WHERE item_id=?',
            (job_id,expected_kind,proof.item_id))

    def activate_item(self,cursor,proof,*,due_at,now):
        self.require_item(cursor,proof,operation='activate',now=now)
        record=cursor.execute('SELECT job_id FROM fixture_migrations WHERE item_id=?',(proof.item_id,)).fetchone()
        cursor.execute('UPDATE durable_jobs SET next_attempt_at=?,updated_at=? WHERE job_id=? AND next_attempt_at=? AND attempts=0',(due_at,now,record['job_id'],PARKED))
        if cursor.rowcount!=1:
            raise ContinuationDenied()

    def bind_item_plan(self,cursor,proof,*,plan_id,job,now):
        self.require_item(cursor,proof,operation='send',job=job,now=now)
        if not cursor.execute('SELECT 1 FROM qq_official_schedule_bindings WHERE item_id=? AND job_id=? AND plan_id=?',(proof.item_id,job.job_id,plan_id)).fetchone():
            raise ContinuationDenied()

    def expire_item(self,cursor,proof,*,now):
        self.require_item(cursor,proof,operation='expire',now=now)
        cursor.execute('''UPDATE durable_jobs SET status='cancelled',finished_at=?,updated_at=?
            WHERE job_id=(SELECT job_id FROM fixture_migrations WHERE item_id=?) AND status='pending' ''',(now,now,proof.item_id))

    def bind_schedule(self,cursor,proof,*,target_routine_id,now):
        self.require_item(cursor,proof,operation='prepare',now=now)
        if not cursor.execute('SELECT 1 FROM qq_official_schedule_bindings WHERE item_id=? AND target_routine_id=? AND job_id IS NULL',(proof.item_id,target_routine_id)).fetchone():
            raise ContinuationDenied()

    def activate_schedule(self,cursor,proof,*,now):
        self.require_item(cursor,proof,operation='activate',now=now)

    def add_occurrence(self,cursor,parent,*,run_id,scheduled_at,job_id,expected_kind,execution_deadline,result_deadline,now):
        self.require_item(cursor,parent,operation='activate',now=now)
        value=self.fixture.engine.validate_occurrence(cursor,parent,run_id=run_id,scheduled_at=scheduled_at,job_id=job_id,now=now)
        schedule=schedule_of(parent)
        if value!=dict(run_id=run_id,scheduled_at=scheduled_at,job_id=job_id,expected_kind=expected_kind,schedule_digest=schedule_digest(schedule)):
            raise ContinuationDenied()
        if execution_deadline!=min(scheduled_at+schedule['execution_seconds'],schedule['schedule_until']) or result_deadline!=min(scheduled_at+schedule['result_seconds'],schedule['schedule_until']):
            raise ContinuationDenied()
        child=SimpleNamespace(**vars(parent))
        child.item_id=secrets.token_hex(16)
        child.source_key='run:'+run_id
        child.schedule_json=None
        child.execution_deadline=execution_deadline
        child.result_deadline=result_deadline
        child.export_digest=__import__('hashlib').sha256((parent.export_digest+run_id).encode()).hexdigest()
        self.issued[id(child)]=child
        self.grants[child.item_id]=child
        self.persist_item(cursor,child,"{}",parent=parent,job_id=job_id,kind=expected_kind)
        cursor.execute("INSERT INTO fixture_migrations VALUES (?,'completed',1,1,?,?)",(child.item_id,job_id,expected_kind))
        return child

    def phase(self, proof, phase):
        self.fixture.query('UPDATE fixture_migrations SET phase=? WHERE item_id=?',(phase,proof.item_id))


class ContinuationTests(OutboxFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = DatabaseCalls()
        self.jobs = self.owned(DurableJobStore(self.database, lease_seconds=20))
        self.deliveries = self.owned(DeliveryStore(self.database, recover_interrupted=False))
        self.ledger = self.owned(MessageLedger(self.database))
        self.usage = self.owned(UsageStore(self.database))
        self.reminders = self.owned(ReminderStore(self.database))
        self.routines = self.owned(RoutineStore(self.database,jobs=self.jobs,outbox=self.deliveries,usage=self.usage))
        self.outbox = OfficialOutbox(self.deliveries,admission=lambda c,e,a: e==self.endpoint and a==self.actor,
            pepper=b'TEST_PEPPER'*5,clock=lambda:self.now,allow_newer_windows=True)
        with self.tx() as cursor:
            observe_ready(cursor,self.token,{'op':0,'t':'READY','d':{'session_id':'TEST_SESSION','user':{'id':'TEST_BOT','bot':True}}},self.now)
        self.window()
        self.fake = FakePlatform(clock=lambda:self.now)
        self.fake.app_id=self.account.app_id
        await self.fake.start()
        self.fake.add_window('group',self.endpoint.native_conversation_id,'TEST_SOURCE')
        self.tokens=TokenManager(Credentials(self.fake.app_id,self.fake.app_secret),self.fake.base_url,allow_test_server=True)
        self.rest=RestClient(self.tokens,allow_test_server=True)
        self.sender=OfficialSender(self.account,self.token,self.outbox,self.rest,self.ledger,
            database_calls=self.calls,ensure_current=self.ensure_current,wall_clock=lambda:self.now)
        self.source=EndpointKey(AccountKey('onebot-v11','301','test'),'group','101')
        self.source_scope=ConversationScope('onebot-v11','group','101','201','301')
        self.selection=Selection(self.source,self.source_scope,self.endpoint)
        self.preview=Previewer(route_guard=lambda *args:True,
            actor_mapping=lambda c,s,r,a:MappingEvidence('a'*32,1,a,self.endpoint,self.actor),
            project_guard=lambda *args:False)
        self.journal=JournalFixture(self)
        self.model=AsyncMock(return_value=MessageBody((TextNode(0,'TEST scheduled result'),)))
        self.engine=self.new_engine()
        self.logger=SimpleNamespace(error=lambda *x:None,warning=lambda *x:None,info=lambda *x:None)
        self.worker=DurableJobWorker(self.jobs,logger=self.logger,database_calls=self.calls,worker_id='TEST_WORKER')
        for kind in ('reminder','routine'):
            self.worker.register(account_job_kind(kind,self.account.key),self.engine.execute)

    def owned(self,obj):
        self.addCleanup(obj.close)
        return obj

    def new_engine(self):
        return ScheduledContinuations(self.database,token=self.token,jobs=self.jobs,usage=self.usage,
            outbox=self.outbox,journal=self.journal,preview=self.preview,clock=lambda:self.now,run_model=self.model,database_calls=self.calls)

    async def ensure_current(self):
        with self.tx() as cursor:
            assert_epoch(cursor,self.token)

    async def asyncTearDown(self):
        await self.sender.stop()
        await self.calls.close()
        await self.rest.aclose()
        await self.tokens.aclose()
        await self.fake.close()

    def reminder(self):
        r=self.reminders.create(self.source_scope,creator_native_user_id='201',creator_principal_id=None,
            message='TEST reminder text',scheduled_for=self.now+1,now=self.now)
        return SourceRef('reminder',str(r.reminder_id))

    def routine(self,*,generated=False,**changes):
        spec=self.routines.set(self.source_scope,name='TEST routine',prompt='TEST summarize text',
            cron='* * * * *',now=self.now,**changes)
        run=self.routines.materialize_due(now=spec.next_due_at)[0]
        if generated:
            self.query("UPDATE routine_runs SET status='generated',body_json=? WHERE run_id=?",
                (body_to_json(MessageBody((TextNode(0,'TEST frozen result'),))),run.run_id))
        self.query('UPDATE routine_runs SET deadline_at=? WHERE run_id=?',(self.now+100,run.run_id))
        return SourceRef('routine','run:'+run.run_id)

    def prepare(self,source,**kwargs):
        proof=self.journal.create(source,**kwargs)
        with self.tx() as cursor:
            binding=self.engine.prepare(cursor,proof,now=self.now)
        return proof,binding

    def activate(self,proof):
        self.journal.phase(proof,'completed')
        with self.tx() as cursor:
            self.engine.activate(cursor,proof,now=self.now)

    async def tick(self):
        with patch('src.plugins.ai_chat.storage.jobs.time.time',return_value=self.now):
            return await self.worker.run_once()

    def delivery(self):
        return self.query('SELECT * FROM deliveries')[0]

    async def test_reminder_parked_then_existing_worker_and_real_m3_send(self):
        proof,binding=self.prepare(self.reminder())
        self.now+=2
        self.assertEqual(await self.tick(),0)
        self.assertEqual(self.jobs.get(binding['job_id']).attempts,0)
        self.assertEqual(self.reminders.claim_due(now=self.now),[])
        with self.tx() as cursor:
            with self.assertRaises(ContinuationDenied):
                self.engine.activate(cursor,proof,now=self.now)
        self.activate(proof)
        self.assertEqual(await self.tick(),1)
        self.assertEqual(self.jobs.get(binding['job_id']).status,'succeeded')
        delivery=self.delivery()
        manifest=__import__('json').loads(self.query('SELECT manifest_json FROM delivery_plans')[0]['manifest_json'])
        self.assertEqual(manifest['source_native_message_id'],'')
        self.assertIsNone(self.query('SELECT source_canonical_message_id FROM delivery_plans')[0][0])
        result=await self.sender.send_delivery(delivery['delivery_id'])
        self.assertEqual(result.status,'committed')
        self.assertEqual(len(self.fake.received),1)
        self.assertEqual(await self.tick(),0)
        self.assertEqual((await self.sender.send_delivery(delivery['delivery_id'])).status,'committed')
        self.assertEqual(len(self.fake.received),1)
        self.model.assert_not_awaited()

    async def test_routine_existing_domain_run_worker_and_shared_loop(self):
        source=self.routine()
        proof,binding=self.prepare(source)
        self.activate(proof)
        self.now=binding['due_at']
        profile=ModelProfile('TEST','TEST','openai-chat','TEST','http://127.0.0.1:1',api_key_required=False)
        self.engine.run_model=SharedScheduledModel(lambda work:profile)
        with patch('src.plugins.ai_chat.agent_loop.ask_deepseek',new=AsyncMock(return_value='TEST real shared loop')) as provider:
            self.assertEqual(await self.tick(),1)
        self.assertEqual(provider.await_count,1)
        run=self.routines.get_run(binding['target_run_id'])
        self.assertEqual(run.status,'enqueued')
        self.assertEqual(run.spec.scope,self.endpoint.scope)
        self.assertEqual(len(self.query('SELECT * FROM routine_budget_reservations')),1)
        self.assertEqual((await self.sender.send_delivery(self.delivery()['delivery_id'])).status,'committed')
        self.assertEqual(len(self.fake.received),1)
        self.assertTrue(all(r.spec.platform=='onebot-v11' for r in self.routines.materialize_due(now=self.now+3600)))

    async def test_generated_routine_reuses_body_after_execution_deadline(self):
        proof,binding=self.prepare(self.routine(generated=True))
        self.now+=110
        self.activate(proof)
        self.assertEqual(await self.tick(),1)
        self.model.assert_not_awaited()
        self.assertEqual(self.routines.get_run(binding['target_run_id']).deadline_at,proof.execution_deadline)
        self.assertEqual((await self.sender.send_delivery(self.delivery()['delivery_id'])).status,'committed')

    async def test_claimed_model_without_capture_is_not_reexecuted_on_restart(self):
        proof,binding=self.prepare(self.routine())
        self.activate(proof)
        self.now=binding['due_at']
        job=self.jobs.claim_due('OLD',now=self.now,kinds=(account_job_kind('routine',self.account.key),))[0]
        self.assertEqual(self.engine.begin(job)[0],'execute')
        self.jobs.recover_expired_leases(now=self.now+21)
        self.now+=21
        replacement=self.jobs.claim_due('NEW',now=self.now,kinds=(job.kind,))[0]
        restarted=self.new_engine()
        self.assertEqual((await restarted.execute(replacement))['status'],'needs_attention')
        self.model.assert_not_awaited()
        self.assertFalse(self.query('SELECT * FROM deliveries'))
        self.assertEqual(self.routines.get_run(binding['target_run_id']).last_error,'migration_execution_unknown')

    async def test_unknown_old_reminder_never_creates_target(self):
        source=self.reminder()
        self.query("UPDATE reminders SET status='sending',attempts=1,lease_until=? WHERE reminder_id=?",(self.now-1,int(source.key)))
        proof=self.journal.create(source)
        with self.tx() as cursor:
            with self.assertRaises(ContinuationDenied):
                self.engine.prepare(cursor,proof,now=self.now)
        self.assertFalse(self.query('SELECT * FROM qq_official_schedule_bindings'))

    async def test_result_deadline_expired_before_completed_cancels_without_claim(self):
        proof,binding=self.prepare(self.reminder())
        self.now=proof.result_deadline
        self.activate(proof)
        self.assertEqual(await self.tick(),0)
        job=self.jobs.get(binding['job_id'])
        self.assertEqual((job.status,job.attempts),('cancelled',0))

    async def test_revoked_mapping_and_forged_proof_fail_closed(self):
        source=self.reminder()
        proof=self.journal.create(source)
        forged=SimpleNamespace(**vars(proof))
        with self.tx() as cursor:
            with self.assertRaises(ContinuationDenied):
                self.engine.prepare(cursor,forged,now=self.now)
        with self.tx() as cursor:
            binding=self.engine.prepare(cursor,proof,now=self.now)
        self.activate(proof)
        self.query('UPDATE fixture_migrations SET mapping_revision=2 WHERE item_id=?',(proof.item_id,))
        self.now+=2
        job=self.jobs.claim_due('TEST',now=self.now,kinds=(account_job_kind('reminder',self.account.key),))[0]
        with self.assertRaises(ContinuationDenied):
            await self.engine.execute(job)
        self.assertFalse(self.query('SELECT * FROM deliveries'))
        self.assertEqual(self.jobs.get(binding['job_id']).attempts,1)

    async def test_prepare_and_activation_idempotent_original_deadline_preserved(self):
        proof,binding=self.prepare(self.reminder())
        with self.tx() as cursor:
            self.assertEqual(self.engine.prepare(cursor,proof,now=self.now)['job_id'],binding['job_id'])
        self.activate(proof)
        self.now+=1
        with self.tx() as cursor:
            self.engine.activate(cursor,proof,now=self.now)
        saved=self.query('SELECT * FROM qq_official_schedule_bindings')[0]
        self.assertEqual(saved['result_deadline'],proof.result_deadline)
        self.assertEqual(len(self.query('SELECT * FROM durable_jobs')),1)

    async def test_source_freeze_rolls_back_when_binding_job_registration_fails(self):
        source=self.reminder()
        proof=self.journal.create(source)
        with patch.object(self.journal,'bind_item_job',side_effect=ContinuationDenied):
            with self.assertRaises(ContinuationDenied):
                with self.tx() as cursor:
                    self.engine.prepare(cursor,proof,now=self.now)
        self.assertEqual(self.query('SELECT status FROM reminders WHERE reminder_id=?',(int(source.key),))[0]['status'],'pending')
        self.assertFalse(self.query('SELECT * FROM qq_official_schedule_bindings'))
        self.assertFalse(self.query('SELECT * FROM durable_jobs'))

    async def test_no_window_after_handoff_waits_and_ambiguous_never_replays(self):
        proof,binding=self.prepare(self.reminder())
        self.activate(proof)
        self.now+=2
        await self.tick()
        self.outbox.allow_newer_windows=False
        self.assertEqual((await self.sender.send_delivery(self.delivery()['delivery_id'])).status,'pending')
        self.assertEqual(len(self.fake.received),0)
        self.outbox.allow_newer_windows=True
        self.now=self.delivery()['next_attempt_at']
        self.fake.drop_next_response=True
        self.assertEqual((await self.sender.send_delivery(self.delivery()['delivery_id'])).status,'ambiguous')
        self.engine=self.new_engine()
        self.assertEqual((await self.sender.send_delivery(self.delivery()['delivery_id'])).status,'ambiguous')
        self.assertEqual(len(self.fake.received),1)

    async def test_generated_capture_survives_enqueue_failure_and_restart(self):
        proof,binding=self.prepare(self.routine())
        self.activate(proof)
        self.now=binding['due_at']
        job=self.jobs.claim_due('OLD',now=self.now,kinds=(account_job_kind('routine',self.account.key),))[0]
        with patch.object(self.outbox,'enqueue',side_effect=RuntimeError('TEST unavailable')):
            with self.assertRaises(ContinuationDenied):
                await self.engine.execute(job)
        self.assertEqual(self.routines.get_run(binding['target_run_id']).status,'generated')
        self.jobs.recover_expired_leases(now=self.now+21)
        self.now+=21
        replacement=self.jobs.claim_due('NEW',now=self.now,kinds=(job.kind,))[0]
        self.assertEqual((await self.new_engine().execute(replacement))['status'],'handed_off')
        self.assertEqual(self.model.await_count,1)
        self.assertEqual((await self.sender.send_delivery(self.delivery()['delivery_id'])).status,'committed')

    async def test_plan_guard_survives_completed_job_but_rejects_revoked_mapping(self):
        proof,binding=self.prepare(self.reminder())
        self.activate(proof)
        self.now+=2
        await self.tick()
        plan_id=self.delivery()['plan_id']
        self.assertEqual(self.jobs.get(binding['job_id']).status,'succeeded')
        with self.tx() as cursor:
            self.assertTrue(self.engine.current_plan_source(cursor,plan_id,self.now))
        self.query('UPDATE fixture_migrations SET mapping_revision=2 WHERE item_id=?',(proof.item_id,))
        with self.tx() as cursor:
            with self.assertRaises(ContinuationDenied):
                self.engine.current_plan_source(cursor,plan_id,self.now)

    async def test_old_unknown_budget_counts_against_migrated_routine(self):
        source=self.routine(max_tokens=2000,daily_tokens=2000)
        first=self.routines.get_run(source.key[4:])
        self.assertTrue(self.usage.reserve_routine(run_id=first.run_id,attempt_id='TEST_OLD_UNKNOWN',now=self.now))
        self.query("UPDATE routine_runs SET status='quiet' WHERE run_id=?",(first.run_id,))
        self.query("UPDATE routine_budget_reservations SET status='unknown' WHERE attempt_id='TEST_OLD_UNKNOWN'")
        next_run=self.routines.materialize_due(now=first.scheduled_at+60)[0]
        self.query('UPDATE routine_runs SET deadline_at=? WHERE run_id=?',(self.now+190,next_run.run_id))
        proof,binding=self.prepare(SourceRef('routine','run:'+next_run.run_id),execution_deadline=self.now+190)
        self.activate(proof)
        self.now=binding['due_at']
        await self.tick()
        self.model.assert_not_awaited()
        self.assertEqual(self.routines.get_run(binding['target_run_id']).last_error,'migration_budget_exhausted')
        self.assertEqual(len(self.query('SELECT * FROM routine_budget_reservations')),1)
        view=self.usage.routine_status(binding['target_routine_id'],scope_key=self.endpoint.scope.key,
            bot_id="TEST_BOT",creator_id=self.actor.native_user_id,now=self.now)
        self.assertEqual((view['charged_tokens'],view['runs']),(2000,1))

    async def test_concurrent_old_and_new_routine_share_original_cap(self):
        source=self.routine(max_tokens=2000,daily_tokens=2000)
        old=self.routines.get_run(source.key[4:])
        proof,binding=self.prepare(source)
        self.activate(proof)
        next_run=self.routines.materialize_due(now=old.scheduled_at+60)[0]
        barrier=threading.Barrier(2)
        def reserve(run_id,attempt):
            with self.tx() as cursor:
                barrier.wait(timeout=5)
                return self.usage.reserve_routine(run_id=run_id,attempt_id=attempt,now=self.now,transaction_cursor=cursor)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda args:reserve(*args),[(next_run.run_id,'TEST_A'),(binding['target_run_id'],'TEST_B')]))
        self.assertEqual(sorted(results),[False,True])
        self.assertEqual(sum(r['charged_tokens'] for r in self.query('SELECT * FROM routine_budget_reservations')),2000)

    def schedule(self,*,max_occurrences=2,unknown_old=False):
        spec=self.routines.set(self.source_scope,name='TEST recurring',prompt='TEST periodic text',
            cron='* * * * *',now=self.now)
        if unknown_old:
            run=self.routines.materialize_due(now=spec.next_due_at)[0]
            self.query("UPDATE routine_runs SET status='running' WHERE run_id=?",(run.run_id,))
        source=SourceRef('routine','spec:'+str(spec.routine_id))
        proof=self.journal.create(source,execution_deadline=self.now+400,result_deadline=self.now+400)
        proof.schedule_json=__import__('json').dumps(dict(cron=spec.cron,timezone=spec.timezone,version=spec.version,
            cutover=self.now,schedule_until=self.now+400,max_occurrences=max_occurrences,
            execution_seconds=90,result_seconds=200,
            capability_grant={'profile':'official-scheduled-text-v1','tools':[]}),sort_keys=True,separators=(',',':'))
        with self.tx() as cursor:
            binding=self.engine.prepare(cursor,proof,now=self.now)
        return proof,binding,spec

    async def test_recurring_parent_completed_then_real_occurrence_job_and_m3(self):
        proof,parent,spec=self.schedule()
        self.now=spec.next_due_at
        with self.tx() as cursor:
            with self.assertRaises(ContinuationDenied):
                self.engine.materialize_due(cursor,proof,now=self.now)
        self.assertEqual(await self.tick(),0)
        self.assertFalse(self.query('SELECT * FROM durable_jobs'))
        self.assertEqual(self.routines.materialize_due(now=self.now),[])
        self.activate(proof)
        with self.tx() as cursor:
            child=self.engine.materialize_due(cursor,proof,now=self.now)
        self.assertEqual(child['execution_deadline'],spec.next_due_at+90)
        self.assertEqual(child['result_deadline'],spec.next_due_at+200)
        self.assertEqual(await self.tick(),1)
        self.assertEqual((await self.sender.send_delivery(self.delivery()['delivery_id'])).status,'committed')
        with self.tx() as cursor:
            self.assertIsNone(self.engine.materialize_due(cursor,proof,now=self.now))
        self.now+=60
        with self.tx() as cursor:
            second=self.engine.materialize_due(cursor,proof,now=self.now)
        self.assertNotEqual(child['job_id'],second['job_id'])
        self.assertEqual(await self.tick(),1)
        self.now+=60
        with self.tx() as cursor:
            self.assertIsNone(self.engine.materialize_due(cursor,proof,now=self.now))
        self.assertEqual(len(self.query('SELECT * FROM routine_runs')),2)
        self.assertEqual(self.query('SELECT enabled FROM agent_routines WHERE routine_id=?',(spec.routine_id,))[0]['enabled'],False)

    async def test_recurring_unknown_source_slot_not_backfilled_after_restart(self):
        proof,parent,spec=self.schedule(unknown_old=True)
        self.activate(proof)
        self.now=spec.next_due_at
        with self.tx() as cursor:
            self.assertIsNone(self.new_engine().materialize_due(cursor,proof,now=self.now))
        self.assertFalse(self.query('SELECT * FROM qq_official_schedule_bindings WHERE parent_binding_id IS NOT NULL'))
        self.now+=60
        with self.tx() as cursor:
            child=self.new_engine().materialize_due(cursor,proof,now=self.now)
        self.assertIsNotNone(child)
        self.assertEqual(child['due_at'],self.now)
        self.assertEqual(self.query('SELECT status FROM routine_runs WHERE routine_id=?',(spec.routine_id,))[0]['status'],'running')

    async def test_recurring_missed_policy_deadline_anchored_to_slot_and_source_revision(self):
        proof,parent,spec=self.schedule()
        self.activate(proof)
        self.now=spec.next_due_at+125
        with self.tx() as cursor:
            child=self.engine.materialize_due(cursor,proof,now=self.now)
        expected=spec.next_due_at+120
        self.assertEqual((child['due_at'],child['execution_deadline']),(expected,expected+90))
        self.assertLess(child['execution_deadline'],self.now+90)
        await self.tick()
        self.query('UPDATE agent_routines SET version=version+1 WHERE routine_id=?',(spec.routine_id,))
        self.now=expected+60
        before=len(self.query('SELECT * FROM durable_jobs'))
        with self.assertRaises(ContinuationDenied):
            with self.tx() as cursor:
                self.engine.materialize_due(cursor,proof,now=self.now)
        self.assertEqual(len(self.query('SELECT * FROM durable_jobs')),before)

    async def test_recurring_expiry_and_unapproved_tool_profile_do_not_create_jobs(self):
        proof,parent,spec=self.schedule()
        self.activate(proof)
        self.now=proof.result_deadline
        with self.tx() as cursor:
            self.assertIsNone(self.engine.materialize_due(cursor,proof,now=self.now))
        self.assertFalse(self.query('SELECT * FROM durable_jobs'))
        altered=__import__('json').loads(proof.schedule_json)
        altered['capability_grant']={'profile':'host-admin','tools':['exec']}
        proof.schedule_json=__import__('json').dumps(altered)
        with self.tx() as cursor:
            with self.assertRaises(ContinuationDenied):
                self.engine.materialize_due(cursor,proof,now=self.now-1)

    async def test_reminder_projection_only_after_real_receipt_even_revoked_mapping(self):
        proof,binding=self.prepare(self.reminder())
        self.activate(proof)
        self.now+=2
        await self.tick()
        delivery=self.delivery()
        self.assertEqual(self.query('SELECT status FROM reminders WHERE reminder_id=?',(binding['target_reminder_id'],))[0][0],'sending')
        await self.sender.send_delivery(delivery['delivery_id'])
        self.query('UPDATE fixture_migrations SET mapping_revision=2 WHERE item_id=?',(proof.item_id,))
        with self.tx() as cursor:
            self.assertTrue(self.engine.reconcile_delivery(cursor,delivery['plan_id']))
        self.assertEqual(self.query('SELECT status FROM reminders WHERE reminder_id=?',(binding['target_reminder_id'],))[0][0],'sent')

    async def test_unknown_receipt_projection_retains_unknown_and_no_new_plan(self):
        proof,binding=self.prepare(self.reminder())
        self.activate(proof)
        self.now+=2
        await self.tick()
        self.fake.drop_next_response=True
        await self.sender.send_delivery(self.delivery()['delivery_id'])
        with self.tx() as cursor:
            self.engine.reconcile_delivery(cursor,self.delivery()['plan_id'])
        reminder=self.query('SELECT status,last_error FROM reminders WHERE reminder_id=?',(binding['target_reminder_id'],))[0]
        self.assertEqual((reminder['status'],reminder['last_error']),('sending','migration_delivery_unknown'))
        self.assertEqual(len(self.query('SELECT * FROM delivery_plans')),1)

    async def test_clock_accepts_fractional_seconds_and_rejects_nonfinite_bool(self):
        proof,binding=self.prepare(self.reminder())
        self.activate(proof)
        self.now+=2
        self.engine.clock=lambda:self.now+0.5
        self.assertEqual(await self.tick(),1)
        for invalid in (float('nan'),float('inf'),True):
            self.engine.clock=lambda value=invalid:value
            with self.assertRaises(ContinuationDenied):
                self.engine._now()

    async def test_converted_routine_uses_verified_bot_native_id_not_app_id(self):
        _,binding=self.prepare(self.routine())
        bot=self.query('SELECT bot_id FROM agent_routines WHERE routine_id=?',(binding['target_routine_id'],))[0][0]
        self.assertEqual(bot,'TEST_BOT');self.assertNotEqual(bot,self.account.app_id)

    async def test_converted_schedule_uses_verified_bot_native_id_not_app_id(self):
        _,binding,_=self.schedule()
        self.assertEqual(self.query('SELECT bot_id FROM agent_routines WHERE routine_id=?',(binding['target_routine_id'],))[0][0],'TEST_BOT')

    async def test_routine_without_verified_bot_author_rejects_atomically(self):
        source=self.routine()
        proof=self.journal.create(source)
        self.query('''UPDATE qq_official_accounts SET bot_native_id=NULL,bot_identity_source=NULL,
            bot_identity_verified_at=NULL,bot_identity_revision=0,bot_identity_contract=NULL WHERE account_key=?''',(self.account.key,))
        with self.assertRaises(ContinuationDenied):
            with self.tx() as cursor:self.engine.prepare(cursor,proof,now=self.now)
        self.assertEqual(self.routines.get_run(source.key[4:]).status,'queued')
        self.assertEqual(self.query('SELECT COUNT(*) FROM agent_routines WHERE account_key IS NOT NULL')[0][0],0)

    async def test_schedule_without_verified_bot_author_does_not_disable_source(self):
        self.query('''UPDATE qq_official_accounts SET bot_native_id=NULL,bot_identity_source=NULL,
            bot_identity_verified_at=NULL,bot_identity_revision=0,bot_identity_contract=NULL WHERE account_key=?''',(self.account.key,))
        with self.assertRaises(ContinuationDenied):self.schedule()
        self.assertEqual(self.query('SELECT enabled FROM agent_routines WHERE account_key IS NULL')[0][0],True)
        self.assertEqual(self.query('SELECT COUNT(*) FROM agent_routines WHERE account_key IS NOT NULL')[0][0],0)
