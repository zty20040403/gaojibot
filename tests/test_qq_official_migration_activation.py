"""Authenticated console -> real journal -> original domains and existing jobs."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from src.bot_security.service import MobileAuthorization
from src.plugins.ai_chat.admin import AdminServices, register_admin
from src.plugins.ai_chat.admin_security import SESSION_COOKIE
from src.plugins.ai_chat.adapters.qq_official.migration_activation import MigrationActivation
from src.plugins.ai_chat.adapters.qq_official.migration_console import MigrationConsoleService
from src.plugins.ai_chat.adapters.qq_official.migration_journal import PARKED_AT
from src.plugins.ai_chat.adapters.qq_official.migration_preview import SourceRef
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied, VerificationEvidence
from tests import test_qq_official_migration_subagents as subagents
from tests import test_qq_official_migration_journal_continuation as scheduled
from tests import qq_official_migration_journal_fixture as verification
from tests.qq_official_outbox_fixture import OutboxFixture


class MigrationActivationTests(OutboxFixture, unittest.IsolatedAsyncioTestCase):
    owned=subagents.SubAgentContinuationTests.owned
    new_engine=subagents.SubAgentContinuationTests.new_engine
    ensure_current=subagents.SubAgentContinuationTests.ensure_current
    rotate_mapping=subagents.SubAgentContinuationTests.rotate_mapping
    tick=subagents.SubAgentContinuationTests.tick
    source_task=subagents.SubAgentContinuationTests.source_task
    reminder=scheduled.JournalContinuationTests.reminder
    routine=scheduled.JournalContinuationTests.routine

    async def asyncSetUp(self):
        await subagents.SubAgentContinuationTests.asyncSetUp(self)
        self.scheduled=scheduled.JournalContinuationTests.new_engine(self)
        self.domains={'reminder':self.scheduled,'routine':self.scheduled,'subagent':self.engine}
        self.ports.control=self.mapping_authority.control
        for method in ('bound_job','bound_plan','bound_schedule','occurrence'):
            target={'bound_job':'validate_bound_job','bound_plan':'validate_bound_plan',
                'bound_schedule':'validate_bound_schedule','occurrence':'validate_occurrence'}[method]
            def route(cursor,item,*args,_target=target,**kwargs):
                kind=item.source_kind if hasattr(item,'source_kind') else item['kind']
                return getattr(self.domains[kind],_target)(cursor,item,*args,**kwargs)
            setattr(self.ports,method,route)
        from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
        for kind in ('reminder','routine'):
            self.worker.register(account_job_kind(kind,self.account.key),self.scheduled.execute)
        self.make_console()
        self.app=FastAPI()
        register_admin(self.app,AdminServices(version='test',started_at=1,database=self.database,
            settings=SimpleNamespace(admin_origin='https://test'),
            mobile_authorization=MobileAuthorization(self.mapping_security,sender=None,bot_selector=lambda:None),
            official_migration_service=lambda:self.console))
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='https://test',
            headers={'Origin':'https://test','x-csrf-token':self.mapping_csrf})
        self.client.cookies.set(SESSION_COOKIE,self.mapping_cookie)
        self.path='/bot-admin/api/v1/qq-official/migrations'

    async def asyncTearDown(self):
        await self.client.aclose()
        await subagents.SubAgentContinuationTests.asyncTearDown(self)

    def domain(self,cursor,item):
        domain=self.domains.get(item['kind'])
        if domain is None:raise MigrationDenied('TEST domain unavailable')
        return domain

    def make_console(self):
        self.activation=MigrationActivation(journal=self.migration,domain_for_item=self.domain)
        self.console=MigrationConsoleService(authority=self.mapping_authority,mappings=self.mapping_store,
            transaction=self.tx,calls=self.calls,journal=self.migration,activation=self.activation,
            baseline_source=lambda c,s,n:self.baseline_source,clock=lambda:self.now)

    async def plan(self,*sources):
        response=await self.client.post(self.path,json=dict(source_endpoint=self.source.key,
            source_scope=self.source_scope.key,target_endpoint=self.endpoint.key,
            items=[dict(kind=s.kind,key=s.key) for s in sources],deadline=self.now+500))
        self.assertEqual(response.status_code,200,response.text)
        self.plan_value=response.json()
        for action in ('freeze','drain'):
            await self.action(action)

    async def action(self,action,expected=200):
        response=await self.client.post(self.path+'/'+self.plan_value['migration_id']+'/actions',
            json=dict(action=action,preview_digest=self.plan_value['preview_digest']))
        self.assertEqual(response.status_code,expected,response.text)
        return response

    def target_jobs(self):
        return self.query('''SELECT j.* FROM migration_items i JOIN durable_jobs j ON j.job_id=i.job_id
            WHERE i.migration_id=? ORDER BY j.job_id''',(self.plan_value['migration_id'],))

    async def verified(self):
        await self.action('verify')
        migration_id=self.plan_value['migration_id']
        nonce=self.query('SELECT verification_nonce FROM migration_journals WHERE migration_id=?',(migration_id,))[0][0]
        # Receipt transport fixtures establish actual applied M3 evidence. They
        # do not bypass the real console completion or domain activation.
        for kind in ('text','file'):
            with self.tx() as cursor:
                proof=self.migration.issue_verification(cursor,migration_id=migration_id,kind=kind,
                    actor=self.actor,source=self.verification_request_source,now=self.now)
            def register(cursor,**kwargs):
                plan=verification.enqueue_plan(cursor,**kwargs)
                self.verification_plans[plan.plan_id]=(proof.migration_id,proof.kind,proof.request_id)
                self.migration.bind_verification_plan(cursor,proof,plan_id=plan.plan_id,now=self.now)
                return plan
            with patch.object(verification.receipt_fixture,'enqueue_plan',side_effect=register):
                attempt=verification.receipt_fixture.ReceiptFixture.attempt(self)
            receipt=verification.receipt_fixture.ReceiptFixture.capture(self,attempt,{'id':'TEST_VERIFY_'+kind})
            self.assertEqual(verification.receipt_fixture.ReceiptFixture.finalize(self,receipt,journal=False).status,'committed')
            source=object()
            self.verifications[source]=VerificationEvidence(receipt,attempt.delivery_id,kind,nonce)
            with self.tx() as cursor:
                self.migration.record_verification(cursor,migration_id=migration_id,source=source,now=self.now)
        self.query("UPDATE qq_official_reply_windows SET state='expired' WHERE credential_value<>'TEST_SOURCE'")

    async def test_reminder_console_switch_parks_and_restart_completion_releases_once(self):
        await self.plan(self.reminder());await self.action('switch')
        jobs=self.target_jobs()
        self.assertEqual([(j['attempts'],j['next_attempt_at']) for j in jobs],[(0,PARKED_AT)])
        self.assertEqual(await self.tick(),0)
        self.make_console();await self.action('switch')
        self.assertEqual([j['job_id'] for j in self.target_jobs()],[jobs[0]['job_id']])
        await self.verified();await self.action('complete');await self.action('complete')
        self.now+=2;self.assertEqual(await self.tick(),1)
        self.assertEqual(self.target_jobs()[0]['status'],'succeeded')
        binding=self.query('SELECT * FROM qq_official_schedule_bindings')[0]
        delivery=self.query('SELECT delivery_id FROM deliveries WHERE plan_id=?',(binding['plan_id'],))[0][0]
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertEqual(len(self.fake.received),1)

    async def test_missing_domain_rolls_back_switch_without_freezing_source(self):
        source=self.reminder();await self.plan(source);self.domains.pop('reminder')
        await self.action('switch',409)
        self.assertEqual(self.query('SELECT phase FROM migration_routes')[0][0],'draining')
        self.assertEqual(self.target_jobs(),[])
        self.assertEqual(self.query('SELECT status FROM reminders WHERE reminder_id=?',(int(source.key),))[0][0],'pending')

    async def test_preparation_failure_rolls_back_all_domains_and_original_rows(self):
        await self.plan(self.reminder(),self.reminder())
        original=self.scheduled.prepare;count=0
        def fail_second(*args,**kwargs):
            nonlocal count
            count+=1
            if count==2:raise RuntimeError('TEST conversion interrupted')
            return original(*args,**kwargs)
        with patch.object(self.scheduled,'prepare',side_effect=fail_second):await self.action('switch',409)
        self.assertEqual(self.target_jobs(),[])
        self.assertEqual(self.query('SELECT count(*) FROM reminders WHERE status=\'pending\'')[0][0],2)
        self.assertEqual(self.query('SELECT count(*) FROM qq_official_schedule_bindings')[0][0],0)
        await self.action('switch');self.assertEqual(len(self.target_jobs()),2)

    async def test_activation_failure_keeps_receipts_and_both_jobs_parked(self):
        await self.plan(self.reminder(),self.reminder());await self.action('switch');await self.verified()
        original=self.scheduled.activate;count=0
        def fail_second(*args,**kwargs):
            nonlocal count
            count+=1
            if count==2:raise RuntimeError('TEST activation interrupted')
            return original(*args,**kwargs)
        with patch.object(self.scheduled,'activate',side_effect=fail_second):await self.action('complete',409)
        self.assertEqual(self.query('SELECT phase FROM migration_routes')[0][0],'verifying')
        self.assertTrue(all(j['next_attempt_at']==PARKED_AT and j['attempts']==0 for j in self.target_jobs()))
        self.assertEqual(self.query('SELECT count(*) FROM migration_verification_receipts')[0][0],2)
        self.make_console();await self.action('complete')
        self.assertTrue(all(j['next_attempt_at']!=PARKED_AT for j in self.target_jobs()))

    async def test_completed_subagent_only_hands_off_original_frozen_result(self):
        await self.plan(self.source_task(completed=True));await self.action('switch');await self.verified()
        await self.action('complete')
        with patch.object(self.coordinator,'resume',new=AsyncMock(side_effect=AssertionError('must not rerun'))):
            self.assertEqual(await self.tick(),1)
        task=self.query('SELECT t.* FROM subagent_tasks t JOIN migration_subagent_continuations m ON m.target_task_id=t.task_id')[0]
        self.assertIsNone(task['requester_user_id']);self.assertIsNone(task['trigger_message_id'])
        self.assertEqual(self.target_jobs()[0]['status'],'succeeded')

    async def test_recurring_parent_activates_original_schedule_then_one_real_child(self):
        spec=self.routines.set(self.source_scope,name='TEST schedule',prompt='TEST run',cron='* * * * *',now=self.now)
        self.execution_deadline=self.result_deadline=self.now+400
        self.ports.schedule=dict(cron=spec.cron,timezone=spec.timezone,version=spec.version,cutover=self.now,
            schedule_until=self.now+400,max_occurrences=2,execution_seconds=90,result_seconds=200,
            capability_grant={'profile':'official-scheduled-text-v1','tools':[]})
        await self.plan(SourceRef('routine','spec:'+str(spec.routine_id)));await self.action('switch')
        self.assertEqual(self.target_jobs(),[])
        self.assertFalse(self.query('SELECT enabled FROM agent_routines WHERE routine_id=?',(spec.routine_id,))[0][0])
        await self.verified();await self.action('complete')
        self.now=spec.next_due_at
        item=self.query('SELECT item_id FROM migration_items WHERE parent_item_id IS NULL')[0][0]
        with self.tx() as cursor:
            proof=self.migration.issue_item(cursor,item_id=item,operation='activate',now=self.now)
            child=self.scheduled.materialize_due(cursor,proof,now=self.now)
        self.assertEqual(child['execution_deadline'],spec.next_due_at+90)
        self.assertEqual(await self.tick(),1)

    async def test_session_revoked_before_complete_never_releases_jobs(self):
        await self.plan(self.reminder());await self.action('switch');await self.verified()
        self.mapping_security.logout(self.mapping_cookie)
        await self.action('complete',401)
        self.assertTrue(all(j['next_attempt_at']==PARKED_AT for j in self.target_jobs()))
