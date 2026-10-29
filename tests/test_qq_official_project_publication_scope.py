"""Real PG/CAS resource quota checks independent of the requesting conversation.

Private _commit is called only after the fixture's real publication authority
check. A poison access scope ensures final accounting uses the authorized
resource, while its real principal is retained in publication attribution.
"""
import hashlib
from types import SimpleNamespace
import unittest
from src.project_content import manifest_bytes
from src.plugins.ai_chat.projects import ProjectQuotaExceeded
from tests import test_project_publication as fixture

class PoisonScope:
    @property
    def key(self):raise AssertionError('resource quota read actor conversation scope')

class PublicationResourceTests(unittest.TestCase):
    owned=fixture.ProjectPublicationTests.owned
    new_store=fixture.ProjectPublicationTests.new_store
    create=fixture.ProjectPublicationTests.create
    snapshot=fixture.ProjectPublicationTests.snapshot
    prepare=fixture.ProjectPublicationTests.prepare
    count=fixture.ProjectPublicationTests.count
    accepted=fixture.ProjectPublicationTests.accepted
    event=fixture.ProjectPublicationTests.event
    def setUp(self):fixture.ProjectPublicationTests.setUp(self)

    def material(self):
        project=self.store.get(self.access,self.pid)
        doc=self.baseline
        encoded=manifest_bytes(doc,quota_bytes=project['quota_bytes'])
        return project,doc,encoded

    def target_policy(self):
        target='qq-official:group:TEST_UNRELATED_RESOURCE'
        self.sql.execute('INSERT INTO project_scope_limits(scope_key,kind,quota_bytes,max_projects) VALUES (?,?,?,?)',
            (target,'group',10000000,100))
        return target

    def test_precheck_works_without_any_target_scope_policy(self):
        project,doc,encoded=self.material()
        self.assertEqual(self.count('project_scope_limits'),1)
        with self.store._transaction() as cursor:self.publisher._check_capacity(cursor,project,doc,encoded)
        self.assertEqual(self.count('project_scope_limits'),1)

    def test_large_target_policy_cannot_bypass_old_resource_limit(self):
        self.target_policy()
        self.sql.execute('UPDATE project_scope_limits SET quota_bytes=1 WHERE scope_key=?',(self.scope.key,))
        project,doc,encoded=self.material()
        with self.store._transaction() as cursor,self.assertRaises(ProjectQuotaExceeded):
            self.publisher._check_capacity(cursor,project,doc,encoded)
        self.assertEqual(self.count('project_task_publications'),0)

    def test_low_unrelated_target_limit_does_not_deny_resource_precheck(self):
        target=self.target_policy()
        self.sql.execute('UPDATE project_scope_limits SET quota_bytes=1 WHERE scope_key=?',(target,))
        project,doc,encoded=self.material()
        with self.store._transaction() as cursor:self.publisher._check_capacity(cursor,project,doc,encoded)
        self.assertEqual(self.count('project_scope_limits'),2)

    def commit_noop(self):
        self.accepted({'index.html':b'first'})
        with self.tasks._transaction() as cursor:
            self.store._scope_lock(cursor,self.access)
            project,binding,prior,proof=self.publisher._authority(cursor,self.access,self.task.task_id,self.digest)
            self.assertIsNone(prior)
            doc=self.baseline
            encoded=manifest_bytes(doc,quota_bytes=project['quota_bytes'])
            manifest=SimpleNamespace(sha256=hashlib.sha256(encoded).hexdigest(),byte_size=len(encoded))
            attributed=SimpleNamespace(principal_id=self.access.principal_id,scope=PoisonScope())
            return self.publisher._commit(cursor,attributed,self.task.task_id,project,binding,proof,
                self.digest,doc,manifest,100)

    def test_final_commit_uses_resource_scope_and_keeps_real_principal(self):
        receipt=self.commit_noop()
        self.assertEqual(receipt['version_no'],1)
        self.assertEqual(self.count('project_task_publications'),1)
        row=self.sql.execute("SELECT actor_principal_id FROM project_events WHERE kind='task_version_published'").fetchone()
        self.assertEqual(row[0],self.access.principal_id)

    def test_final_commit_checks_old_resource_limit_and_rolls_back(self):
        self.target_policy()
        self.sql.execute('UPDATE project_scope_limits SET quota_bytes=1 WHERE scope_key=?',(self.scope.key,))
        with self.assertRaises(ProjectQuotaExceeded):self.commit_noop()
        self.assertEqual(self.count('project_task_publications'),0)
        self.assertEqual(self.count('project_versions'),1)

    def test_final_commit_does_not_charge_unrelated_low_target_limit(self):
        target=self.target_policy()
        self.sql.execute('UPDATE project_scope_limits SET quota_bytes=1 WHERE scope_key=?',(target,))
        self.assertEqual(self.commit_noop()['version_no'],1)
        self.assertEqual(self.count('project_task_publications'),1)
        self.assertEqual(self.sql.execute('SELECT quota_bytes FROM project_scope_limits WHERE scope_key=?',(target,)).fetchone()[0],1)
