import unittest
from unittest.mock import patch
import psycopg

from src.bot_storage import PostgresDatabase
from tests import test_conversation_link_schema as fixture,test_qq_official_outbox_migration as migration


class LinkReceiptMigrationTests(unittest.TestCase):
    def test_unknown_old_receipts_stay_unknown_and_only_empty_increment_downgrades(self):
        schema = migration.owned_schema(); self.addCleanup(migration.drop,schema)
        migration.migrate(schema,'0099_link_role_evidence')
        database = PostgresDatabase(migration.checked_test_dsn(),schema=schema); self.addCleanup(database.close)
        case = fixture.ConversationLinkSchemaTests('runTest'); self.addCleanup(case.doCleanups)
        with patch('tests.qq_official_outbox_fixture.postgres_for_test',return_value=database):case.setUp()
        old = case.challenge(**case.confirmation(),state='active',delivery_state='committed',code_hmac='',activated_at=case.now+2)
        pending = case.challenge(state='cancelled',code_hmac='')
        migration.migrate(schema,'0100_link_receipts')
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT delivery_receipt_hash,delivery_received_at FROM qq_official_link_challenges WHERE challenge_id=%s',
                (old['challenge_id'],)).fetchone(),(None,None))
            self.assertEqual(connection.execute('SELECT count(*) FROM qq_official_conversation_links').fetchone(),(0,))
            self.assertEqual(connection.execute('SELECT state FROM qq_official_link_challenges WHERE challenge_id=%s',
                (old['challenge_id'],)).fetchone(),('active',))
        with self.assertRaisesRegex(psycopg.Error,'link_requires_bilateral_committed_proof'):case.link(old)
        migration.migrate(schema,'0099_link_role_evidence',downgrade=True)
        # The exact strict pre-0100 terminal trigger is restored, not weakened.
        with self.assertRaisesRegex(psycopg.Error,'immutable_link_challenge'):
            case.query("UPDATE qq_official_link_challenges SET delivery_state='unknown' WHERE challenge_id=?",(pending['challenge_id'],))
        migration.migrate(schema,'0100_link_receipts')
        case.query("UPDATE qq_official_link_challenges SET delivery_state='committed',delivery_receipt_hash=?,delivery_received_at=? WHERE challenge_id=?",
            ('a'*64,case.now+700,pending['challenge_id']))
        with self.assertRaisesRegex(RuntimeError,'link_receipts_require_export'):
            migration.migrate(schema,'0099_link_role_evidence',downgrade=True)
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT state,delivery_receipt_hash FROM qq_official_link_challenges WHERE challenge_id=%s',
                (pending['challenge_id'],)).fetchone(),('cancelled','a'*64))
            self.assertEqual(connection.execute('SELECT version_num FROM alembic_version').fetchone(),('0100_link_receipts',))
