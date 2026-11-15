"""Empty 0106 rollback preserves exact 0103/0105 constraints and blocks concurrent inserts."""
import unittest
from unittest.mock import patch
import psycopg
from sqlalchemy import event
from sqlalchemy.engine import Engine

from src.bot_storage import DatabaseError,PostgresDatabase
from tests import test_qq_official_outbox_migration as migration,test_question_notice_schema as schema_tests


class QuestionNoticeMigrationTests(unittest.TestCase):
    def old_case(self):
        schema = migration.owned_schema(); self.addCleanup(migration.drop,schema)
        migration.migrate(schema,'0105_question_notice_sources')
        database = PostgresDatabase(migration.checked_test_dsn(),schema=schema); self.addCleanup(database.close)
        case = schema_tests.QuestionNoticeSchemaTests('runTest'); self.addCleanup(case.doCleanups)
        with patch('tests.qq_official_outbox_fixture.postgres_for_test',return_value=database):case.setUp()
        return schema,case

    def constraints(self,schema):
        with migration.connect(schema) as connection:
            return dict(connection.execute("SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE conname IN ('delivery_plans_check2','delivery_plans_purpose_check','ck_deliveries_official_fields','ck_deliveries_ephemeral') AND connamespace=%s::regnamespace",(schema,)).fetchall())

    def test_empty_increment_restores_exact_constraints_and_audit_blocks_rollback(self):
        schema,case = self.old_case()
        source = case.notice()
        before = self.constraints(schema)
        migration.migrate(schema,'0106_question_notices')
        migration.migrate(schema,'0105_question_notice_sources',downgrade=True)
        self.assertEqual(before,self.constraints(schema))
        self.assertEqual(case.query('SELECT * FROM qq_official_question_notice_sources'),[source])

        migration.migrate(schema,'0106_question_notices')
        notice = case.question_notice(source)
        with self.assertRaisesRegex(RuntimeError,'question_notices_require_export_before_downgrade'):
            migration.migrate(schema,'0105_question_notice_sources',downgrade=True)
        self.assertEqual(case.query('SELECT * FROM qq_official_question_notices'),[notice])
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT version_num FROM alembic_version').fetchone(),('0106_question_notices',))

    def test_downgrade_blocks_a_concurrent_notice_or_plan_insert_before_empty_check(self):
        for target in ('qq_official_question_notices','delivery_plans'):
            with self.subTest(target=target):
                schema,case = self.old_case()
                source = case.notice()
                migration.migrate(schema,'0106_question_notices')
                observed = dict(checked=False,blocked=False,created=False)
                def after_cursor_execute(_connection,_cursor,statement,*_):
                    if 'select 1 from qq_official_question_notices' not in ' '.join(statement.lower().split()):return
                    observed['checked'] = True
                    try:
                        with case.tx() as cursor:
                            cursor.execute(f'LOCK TABLE {target} IN ROW EXCLUSIVE MODE NOWAIT')
                    except DatabaseError as error:
                        self.assertIsInstance(error.__cause__,psycopg.errors.LockNotAvailable)
                        observed['blocked'] = True
                    else:
                        if target=='qq_official_question_notices':case.question_notice(source)
                        else:case.notice_plan(source)
                        observed['created'] = True
                event.listen(Engine,'after_cursor_execute',after_cursor_execute)
                try:migration.migrate(schema,'0105_question_notice_sources',downgrade=True)
                finally:event.remove(Engine,'after_cursor_execute',after_cursor_execute)
                self.assertTrue(observed['checked'])
                self.assertTrue(observed['blocked'],f'Concurrent write on {target} raced downgrade: {observed["created"]}')

    def test_plan_audit_without_notice_row_also_blocks_downgrade(self):
        schema,case = self.old_case()
        source = case.notice()
        migration.migrate(schema,'0106_question_notices')
        plan = case.notice_plan(source)
        with self.assertRaisesRegex(RuntimeError,'question_notices_require_export_before_downgrade'):
            migration.migrate(schema,'0105_question_notice_sources',downgrade=True)
        self.assertEqual(case.query('SELECT question_notice_id FROM delivery_plans WHERE plan_id=?',(plan,)),
            [dict(question_notice_id=source['notice_id'])])
