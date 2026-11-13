"""Program file provenance uses real storage, never a fabricated human turn."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from tests.qq_official_outbox_fixture import OutboxFixture
from src.bot_storage.database import DatabaseError
from tests.test_vm_sessions import BOOT
from src.plugins.ai_chat.adapters.qq_official.file_artifacts import ArtifactError, ArtifactStore
from src.plugins.ai_chat.adapters.qq_official.file_authority import assert_file_read_current
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.program_actions import ProgramActions
from src.plugins.ai_chat.adapters.qq_official.program_file_source import issue, assert_artifact
from src.plugins.ai_chat.adapters.qq_official.vm_sessions import VMSessions
from src.plugins.ai_chat.blob_store import ProjectBlobStore
from src.plugins.ai_chat.platform_types import EndpointKey
from src.plugins.ai_chat.storage.jobs import DurableJobStore


class ProgramFileSourceTests(OutboxFixture):
    def setUp(self):
        super().setUp()
        self.sessions = VMSessions(self.tx,self.token,private_admission=self.admission,clock=lambda:self.now)
        self.current = self.sessions.issue(self.endpoint,'s123456',BOOT,expected_route=(1,1)).source
        self.actions = ProgramActions(self.sessions)
        with self.tx() as cursor:
            observe_ready(cursor,self.token,{'op':0,'t':'READY','d':{'session_id':'TEST_SESSION',
                'user':{'id':'TEST_BOT','bot':True}}},self.now)
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.jobs = DurableJobStore(self.database); self.addCleanup(self.jobs.close)
        self.files = ArtifactStore(transaction=self.tx,jobs=self.jobs,blobs=ProjectBlobStore(Path(temporary.name)),
            account_key=self.account.key,clock=lambda:self.now)
        self.action,_ = self.actions.reserve(self.current,generation=0,kind='send_file',program='TEST_PROGRAM',
            request_id='TEST_FILE',payload={'path':'report.txt','filename':'report.txt'})
        with self.tx() as cursor:
            self.source = issue(cursor,self.actions,self.action['action_id'])

    def admission(self, _cursor, _endpoint, actor):
        return actor.identity_kind!='bot'

    def register(self, **changes):
        return self.files.register(self.source,**dict(content=b'TEST_FILE',filename='report.txt',
            logical_request_key=hashlib.sha256(b'TEST_REQUEST').hexdigest())|changes)

    def artifact(self):
        return dict(self.query('SELECT * FROM qq_official_file_artifacts')[0])

    def test_program_source_registers_once_without_human_turn_or_job(self):
        first = self.register()
        self.assertEqual(self.register().artifact_id,first.artifact_id)
        row = self.artifact()
        self.assertEqual(row['source_program_action_id'],self.action['action_id'])
        self.assertEqual(row['source_extension_kind'],'program')
        for key in ('source_execution_id','source_task_id','source_canonical_message_id','source_native_message_id','source_job_id'):
            self.assertIsNone(row[key])
        self.assertEqual(self.query('SELECT * FROM qq_official_turn_executions'),[])
        self.assertEqual(len(self.query('SELECT * FROM durable_jobs')),1) # Only the file delivery worker.
        with self.tx() as cursor:
            checked,_ = assert_file_read_current(cursor,token=self.token,artifact_id=first.artifact_id,
                admission=self.admission,now=self.now)
            self.assertEqual(checked['requester_actor_key'],row['requester_actor_key'])
        self.assertEqual(self.files.read_verified(first.artifact_id,sha256=row['sha256'],byte_size=row['byte_size']),b'TEST_FILE')
        self.assertEqual(self.actions.get(self.current,self.action['action_id'])['state'],'queued')

    def test_metadata_is_not_authority_for_a_different_endpoint_or_actor(self):
        self.register()
        row = self.artifact()
        other = EndpointKey(self.account,'group','TEST_OTHER'); self.provision(other)
        for changed in ({'endpoint_key':other.key},{'source_scope_key':other.scope.key},
                {'requester_actor_key':self.actor.key},{'conversation_generation':1},{'filename':'different.txt'}):
            with self.subTest(changed=changed), self.assertRaises(PermissionError), self.tx() as cursor:
                assert_artifact(cursor,token=self.token,artifact=row|changed,admission=self.admission,now=self.now)
        with self.assertRaises(DatabaseError) as denied:
            self.query('UPDATE qq_official_file_artifacts SET source_program_action_id=NULL')
        self.assertIn('immutable_program_file_source',str(denied.exception.__cause__))
        with self.assertRaisesRegex(ArtifactError,'artifact_request_conflict'):
            self.register(content=b'TEST_CHANGED')

    def test_real_downgrade_refuses_reserved_or_exported_file_evidence(self):
        import os
        from unittest.mock import patch
        from alembic import command
        from alembic.config import Config
        from src.bot_storage.schema import HEAD_REVISION
        # This fixture owns its own disposable DB; do not borrow another
        # migration fixture's schema ownership registry.
        for exported in (False,True):
            if exported:
                self.register()
            with patch.dict(os.environ,{'AI_POSTGRES_DSN':self.database.dsn,'AI_POSTGRES_SCHEMA':self.database.schema}), self.assertRaisesRegex(RuntimeError,'program_file_evidence_requires_export'):
                command.downgrade(Config('alembic.ini'),'0084_program_wakes')
            self.assertEqual(self.query('SELECT version_num FROM alembic_version')[0]['version_num'],HEAD_REVISION)
            self.assertEqual(self.query('SELECT action_id FROM qq_official_program_actions')[0]['action_id'],self.action['action_id'])

    def test_stop_start_disable_and_rotation_never_reuse_old_file_authority(self):
        self.register()
        row = self.artifact()
        def check():
            with self.tx() as cursor:
                assert_artifact(cursor,token=self.token,artifact=row,admission=self.admission,now=self.now)
        self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,1)',(self.current.scope_key,))
        for stopped in (True,False):
            self.query('UPDATE conversation_settings SET stopped=?',(stopped,))
            with self.assertRaisesRegex(PermissionError,'conversation_stopped_or_replaced'):
                check()
        self.query('UPDATE conversation_settings SET stop_generation=0')
        self.query('INSERT INTO program_controls VALUES (?,?,FALSE,?)',(self.current.scope_key,'TEST_PROGRAM',self.now))
        with self.assertRaisesRegex(PermissionError,'program_disabled'):
            check()
        self.query('UPDATE program_controls SET enabled=TRUE')
        self.sessions.revoke_vm('s123456')
        with self.assertRaises(PermissionError):
            check()


class ProgramFileMigrationTests(unittest.TestCase):
    def test_real_empty_upgrade_downgrade_roundtrip_keeps_settings(self):
        from tests import test_qq_official_outbox_migration as fixture
        schema = fixture.owned_schema(); self.addCleanup(fixture.drop,schema)
        fixture.migrate(schema,'0084_program_wakes')
        with fixture.connect(schema) as connection:
            connection.execute("INSERT INTO conversation_settings(scope_key,program_say_limit) VALUES ('TEST_SCOPE',3)")
        fixture.migrate(schema,'0085_program_files')
        with fixture.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT program_say_limit FROM conversation_settings').fetchone(),(3,))
            self.assertEqual(connection.execute('SELECT source_program_action_id FROM qq_official_file_artifacts').fetchall(),[])
        fixture.migrate(schema,'0084_program_wakes',downgrade=True)
        with fixture.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT program_say_limit FROM conversation_settings').fetchone(),(3,))
            self.assertIsNone(connection.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name='qq_official_file_artifacts' AND column_name='source_program_action_id'",(schema,)).fetchone())
        fixture.migrate(schema,'0085_program_files')
