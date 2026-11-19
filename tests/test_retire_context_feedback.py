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
                journal.record_tool_started(turn.turn_id, 1, 'TEST_TOOL', {'text': 'TEST_INPUT'}, ())
                journal.record_tool_finished(turn.turn_id, 1, 'TEST_TOOL', 'succeeded', 'TEST_RESULT', ())
                trace = AgentTrace(trace_id='a' * 32, provider='TEST_PROVIDER', model='TEST_MODEL')
                journal.finish_turn(turn.turn_id, status='succeeded', final_text='TEST_ANSWER', trace_payload=trace.to_payload())
                with migration.connect(schema) as db:
                    db.execute('''INSERT INTO turn_context_plans(turn_id,scope_key,current_message_id,resolver_version,
                               context_hash,created_at) VALUES (%s,%s,1,'TEST_RESOLVER','TEST_HASH',10)''', (turn.turn_id, scope.key))
                    db.execute('INSERT INTO turn_context_feedback VALUES (%s,%s,%s,1,%s,10,20)',
                               (turn.turn_id, verdict, 'TEST_NOTE', 'TEST_ADMIN'))
                if verdict == 'correct':
                    journal.hide_history(scope)
            traces = journal.recent_trace_summaries()
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
            self.assertEqual(journal.recent_trace_summaries(), traces)
        with self.assertRaisesRegex(RuntimeError, 'pre-0120 full database backup'):
            migration.migrate(schema, BASE, downgrade=True)
        with migration.connect(schema) as db:
            self.assertEqual(_schema_snapshot(db, schema), expected)
            self.assertEqual(db.execute('SELECT version_num FROM alembic_version').fetchone(), (HEAD,))

        migration.migrate(schema, '0124_retire_approval_receipts')
        with migration.connect(schema) as db:
            tables = _schema_snapshot(db, schema)[0]
            self.assertEqual(db.execute('SELECT count(*) FROM turn_context_plans').fetchone(), (2,))
            kept = tables - {'turn_context_plans', 'alembic_version'}
            before = rows(db, kept)
        migration.migrate(schema, '0125_retire_turn_context_plans')
        with migration.connect(schema) as db:
            self.assertEqual(tables - _schema_snapshot(db, schema)[0], {'turn_context_plans'})
            self.assertEqual(rows(db, kept), before)
        with self.assertRaisesRegex(RuntimeError, 'pre-0125 full database backup'):
            migration.migrate(schema, '0124_retire_approval_receipts', downgrade=True)
        with migration.connect(schema) as db:
            db.execute("""INSERT INTO turn_edges(from_turn_id,to_turn_id,kind,created_at)
                       SELECT max(turn_id),min(turn_id),'fork-from',10 FROM agent_turns""")
            kept.remove('turn_edges')
            before = rows(db, kept)
        migration.migrate(schema, '0126_retire_turn_edges')
        with migration.connect(schema) as db:
            self.assertEqual(_schema_snapshot(db, schema)[0], kept | {'alembic_version'})
            self.assertEqual(rows(db, kept), before)
        with self.assertRaisesRegex(RuntimeError, 'pre-0126 full database backup'):
            migration.migrate(schema, '0125_retire_turn_context_plans', downgrade=True)
        with migration.connect(schema) as db:
            source, = db.execute("""INSERT INTO content_sources(platform,canonical_url,content_kind,first_seen_at,
                last_seen_at,created_at,updated_at) VALUES('web','https://example.test/old','webpage',1,1,1,1)
                RETURNING source_id""").fetchone()
            db.execute("""INSERT INTO message_sources(source_id,scope_key,native_message_id,sender_native_user_id,
                segment_index,created_at) VALUES(%s,'TEST_SCOPE','TEST_MESSAGE','TEST_SENDER',0,1)""", (source,))
            kept -= {'message_sources', 'content_sources'}
            before = rows(db, kept)
        migration.migrate(schema, '0127_retire_legacy_shares')
        with migration.connect(schema) as db:
            self.assertEqual(_schema_snapshot(db, schema)[0], kept | {'alembic_version'})
            self.assertEqual(rows(db, kept), before)
        with self.assertRaisesRegex(RuntimeError, 'pre-0127 full database backup'):
            migration.migrate(schema, '0126_retire_turn_edges', downgrade=True)
        with migration.connect(schema) as db:
            db.execute("""INSERT INTO vision_jobs(scope_key,native_message_id,segment_index,requester_native_user_id,
                created_at,updated_at,next_attempt_at) VALUES('TEST_SCOPE','TEST_MESSAGE',0,'TEST_SENDER',1,1,1)""")
        with self.assertRaisesRegex(RuntimeError, 'preserve existing vision jobs'):
            migration.migrate(schema, '0128_retire_vision_jobs')
        with migration.connect(schema) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM vision_jobs').fetchone(), (1,))
            db.execute('DELETE FROM vision_jobs')
            kept.remove('vision_jobs')
            before = rows(db, kept)
        migration.migrate(schema, '0128_retire_vision_jobs')
        with migration.connect(schema) as db:
            self.assertEqual(_schema_snapshot(db, schema)[0], kept | {'alembic_version'})
            self.assertEqual(rows(db, kept), before)
