"""A real task may capture locally after recipient evidence ages out.

Existing M6 upload and M3 send still demand fresh evidence under the original
grant. All credentials and transports are fake and confined to loopback.
"""
import unittest
from unittest.mock import patch

from tests import test_qq_official_project_sessions as session_fixture
from tests.qq_official_file_fixture import PDF


class ProjectCaptureAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # A locally instantiated fixture is not discovered as another test
        # suite. Its schema and cleanups belong to this actual async test.
        class Fixture(session_fixture.ProjectSessionTests):
            pass
        f=self.f=Fixture('test_real_per_step_owners_baseline_and_cross_step_denial')
        self.addCleanup(Fixture.doClassCleanups)
        try:
            f.setUp()
        finally:
            for callback,args,kwargs in f._cleanups:
                self.addCleanup(callback,*args,**kwargs)
            f._cleanups.clear()
        await f.asyncSetUp();self.addAsyncCleanup(f.asyncTearDown)
        f.outbox.protected_payloads=f.files.payloads
        f.admit();f.tasks.set_task_state(f.task.task_id,'running',now=f.now)
        with f.tx() as cursor:
            self.provider,self.access=f.sessions._provider(cursor,f.task_job,f.tasks.get(f.task.task_id))
            self.source=f.authority.issue(cursor,access=self.access,project_id=f.project['project_id'])
        self.original_deadlines=(self.source.deadline_at,self.source.pending_deadline_at)
        self.original_grant=f.query('SELECT * FROM qq_official_project_grants')[0]

















    async def test_revoking_original_grant_still_blocks_capture_before_disk_write(self):
        f=self.f;f.now+=301
        f.query("UPDATE qq_official_project_grants SET state='revoked',revision=revision+1,revoked_at=?",
            (f.now,))
        with patch.object(f.files.artifacts,'_store_blob',wraps=f.files.artifacts._store_blob) as store:
            with self.assertRaises(PermissionError):
                await f.calls(f.files.artifacts.capture_verified,self.source,content=PDF,filename='report.pdf')
        store.assert_not_called()
        self.assertEqual(f.query('SELECT * FROM qq_official_file_artifacts'),[])

    async def test_execute_without_original_deliver_capability_cannot_capture(self):
        f=self.f
        f.query("UPDATE qq_official_project_grants SET capabilities=array_remove(capabilities,'project.deliver')")
        with self.assertRaises(PermissionError):
            await f.calls(f.files.artifacts.capture_verified,self.source,content=PDF,filename='report.pdf')
        self.assertEqual(f.query('SELECT * FROM qq_official_file_artifacts'),[])
