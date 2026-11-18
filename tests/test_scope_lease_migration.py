from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest

from src.bot_storage.database import PostgresDatabase
from src.plugins.ai_chat.scope_lease import ScopeLeaseBusy, claim_scope, lock_scope, renew_scope
from tests import test_qq_official_outbox_migration as migration
from tests.test_drop_dead_tables_migration import _schema_snapshot

BASE = '0110_drop_remaining_dead_tables'
HEAD = '0111_scope_leases_checkpoints'
SCOPE = 'TEST_SCOPE'


class ScopeLeaseMigrationTests(unittest.TestCase):
    def test_upgrade_contention_expiry_and_downgrade(self):
        schema = migration.owned_schema()
        self.addCleanup(migration.drop, schema)
        migration.migrate(schema, BASE)
        with migration.connect(schema) as connection:
            before = _schema_snapshot(connection, schema)
        migration.migrate(schema, HEAD)
        db = PostgresDatabase(migration.checked_test_dsn(), schema=schema)
        self.addCleanup(db.close)
        db.require_revision(HEAD)
        barrier = Barrier(4)

        def compete(idx):
            owner = f'TEST_WORKER_{idx}'
            conn = db.store_connection()
            try:
                cursor = conn.cursor()
                barrier.wait(timeout=10)
                generation = claim_scope(cursor, SCOPE, owner, now=100, until=110)
                conn.commit()
                return owner, generation
            except ScopeLeaseBusy:
                conn.rollback()
                return None
            finally:
                conn.close()

        with ThreadPoolExecutor(4) as pool:
            winners = [result for result in pool.map(compete, range(4)) if result]
        self.assertEqual(len(winners), 1)
        owner, first = winners[0]
        self.assertEqual(first, 1)
        conn = db.store_connection()
        self.addCleanup(conn.close)
        cursor = conn.cursor()
        lock_scope(cursor, SCOPE)  # Ordinary ledger writes do not take over the turn.
        renew_scope(cursor, SCOPE, owner, first, now=109, until=120)
        with self.assertRaises(ScopeLeaseBusy):
            claim_scope(cursor, SCOPE, 'TEST_NEXT', now=119, until=130)
        with self.assertRaises(ScopeLeaseBusy):
            renew_scope(cursor, SCOPE, owner, first, now=120, until=140)
        self.assertEqual(claim_scope(cursor, SCOPE, owner, now=120, until=130), first + 1)
        for stale_owner, stale_generation in ((owner, first), ('TEST_OTHER', first + 1)):
            with self.assertRaises(ScopeLeaseBusy):
                renew_scope(cursor, SCOPE, stale_owner, stale_generation, now=121, until=140)
        conn.commit()
        migration.migrate(schema, BASE, downgrade=True)
        with migration.connect(schema) as connection:
            self.assertEqual(_schema_snapshot(connection, schema), before)
