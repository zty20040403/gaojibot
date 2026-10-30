from dataclasses import replace
import unittest

from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.adapters.qq_official.lease import DedicatedAccountLease
from src.plugins.ai_chat.platform_types import AccountKey
from src.plugins.ai_chat.adapters.qq_official.file_artifacts import file_job_kind
from src.plugins.ai_chat.adapters.qq_official.file_authority import assert_file_current
from tests.qq_official_file_fixture import FileFixture


class FileAuthorityTests(FileFixture):
    def setUp(self):
        super().setUp()
        self.item=self.put()
        self.file_job=self.jobs.claim_due('TEST_FILE_WORKER',kinds=(file_job_kind(self.account),),now=self.now)[0]

    def check(self,**overrides):
        args=dict(token=self.token,artifact_id=self.item.artifact_id,admission=self.admission,now=self.now,job=self.file_job)
        args.update(overrides)
        with self.tx() as cursor:
            return assert_file_current(cursor,**args)

    def finish_producer(self):
        turn=self.query('SELECT journal_turn_id FROM qq_official_turn_executions')[0]['journal_turn_id']
        self.result(self.handle,turn_id=turn)

    def test_file_current_uses_actual_registered_source(self):
        artifact,delivery=self.check()
        self.assertEqual(artifact['artifact_id'],self.item.artifact_id)
        self.assertEqual(delivery['job_id'],self.file_job.job_id)

    def test_file_after_producer_completed(self):
        self.finish_producer()
        self.now+=301
        self.file_job=self.jobs.claim_due('TEST_FILE_RESTART',kinds=(file_job_kind(self.account),),now=self.now)[0]
        self.assertEqual(self.check()[0]['artifact_id'],self.item.artifact_id)
        self.assertEqual(len(self.query('SELECT * FROM qq_official_turn_executions')),1)

    def test_file_old_job_generation_cannot_act(self):
        self.finish_producer()
        old=self.file_job
        self.now+=301
        self.file_job=self.jobs.claim_due('TEST_FILE_RESTART',kinds=(file_job_kind(self.account),),now=self.now)[0]
        with self.assertRaises(LeaseLost):
            self.check(job=old)
        with self.assertRaises(LeaseLost):
            self.check(job=replace(self.file_job,payload={'artifact_id':'f'*32}))
        self.check()

    def test_file_route_or_admission_revocation_denies(self):
        self.finish_producer()
        self.admitted=False
        with self.assertRaises(LeaseLost):
            self.check()
        self.admitted=True
        self.query("UPDATE qq_official_endpoints SET binding_state='revoked' WHERE endpoint_key=?",(self.endpoint.key,))
        with self.assertRaises(LeaseLost):
            self.check()

    def test_file_retention_expiry_denies(self):
        self.query('UPDATE qq_official_file_artifacts SET retain_until=? WHERE artifact_id=?',(self.now,self.item.artifact_id))
        with self.assertRaises(LeaseLost):
            self.check()

    def test_file_other_account_cannot_use_same_artifact_id(self):
        account=AccountKey('qq-official','TEST_OTHER_FILE_ACCOUNT','test')
        self.provision(type(self.endpoint)(account,'group','TEST_GROUP'))
        lease=DedicatedAccountLease(self.database.dsn,account,schema=self.database.schema,clock=lambda:self.now)
        self.addCleanup(lease.close)
        token=lease.acquire()
        with self.assertRaises(LeaseLost):
            self.check(token=token)

    def test_file_jobless_call_cannot_invent_handoff(self):
        with self.assertRaises(LeaseLost):
            self.check(job=None,plan_id='f'*32)



class SubAgentFileAuthorityTests(FileFixture):
    """Real structured task + existing durable job; no model or transport fake identity."""
    def setUp(self):
        super().setUp()
        from unittest.mock import Mock
        from src.plugins.ai_chat.adapters.qq_official import turn_source as execution
        from src.plugins.ai_chat.adapters.qq_official.file_artifacts import task_source
        from src.plugins.ai_chat.adapters.qq_official.subagent_runtime import submit, job_kind
        from src.plugins.ai_chat.agent.contracts import ContextPacket
        from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelProfile
        from src.plugins.ai_chat.subagents import SubAgentCoordinator, SubAgentStore
        self.task_store = SubAgentStore(self.database)
        self.addCleanup(self.task_store.close)
        profile = ModelProfile(name='test', provider='test', protocol='openai-chat', model='test',
            base_url='http://127.0.0.1', api_key_required=False)
        coordinator = SubAgentCoordinator(self.task_store,
            ModelCatalog({'test':profile}, default_profile='test'), logger=Mock())
        packet = ContextPacket(self.endpoint.scope.key, 'TEST_FILE_TASK_CONTEXT', None,
            self.message.canonical_message_id, '测试产物来源', endpoint=self.endpoint, requester_actor=self.actor)
        with self.tx() as cursor:
            self.task = submit(cursor, token=self.token, source_api=execution, source_handle=self.handle,
                source_job=self.job, tool_call_id='TEST_ARTIFACT_TASK', tool_name='run_subagents',
                packet=packet, decision=None, profile='test', now=self.now, execution_deadline=self.now+1200,
                result_wait_seconds=600, admission=self.admission, coordinator=coordinator, jobs=self.jobs)
        self.task_job = self.jobs.claim_due('TEST_ARTIFACT_TASK_WORKER', kinds=(job_kind(self.account),), now=self.now)[0]
        self.task_store.set_task_state(self.task.task_id, 'running')
        self.source_args = dict(token=self.token, task_id=self.task.task_id, job=self.task_job,
            admission=self.admission, now=self.now, pending_deadline_at=self.now+86400)
        with self.tx() as cursor:
            self.source = task_source(cursor, **self.source_args)
        self.item = self.put()
        self.file_job = self.jobs.claim_due('TEST_TASK_FILE_WORKER', kinds=(file_job_kind(self.account),), now=self.now)[0]

    def check(self):
        with self.tx() as cursor:
            return assert_file_current(cursor, token=self.token, artifact_id=self.item.artifact_id,
                admission=self.admission, now=self.now, job=self.file_job)

    def complete_producer(self, status):
        self.task_store.set_task_state(self.task.task_id, status, result={'answer':'TEST frozen content'})
        self.assertTrue(self.jobs.mark_succeeded(self.task_job.job_id, self.task_job.lease_owner,
            now=self.now, lease_generation=self.task_job.lease_generation))
        turn = self.query('SELECT journal_turn_id FROM qq_official_turn_executions')[0]['journal_turn_id']
        self.result(self.handle, turn_id=turn)
        # File delivery remains authorized after producer leases/execution deadlines expire.
        self.now += 3601
        self.file_job = self.jobs.claim_due('TEST_TASK_FILE_RESTART', kinds=(file_job_kind(self.account),), now=self.now)[0]

    def test_completed_subagent_file_survives_producer_completion_and_lease_expiry(self):
        self.complete_producer('completed')
        artifact, _ = self.check()
        self.assertEqual(artifact['source_task_id'], self.task.task_id)
        self.assertEqual(artifact['source_job_id'], self.task_job.job_id)
        self.assertEqual(artifact['source_native_message_id'], self.handle.source_native_message_id)
        self.assertEqual(self.files.read_verified(self.item.artifact_id, sha256=self.item.sha256,
            byte_size=self.item.byte_size), b'%PDF-1.4\n% TEST controlled fixture\n%%EOF\n')

    def test_partial_subagent_can_deliver_file(self):
        self.complete_producer('partial')
        artifact, _ = self.check()
        self.assertFalse(artifact['draft'])
        self.assertEqual(self.task_store.get(self.task.task_id).status, 'partial')

    def test_cancel_requested_blocks_existing_file_while_task_still_running(self):
        self.query('UPDATE subagent_tasks SET cancel_requested=TRUE WHERE task_id=?', (self.task.task_id,))
        with self.assertRaisesRegex(LeaseLost, 'artifact_task_cancelled'):
            self.check()
        self.assertEqual(self.task_store.get(self.task.task_id).status, 'running')

    def test_cancel_requested_blocks_new_task_source(self):
        from src.plugins.ai_chat.adapters.qq_official.file_artifacts import task_source
        self.query('UPDATE subagent_tasks SET cancel_requested=TRUE WHERE task_id=?', (self.task.task_id,))
        with self.assertRaises(LeaseLost), self.tx() as cursor:
            task_source(cursor, **self.source_args)

    def test_cancel_requested_blocks_registration_with_previously_issued_source(self):
        import hashlib
        from tests.qq_official_file_fixture import PDF
        self.query('UPDATE subagent_tasks SET cancel_requested=TRUE WHERE task_id=?', (self.task.task_id,))
        before = len(self.query('SELECT * FROM qq_official_file_artifacts'))
        with self.assertRaises(LeaseLost):
            self.files.register(self.source, content=PDF, filename='new.pdf',
                logical_request_key=hashlib.sha256(b'TEST_AFTER_CANCEL').hexdigest())
        self.assertEqual(len(self.query('SELECT * FROM qq_official_file_artifacts')), before)

    def test_failed_subagent_can_deliver_existing_artifact(self):
        self.complete_producer('failed')
        self.assertEqual(self.check()[0]['artifact_id'], self.item.artifact_id)



if __name__ == "__main__":
    unittest.main()
