import json
import unittest

from tests import test_qq_official_outbox_migration as migration


class RetireApprovalTablesTests(unittest.TestCase):
    def test_preserves_unsent_onebot_success_without_retiring_unfinished_work(self):
        schema = migration.owned_schema()
        self.addCleanup(migration.drop, schema)
        migration.migrate(schema, '0131_merge_security_audit')
        with migration.connect(schema) as db:
            db.execute("""INSERT INTO admin_accounts (account_id,username,password_hash,role,qq_id,created_at)
                VALUES ('TEST_ACCOUNT','test_admin','TEST_HASH','admin','TEST_QQ',1)""")
            db.execute("""INSERT INTO admin_approvals
                (approval_id,account_id,account_version,qq_id,bot_id,session_hash,kind,payload_json,
                 payload_hash,summary,code_hash,expires_at,status,created_at,updated_at)
                VALUES ('TEST_APPROVAL','TEST_ACCOUNT',1,'TEST_QQ','TEST_BOT','','test','{}',
                        'TEST_DIGEST','TEST_SUMMARY','',999,'pending',1,1)""")
        revision = '0132_retire_approval_tables'
        for status in ('pending', 'needs_attention'):
            with self.subTest(status=status):
                with migration.connect(schema) as db:
                    db.execute('UPDATE admin_approvals SET status=%s', (status,))
                with self.assertRaisesRegex(RuntimeError, 'active approval or unsent receipt'):
                    migration.migrate(schema, revision)
        with migration.connect(schema) as db:
            db.execute("UPDATE admin_approvals SET status='succeeded'")
        migration.migrate(schema, revision)
        with migration.connect(schema) as db:
            self.assertIsNone(db.execute("SELECT to_regclass('admin_approvals')").fetchone()[0])
            columns = db.execute("""SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema()
                AND table_name='delivery_plans' AND column_name IN ('approval_source_id','approval_source_expires_at')""").fetchall()
            self.assertFalse(columns)
            self.assertEqual(db.execute('SELECT account_id FROM admin_accounts').fetchone(), ('TEST_ACCOUNT',))
            row = db.execute("""SELECT actor,target,security_detail,created_at FROM admin_audit_log
                WHERE security_audit_id='retired-onebot-notice:TEST_APPROVAL' AND action='approval.notice.retired_unsent'""").fetchone()
            self.assertEqual((row[0], row[1], row[3]), ('TEST_ACCOUNT', 'TEST_APPROVAL', 1))
            self.assertEqual(json.loads(row[2]), {'transport':'onebot','status':'succeeded','notice_sent':False})
