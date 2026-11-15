"""Actual old 0104 rows remain private; notice audit requires export to roll back."""
import unittest
from unittest.mock import patch
import psycopg
from sqlalchemy import event
from sqlalchemy.engine import Engine

from src.bot_storage import DatabaseError,PostgresDatabase
from tests import test_qq_official_outbox_migration as migration
from tests.question_notice_fixture import NoticeFixture


class QuestionNoticeSourceMigrationTests(unittest.TestCase):
    def old_case(self):
        schema = migration.owned_schema(); self.addCleanup(migration.drop,schema)
        migration.migrate(schema,'0104_conversation_questions')
        database = PostgresDatabase(migration.checked_test_dsn(),schema=schema); self.addCleanup(database.close)
        case = NoticeFixture('runTest'); self.addCleanup(case.doCleanups)
        with patch('tests.qq_official_outbox_fixture.postgres_for_test',return_value=database):case.setUp()
        return schema,case

    def test_empty_increment_preserves_question_and_queue_then_notice_audit_blocks_rollback(self):
        schema,case = self.old_case()
        tables = ('qq_official_conversation_questions','durable_jobs','qq_official_accounts','qq_official_endpoints')
        before = {table:case.query('SELECT * FROM '+table) for table in tables}
        migration.migrate(schema,'0105_question_notice_sources')
        migration.migrate(schema,'0104_conversation_questions',downgrade=True)
        self.assertEqual(before,{table:case.query('SELECT * FROM '+table) for table in tables})
        migration.migrate(schema,'0105_question_notice_sources'); row = case.notice()
        with self.assertRaisesRegex(RuntimeError,'question_notice_sources_require_export_before_downgrade'):
            migration.migrate(schema,'0104_conversation_questions',downgrade=True)
        self.assertEqual(case.query('SELECT * FROM qq_official_question_notice_sources'),[row])
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT version_num FROM alembic_version').fetchone(),('0105_question_notice_sources',))

    def test_downgrade_blocks_a_concurrent_insert_before_its_empty_check(self):
        schema,case = self.old_case()
        migration.migrate(schema,'0105_question_notice_sources')
        observed = dict(checked=False,blocked=False,notice=None)
        def after_cursor_execute(_connection,_cursor,statement,*_):
            if ' '.join(statement.lower().split())!='select 1 from qq_official_question_notice_sources limit 1':return
            observed['checked'] = True
            try:
                with case.tx() as cursor:
                    cursor.execute('LOCK TABLE qq_official_question_notice_sources IN ROW EXCLUSIVE MODE NOWAIT')
            except DatabaseError as error:
                self.assertIsInstance(error.__cause__,psycopg.errors.LockNotAvailable)
                observed['blocked'] = True
            else:
                observed['notice'] = case.notice()  # The old empty SELECT has already executed.
        event.listen(Engine,'after_cursor_execute',after_cursor_execute)
        try:migration.migrate(schema,'0104_conversation_questions',downgrade=True)
        finally:event.remove(Engine,'after_cursor_execute',after_cursor_execute)
        self.assertTrue(observed['checked'])
        self.assertTrue(observed['blocked'],f"Concurrent audit was accepted then dropped: {observed['notice'] is not None}")

    def test_closed_legacy_question_is_never_given_new_public_authority(self):
        schema,case = self.old_case()
        case.query("UPDATE qq_official_conversation_questions SET state='cancelled',reason_code='restart',finished_at=? WHERE question_id=?",
            (case.now,case.question['question_id']))
        migration.migrate(schema,'0105_question_notice_sources')
        with self.assertRaises(DatabaseError) as caught:case.notice()
        self.assertIn('question_notice_original_source_required',str(caught.exception.__cause__))
        self.assertEqual(case.query('SELECT * FROM qq_official_question_notice_sources'),[])
        self.assertEqual(case.query('SELECT * FROM delivery_plans'),[])
