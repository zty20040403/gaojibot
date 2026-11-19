"""Security transitions across independent PostgreSQL connection pools."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

from psycopg.errors import LockNotAvailable

from src.bot_security.passwords import hash_password
from src.bot_security.store import SecurityError, SecurityStore
from src.bot_storage.database import DatabaseError, PostgresDatabase
from src.bot_storage.schema import HEAD_REVISION
from tests import test_qq_official_outbox_migration as migration
from tests.test_account_security import BOT, PASSWORD, QQ


class SecurityPostgresTests(unittest.TestCase):
    def setUp(self):
        self.schema = migration.owned_schema()
        self.addCleanup(migration.drop, self.schema)

    def test_quorum_upgrade_invalidates_legacy_grants_without_replaying_them(self):
        migration.migrate(self.schema, '0038_task_collaboration')
        with migration.connect(self.schema) as db:
            db.execute("INSERT INTO admin_accounts VALUES ('owner','owner','unused','admin',%s,1,1,1)", (QQ,))
            for identifier, kind, status in (('grant', 'server_task', 'succeeded'), ('active', 'fleet', 'executing'),
                                             ('pending', 'test', 'pending'), ('finished', 'test', 'succeeded')):
                db.execute("""INSERT INTO admin_approvals
                    (approval_id,account_id,account_version,qq_id,bot_id,session_hash,kind,payload_json,
                     payload_hash,summary,code_hash,expires_at,status,created_at,updated_at)
                    VALUES (%s,'owner',1,%s,%s,'',%s,'{}','legacy-hash','legacy-summary','legacy-otp',99999,%s,1,1)""",
                    (identifier, QQ, BOT, kind, status))
        migration.migrate(self.schema, HEAD_REVISION)
        with migration.connect(self.schema) as db:
            rows = db.execute('SELECT approval_id,status,quorum FROM admin_approvals').fetchall()
        self.assertEqual({row[0]: row[1:] for row in rows}, {'grant': ('cancelled', 2),
            'active': ('needs_attention', 2), 'pending': ('cancelled', 1), 'finished': ('succeeded', 1)})

    def test_migration_atomic_confirmation_rollback_and_restart(self):
        base = '0121_attention_reason'
        tables = 'SELECT tablename FROM pg_tables WHERE schemaname=current_schema()'
        migration.migrate(self.schema, base)
        with migration.connect(self.schema) as db:
            db.execute('INSERT INTO admin_security_lock VALUES (1, 42)')
            before = set(db.execute(tables).fetchall())
        migration.migrate(self.schema, '0122_retire_security_lock')
        with migration.connect(self.schema) as db:
            self.assertEqual(set(db.execute(tables).fetchall()), before - {('admin_security_lock',)})
        migration.migrate(self.schema, HEAD_REVISION)
        first, second = (self.store() for _ in range(2))
        with first.transaction() as c, first.enlist(c):
            self.assertIsNone(first.claim('TEST_IDLE'))
            self.assertEqual(first.unsent_receipts(), [])
            self.assertEqual(c.execute('SELECT relname FROM pg_stat_xact_user_tables WHERE schemaname=current_schema() '
                                       'AND n_tup_ins+n_tup_upd+n_tup_del>0').fetchall(), [])
        account = first.bootstrap('test_admin', PASSWORD, QQ)
        login = second.login('test_admin', PASSWORD, 'TEST_PEER')
        session = first.session(login['session'], login['csrf'])
        request, code = first.propose(session, bot_id=BOT, kind='test', payload={'unit': 'test.service'}, summary='测试')
        first.delivered(request['approval_id'], 1, True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(lambda store: self.confirm(store, request['approval_id'], code), (first, second))), 1)
            claimed = pool.map(lambda store: store.claim('TEST_WORKER'), (first, second))
            self.assertEqual(sum(item is not None for item in claimed), 1)
        with self.assertRaisesRegex(RuntimeError, 'TEST_ROLLBACK'), first.transaction() as c:
            c.execute('DELETE FROM admin_sessions')
            raise RuntimeError('TEST_ROLLBACK')
        self.assertEqual(second.session(login['session'])['account_id'], account['account_id'])
        with first.transaction() as c:
            c.execute('UPDATE admin_approvals SET lease_until=0 WHERE approval_id=?', (request['approval_id'],))
        self.assertIsNone(second.claim('restarted'))
        self.assertEqual(second.get(request['approval_id'], account['account_id'])['status'], 'needs_attention')
        with self.assertRaises(SecurityError):
            first.confirm(request['approval_id'], QQ, BOT, code)
        self.assertNotIn(code, str(first.audit()))

    def store(self):
        database = PostgresDatabase(migration.checked_test_dsn(), schema=self.schema, min_size=1, max_size=6)
        self.addCleanup(database.close)
        database.require_revision(HEAD_REVISION)
        return SecurityStore(database, b'TEST_SECURITY_SECRET' * 2)

    def confirm(self, store, approval, code):
        try:
            store.confirm(approval, QQ, BOT, code)
        except SecurityError:
            return False
        return True

    def test_contending_changes_cannot_disable_both_admins(self):
        migration.migrate(self.schema, HEAD_REVISION)
        first, second = self.store(), self.store()
        a = first.bootstrap('test_admin', PASSWORD, QQ)
        b = second.apply_account_change(a, dict(action='create', username='test_other', role='admin',
                                                qq_id='123456780', password_hash=hash_password(PASSWORD)))
        def disable(store, admin):
            return store.apply_account_change(admin, dict(action='update', account_id=admin['account_id'],
                                                          expected_version=admin['version'], changes={'enabled': False}))
        with closing(first._database.store_connection()) as connection, first.enlist(connection.cursor()):
            self.assertFalse(disable(first, a)['enabled'])
            with self.assertRaises(DatabaseError) as error, closing(second._database.store_connection()) as contender:
                other = contender.cursor()
                other.execute("SET LOCAL lock_timeout = '150ms'")
                with second.enlist(other):
                    disable(second, b)
            self.assertIsInstance(error.exception.__cause__, LockNotAvailable)
            connection.commit()
        with self.assertRaisesRegex(SecurityError, '最后一个管理员'):
            disable(second, b)
        self.assertEqual([admin for admin in second.accounts() if admin['enabled']], [b])
