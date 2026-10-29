"""Real 0068 round trips preserve legacy data and refuse lost identity evidence."""
from src.bot_security.store import SecurityStore
from src.bot_storage.schema import HEAD_REVISION
from tests import test_qq_official_capability_migration as migration
from tests.qq_official_file_fixture import ExecutionFixture, FileFixture


class OperationsMigrationTests(ExecutionFixture):
    def security(self):
        store=SecurityStore(self.database,b'TEST_M8_MIGRATION_PEPPER_123456789012345',clock=lambda:self.now)
        self.addCleanup(store.close)
        return store

    def refused(self):
        with self.assertRaisesRegex(RuntimeError,'official_operation_evidence_requires_export'):
            migration.migrate(self,'downgrade','0067_qq_official_capabilities')
        self.assertEqual(self.query('SELECT version_num FROM alembic_version')[0][0],HEAD_REVISION)

    def test_roundtrip_restores_catalog_and_legacy_accounts_without_touching_jobs(self):
        store=self.security();store.bootstrap('test_legacy','TEST safe password 12345','10001')
        tables=('admin_accounts','qq_official_inbound_events','durable_jobs')
        before={name:[dict(row) for row in self.query('SELECT * FROM '+name)] for name in tables}
        snapshot=migration.CapabilityMigrationTests.snapshot(self)
        migration.migrate(self,'downgrade','0067_qq_official_capabilities')
        self.assertIsNone(self.query("SELECT to_regclass('qq_official_admin_identity_challenges')")[0][0])
        migration.migrate(self,'upgrade','head')
        self.assertEqual(migration.CapabilityMigrationTests.snapshot(self),snapshot)
        self.assertEqual({name:[dict(row) for row in self.query('SELECT * FROM '+name)] for name in tables},before)

    def test_disabled_local_account_still_prevents_identity_loss(self):
        self.security().bootstrap_local_admin('test_local','TEST safe password 12345')
        self.query('UPDATE admin_accounts SET enabled=0')
        self.refused()
        self.assertEqual(dict(self.query('SELECT identity_mode,enabled FROM admin_accounts')[0]),
                         {'identity_mode':'local_admin','enabled':0})

    def test_unmatched_control_receipt_is_retained_even_without_a_challenge(self):
        self.query('''UPDATE qq_official_inbound_events SET identity_control_actor_key=?,
            identity_control_occurred_at=?,identity_control_commitment=? WHERE inbox_id=?''',
            (self.actor.key,self.now,'a'*64,self.inbox))
        self.assertEqual(self.query('SELECT * FROM qq_official_admin_identity_challenges'),[])
        self.refused()
        self.assertEqual(self.query('SELECT identity_control_commitment FROM qq_official_inbound_events')[0][0],'a'*64)

    def test_terminal_challenge_is_audit_evidence_not_disposable_pending_work(self):
        admin=self.security().bootstrap('test_legacy','TEST safe password 12345','10001')
        self.query('''INSERT INTO qq_official_admin_identity_challenges
            (challenge_id,account_key,admin_account_id,admin_version,session_hash,code_digest,state,created_at,expires_at)
            VALUES (?,?,?,?,?,?,'cancelled',?,?)''',
            ('b'*32,self.account.key,admin['account_id'],admin['version'],'c'*64,'d'*64,self.now,self.now+180))
        self.refused()
        self.assertEqual(self.query('SELECT state FROM qq_official_admin_identity_challenges')[0][0],'cancelled')


class UploadOperationMigrationTests(FileFixture):
    def stage(self, operation, state='confirmed'):
        item=self.put()
        job=self.query('SELECT job_id FROM qq_official_file_deliveries WHERE artifact_id=?',(item.artifact_id,))[0][0]
        self.query('''INSERT INTO qq_official_file_stage_attempts
            (stage_attempt_id,artifact_id,operation,request_commitment,job_id,job_lease_generation,
             account_epoch,state,created_at,expires_at,settled_at,receipt_commitment)
            VALUES (?,?,?,?,?,1,?,?,?,?,?,?)''',
            ('e'*32,item.artifact_id,operation,'f'*64,job,self.token.epoch,state,self.now,
             self.now+60,self.now,'a'*64 if state=='confirmed' else None))
        return item.artifact_id

    def refused(self):
        OperationsMigrationTests.refused(self)

    def test_existing_small_upload_receipt_survives_roundtrip(self):
        self.stage('upload_small')
        original=[dict(row) for row in self.query('SELECT * FROM qq_official_file_stage_attempts')]
        migration.migrate(self,'downgrade','0067_qq_official_capabilities')
        self.assertIsNone(self.query("SELECT to_regclass('qq_official_file_upload_sessions')")[0][0])
        self.assertEqual([dict(row) for row in self.query('SELECT * FROM qq_official_file_stage_attempts')],original)
        migration.migrate(self,'upgrade','head')
        self.assertEqual([dict(row) for row in self.query('SELECT * FROM qq_official_file_stage_attempts')],original)

    def test_unknown_prepare_without_session_still_prevents_evidence_loss(self):
        self.stage('upload_prepare','unknown')
        self.assertEqual(self.query('SELECT * FROM qq_official_file_upload_sessions'),[])
        self.refused()
        self.assertEqual(self.query('SELECT state FROM qq_official_file_stage_attempts')[0][0],'unknown')

    def test_expired_session_is_preserved_audit_evidence(self):
        artifact=self.stage('upload_prepare')
        self.query('''INSERT INTO qq_official_file_upload_sessions
            (artifact_id,prepare_stage_attempt_id,credential_ref,binding_digest,prepared_at,expires_at,part_count,contract)
            VALUES (?,?,?,?,?,?,3,'TEST_CONTRACT')''',
            (artifact,'e'*32,'b'*32,'a'*64,self.now,self.now+60))
        self.now+=61
        original=dict(self.query('SELECT * FROM qq_official_file_upload_sessions')[0])
        self.refused()
        self.assertEqual(dict(self.query('SELECT * FROM qq_official_file_upload_sessions')[0]),original)
