from contextlib import closing
import unittest

from psycopg import sql
from sqlalchemy.exc import DBAPIError

from src.bot_storage import PostgresDatabase
from src.plugins.ai_chat.admin_control import AdminControlStore
from src.plugins.ai_chat.agent_loop import AgentTrace
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey
from src.plugins.ai_chat.turn_journal import TurnJournal
from tests import test_qq_official_outbox_migration as migration
from tests.test_drop_dead_tables_migration import _schema_snapshot

BASE = '0119_retire_import_residue'
HEAD = '0120_retire_context_feedback'
TABLE = 'turn_context_feedback'


class RetireContextFeedbackTests(unittest.TestCase):
    def test_only_feedback_drops_atomically_preserving_plans_traces_and_audit(self):
        schema = migration.owned_schema()
        self.addCleanup(migration.drop, schema)
        migration.migrate(schema, BASE)
        scope = EndpointKey(AccountKey('qq-official', 'TEST_APP', 'test'), 'group', 'TEST_CONTEXT').scope
        with closing(PostgresDatabase(migration.checked_test_dsn(), schema=schema)) as store, closing(TurnJournal(store)) as journal:
            control = AdminControlStore(store)
            control.mutate('traces', expected_version=0, actor='TEST_ADMIN', action='TEST_ACTION', operation=lambda version: version)
            for verdict in ('correct', 'off_topic'):
                turn = journal.start_turn(scope, trigger_canonical_message_id=1,
                                          objective='TEST_OBJECTIVE', provider='TEST_PROVIDER', model='TEST_MODEL')
                journal.record_context_plan(turn.turn_id, {'scope_key': scope.key, 'current_message_id': 1,
                                                          'context_hash': 'TEST_HASH', 'resolver_version': 'TEST_RESOLVER'})
                journal.record_tool_started(turn.turn_id, 1, 'TEST_TOOL', {'text': 'TEST_INPUT'}, ())
                journal.record_tool_finished(turn.turn_id, 1, 'TEST_TOOL', 'succeeded', 'TEST_RESULT', ())
                trace = AgentTrace(trace_id='a' * 32, provider='TEST_PROVIDER', model='TEST_MODEL')
                journal.finish_turn(turn.turn_id, status='succeeded', final_text='TEST_ANSWER', trace_payload=trace.to_payload())
                with migration.connect(schema) as db:
                    db.execute('INSERT INTO turn_context_feedback VALUES (%s,%s,%s,1,%s,10,20)',
                               (turn.turn_id, verdict, 'TEST_NOTE', 'TEST_ADMIN'))
                if verdict == 'correct':
                    journal.hide_history(scope)
            plans = journal.recent_context_plans()
            traces = journal.recent_trace_summaries()
            self.assertEqual([plan['turn_id'] for plan in plans], [turn.turn_id])
            self.assertEqual(len(traces), 2)
            self.assertEqual(traces[0]['trace_id'], 'a' * 32)
            self.assertEqual(traces[0]['tools'], ['TEST_TOOL'])
            self.assertEqual(len(control.audit()), 1)
            with migration.connect(schema) as db:
                before = _schema_snapshot(db, schema)
                feedback_indexes = {name for name, in db.execute(
                    'SELECT indexname FROM pg_indexes WHERE schemaname=%s AND tablename=%s', (schema, TABLE))}
                def rows(db, tables):
                    return {table: sorted(db.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(table))).fetchall(), key=repr)
                            for table in tables}
                original = rows(db, before[0])
                self.assertEqual(len(original[TABLE]), 2)
                for table in ('agent_turns', 'turn_context_plans', 'turn_archives', 'turn_journal_events', 'admin_audit_log'):
                    self.assertTrue(original[table], table)
                db.execute('CREATE VIEW test_feedback_reader AS SELECT * FROM turn_context_feedback')
                blocked = _schema_snapshot(db, schema)
            with self.assertRaises(DBAPIError) as failure:
                migration.migrate(schema, HEAD)
            self.assertEqual(failure.exception.orig.sqlstate, '2BP01')
            with migration.connect(schema) as db:
                self.assertEqual(_schema_snapshot(db, schema), blocked)
                self.assertEqual(rows(db, before[0]), original)
                self.assertEqual(len(db.execute('SELECT * FROM test_feedback_reader').fetchall()), 2)
                self.assertEqual(db.execute('SELECT version_num FROM alembic_version').fetchone(), (BASE,))
                db.execute('DROP VIEW test_feedback_reader')
            migration.migrate(schema, HEAD)
            expected = (before[0] - {TABLE}, {row for row in before[1] if row[0] != TABLE},
                        {row for row in before[2] if row[0] != TABLE}, before[3] - feedback_indexes)
            with migration.connect(schema) as db:
                self.assertEqual(_schema_snapshot(db, schema), expected)
                self.assertEqual(rows(db, before[0] - {TABLE, 'alembic_version'}),
                                 {table: data for table, data in original.items() if table not in {TABLE, 'alembic_version'}})
                self.assertEqual(db.execute('SELECT version_num FROM alembic_version').fetchone(), (HEAD,))
            self.assertEqual(journal.recent_context_plans(), plans)
            self.assertEqual(journal.recent_context_plans(turn_id=turn.turn_id), plans)
            self.assertEqual(journal.recent_trace_summaries(), traces)
            journal.record_context_plan(turn.turn_id, {'scope_key': scope.key, 'current_message_id': 2})
            self.assertEqual(journal.recent_context_plans()[0]['current_message_id'], 2)
            journal.hide_history(scope)
            self.assertEqual(journal.recent_context_plans(), [])
        with self.assertRaisesRegex(RuntimeError, 'pre-0120 full database backup'):
            migration.migrate(schema, BASE, downgrade=True)
        with migration.connect(schema) as db:
            self.assertEqual(_schema_snapshot(db, schema), expected)
            self.assertEqual(db.execute('SELECT version_num FROM alembic_version').fetchone(), (HEAD,))
