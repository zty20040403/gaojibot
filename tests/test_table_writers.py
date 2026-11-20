from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tests import test_qq_official_outbox_migration as migration
from tools.dev.table_writers import writer_tables


class TableWriterTests(unittest.TestCase):
    def test_every_business_table_has_a_host_writer(self):
        schema = migration.owned_schema()
        self.addCleanup(migration.drop, schema)
        migration.migrate(schema, 'head')
        with migration.connect(schema) as db:
            tables = {name for name, in db.execute('SELECT tablename FROM pg_tables WHERE schemaname=%s', (schema,))}
        business_tables = tables - {'alembic_version'}
        self.assertEqual(len(business_tables), 52)
        self.assertFalse(business_tables - writer_tables(Path(__file__).resolve().parents[1] / 'src'))

    def test_only_executed_insert_and_update_targets_count(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sample.py').write_text('''"""INSERT INTO doc_example(a) VALUES(1)"""
# db.execute('UPDATE comment SET a=0')
db.execute('SELECT * FROM read_only')
db.execute('DELETE FROM cleanup')
db.execute(f'UPDATE may_be_prefix{suffix} SET a=?', (1,))
db.execute('INSERT INTO kept(a) VALUES (?) ON CONFLICT DO UPDATE SET a=1', (1,))
db.executemany('UPDATE "updated" SET a=? WHERE ' + scope, args)
''')
            self.assertEqual(writer_tables(root), {'kept', 'updated'})
