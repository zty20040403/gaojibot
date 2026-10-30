"""Real Alembic capability round trips and evidence-preserving downgrade."""
import os
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
import psycopg

from src.bot_storage.database import DatabaseError
from src.bot_storage.schema import HEAD_REVISION
from tests.qq_official_file_fixture import FileFixture, KEY
from tests.test_qq_official_image_formats import png


def migrate(case, direction, revision):
    config = Config()
    config.set_main_option('script_location', str(Path(__file__).resolve().parents[1] / 'migrations'))
    with patch.dict(os.environ, AI_POSTGRES_DSN=case.database.dsn, AI_POSTGRES_SCHEMA=case.database.schema):
        getattr(command, direction)(config, revision)


def refused(case):
    with case.assertRaisesRegex(RuntimeError, 'capability_evidence_requires_export'):
        migrate(case, 'downgrade', '0066_qq_official_migration')
    case.assertEqual(case.query('SELECT version_num FROM alembic_version')[0][0], HEAD_REVISION)


class CapabilityMigrationTests(FileFixture):
    def snapshot(self):
        queries = (
            '''SELECT c.relname,k.conname,pg_get_constraintdef(k.oid)
               FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
               JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=?
               ORDER BY c.relname,k.conname''',
            '''SELECT table_name,column_name,is_nullable,column_default,data_type,generation_expression
               FROM information_schema.columns WHERE table_schema=? ORDER BY table_name,ordinal_position''',
            '''SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=? ORDER BY indexname''',
            '''SELECT p.proname,pg_get_functiondef(p.oid) FROM pg_proc p
               JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=? ORDER BY p.proname''',
            '''SELECT c.relname,t.tgname,pg_get_triggerdef(t.oid) FROM pg_trigger t
               JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname=? AND NOT t.tgisinternal ORDER BY c.relname,t.tgname''',
        )
        return tuple([dict(row) for row in self.query(sql, (self.database.schema,))] for sql in queries)




    def test_image_sql_contract_rejects_wrong_extension_and_oversized_metadata(self):
        self.files.register(self.source, content=png(), filename='test.png', logical_request_key=KEY)
        columns = self.query('''SELECT column_name FROM information_schema.columns
            WHERE table_schema=? AND table_name='qq_official_file_artifacts' AND is_generated='NEVER' ''',
            (self.database.schema,))
        row = self.query('SELECT * FROM qq_official_file_artifacts')[0]
        original = {item['column_name']: row[item['column_name']] for item in columns}
        for changed in ({'filename': 'test.pdf'}, {'byte_size': 3500001}):
            values = original | {'artifact_id': 'f'*32, 'request_key': 'f'*64} | changed
            with self.subTest(changed=changed), self.assertRaises(DatabaseError) as caught:
                self.query('INSERT INTO qq_official_file_artifacts ('+','.join(values)+') VALUES ('+
                           ','.join('?' for _ in values)+')', tuple(values.values()))
            self.assertIsInstance(caught.exception.__cause__, psycopg.errors.CheckViolation)
            self.assertEqual(caught.exception.__cause__.diag.constraint_name, 'ck_qq_file_image_contract')
