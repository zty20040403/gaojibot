from dataclasses import replace
import unittest

from src.plugins.ai_chat.execution_control import LeaseLost
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






if __name__ == "__main__":
    unittest.main()
