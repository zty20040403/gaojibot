import unittest

from tests import test_qq_official_outbox_migration as migration


class RetireApprovalReceiptsTests(unittest.TestCase):
    def test_existing_decision_blocks_retirement_and_gateway_evidence_is_preserved(self):
        schema = migration.owned_schema()
        self.addCleanup(migration.drop, schema)
        before, after = '0123_retire_outbox_diagnostics', '0124_retire_approval_receipts'
        migration.migrate(schema, before)
        with migration.connect(schema) as db:
            fixture = migration.OfficialOutboxMigrationTests()
            fixture.connection = db
            endpoint = fixture.endpoint()
            actor = fixture.actor(endpoint)
            db.execute("""INSERT INTO qq_official_inbound_events(inbox_id,account_key,endpoint_key,endpoint_generation,
                route_epoch,event_type,native_message_id,status,reason_code,echo_state,received_at,created_at,updated_at,
                admin_control_actor_key,admin_control_commitment) VALUES ('TEST_CONTROL',%s,%s,1,1,'C2C_MESSAGE_CREATE',
                'TEST_NATIVE','processed','admin_control','not_echo',1,1,1,%s,%s)""",
                       (endpoint.account.key, endpoint.key, actor.key, 'a' * 64))
            db.execute("""INSERT INTO qq_official_gateway_receipts(account_key,session_id,seq,event_type,disposition,
                inbox_id,received_at) VALUES (%s,'TEST_SESSION',1,'C2C_MESSAGE_CREATE','control','TEST_CONTROL',1)""",
                       (endpoint.account.key,))
            db.execute("""INSERT INTO qq_official_admin_control_receipts(inbox_id,account_key,endpoint_key,actor_key,
                source_session_id,source_seq,action,decision,created_at) VALUES
                ('TEST_CONTROL',%s,%s,%s,'TEST_SESSION',1,'invalid','denied',1)""", (endpoint.account.key, endpoint.key, actor.key))
        with self.assertRaisesRegex(RuntimeError, 'preserve existing approval control receipts'):
            migration.migrate(schema, after)
        with migration.connect(schema) as db:
            self.assertEqual(db.execute('SELECT decision FROM qq_official_admin_control_receipts').fetchall(), [('denied',)])
            db.execute('DELETE FROM qq_official_admin_control_receipts')
        migration.migrate(schema, after)
        with migration.connect(schema) as db:
            self.assertEqual(db.execute('SELECT inbox_id FROM qq_official_gateway_receipts').fetchall(), [('TEST_CONTROL',)])
            self.assertEqual(db.execute('SELECT admin_control_commitment FROM qq_official_inbound_events').fetchall(), [('a' * 64,)])
        with self.assertRaisesRegex(RuntimeError, 'pre-0124 full database backup'):
            migration.migrate(schema, before, downgrade=True)
