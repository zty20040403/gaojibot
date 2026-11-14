"""Empty rollback preserves old consent; private question audit needs export."""
import unittest
from unittest.mock import patch

from src.bot_storage import DatabaseError,PostgresDatabase
from src.plugins.ai_chat.platform_types import EndpointKey
from tests import test_conversation_question_schema as fixture,test_conversation_link_schema as link_fixture,test_qq_official_outbox_migration as migration
from tests.conversation_question_fixture import QuestionFixture
from tests.qq_official_turn_fixture import ExecutionFixture


class ConversationQuestionMigrationTests(unittest.TestCase):
    def test_real_legacy_null_receipt_migrates_but_cannot_authorize_new_question(self):
        schema = migration.owned_schema(); self.addCleanup(migration.drop,schema)
        migration.migrate(schema,'0099_link_role_evidence')
        database = PostgresDatabase(migration.checked_test_dsn(),schema=schema); self.addCleanup(database.close)
        case = ExecutionFixture('runTest'); self.addCleanup(case.doCleanups)
        with patch('tests.qq_official_outbox_fixture.postgres_for_test',return_value=database):case.setUp()
        case.target = EndpointKey(case.account,'group','TEST_LEGACY_QUESTION_TARGET'); case.provision(case.target)
        def insert(table,row):
            with case.tx() as cursor:cursor.execute('INSERT INTO '+table+'('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
        case.insert = insert
        old = link_fixture.ConversationLinkSchemaTests.challenge(case,**link_fixture.ConversationLinkSchemaTests.confirmation(case),
            state='active',delivery_state='committed',code_hmac='',activated_at=case.now+2)
        edge = link_fixture.ConversationLinkSchemaTests.link(case,old)
        case.edge = dict(case.query('SELECT * FROM qq_official_conversation_links WHERE link_id=?',(edge['link_id'],))[0])
        case.deadline = case.now+10000; case.handle = case.claim().handle; case.begin(case.handle); case.now += 2
        migration.migrate(schema,'0104_conversation_questions')
        self.assertIsNone(case.query('SELECT delivery_receipt_hash FROM qq_official_link_challenges')[0]['delivery_receipt_hash'])
        with self.assertRaises(DatabaseError) as caught:QuestionFixture.insert_question(case)
        self.assertIn('question_original_source_required',str(caught.exception.__cause__))
        self.assertEqual(case.query('SELECT * FROM qq_official_conversation_questions'),[])

    def test_empty_increment_retains_original_links_and_turns_but_audit_blocks_downgrade(self):
        schema = migration.owned_schema(); self.addCleanup(migration.drop,schema)
        migration.migrate(schema,'0103_link_feedback')
        database = PostgresDatabase(migration.checked_test_dsn(),schema=schema); self.addCleanup(database.close)
        case = fixture.ConversationQuestionSchemaTests('runTest'); self.addCleanup(case.doCleanups)
        with patch('tests.qq_official_outbox_fixture.postgres_for_test',return_value=database):case.setUp()
        tables = ('qq_official_conversation_links','qq_official_link_challenges','qq_official_turn_executions')
        before = {table:case.query('SELECT * FROM '+table) for table in tables}
        migration.migrate(schema,'0104_conversation_questions'); migration.migrate(schema,'0103_link_feedback',downgrade=True)
        self.assertEqual(before,{table:case.query('SELECT * FROM '+table) for table in tables})
        migration.migrate(schema,'0104_conversation_questions'); question = case.insert_question()
        with self.assertRaisesRegex(RuntimeError,'conversation_questions_require_export'):
            migration.migrate(schema,'0103_link_feedback',downgrade=True)
        self.assertEqual(case.query('SELECT * FROM qq_official_conversation_questions'),[question])
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT version_num FROM alembic_version').fetchone(),('0104_conversation_questions',))
