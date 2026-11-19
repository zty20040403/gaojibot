import unittest

from sqlalchemy.exc import DBAPIError

from tests import test_qq_official_outbox_migration as migration

BASE = '0117_conversation_history_floor'
HEAD = '0118_retire_semantic_state'


class RetireSemanticStateTests(unittest.TestCase):
    def test_only_obsolete_cache_drops_and_dependencies_block_upgrade(self):
        schema = migration.owned_schema()
        self.addCleanup(migration.drop, schema)
        migration.migrate(schema, BASE)
        tables = 'SELECT tablename FROM pg_tables WHERE schemaname=%s'
        with migration.connect(schema) as db:
            db.execute("INSERT INTO semantic_index_state VALUES ('TEST_SOURCE','TEST_HASH',10)")
            db.execute('CREATE VIEW test_cache_reader AS SELECT * FROM semantic_index_state')
            db.execute('''INSERT INTO semantic_documents(scope_key,source_type,source_handle,content,content_hash,
                embedding_model,embedding,created_at,updated_at) VALUES ('TEST_SCOPE','message','msg#1','TEST text',
                'TEST_HASH','TEST_MODEL',array_fill(1.0,ARRAY[1024])::vector,10,20)''')
            before = set(db.execute(tables, (schema,)))
            vector = db.execute('SELECT * FROM semantic_documents').fetchone()
        with self.assertRaises(DBAPIError):
            migration.migrate(schema, HEAD)
        with migration.connect(schema) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM test_cache_reader').fetchone(), (1,))
            db.execute('DROP VIEW test_cache_reader')
        migration.migrate(schema, HEAD)
        with migration.connect(schema) as db:
            self.assertEqual(set(db.execute(tables, (schema,))), before - {('semantic_index_state',)})
            self.assertEqual(db.execute('SELECT * FROM semantic_documents').fetchone(), vector)
        migration.migrate(schema, BASE, downgrade=True)
        with migration.connect(schema) as db:
            self.assertEqual(db.execute('SELECT * FROM semantic_index_state').fetchall(), [])
            db.execute("INSERT INTO semantic_index_state VALUES ('TEST_SOURCE','TEST_HASH',10)")
            self.assertEqual(db.execute('SELECT * FROM semantic_documents').fetchone(), vector)
