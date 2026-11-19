import unittest

from tests import test_qq_official_outbox_migration as migration


class ConfiguredOwnerMigrationTests(unittest.TestCase):
    def test_downgrade_preserves_receipt_with_new_decision_on_legacy_action(self):
        schema = migration.owned_schema()
        self.addCleanup(migration.drop, schema)
        before, after = '0128_retire_vision_jobs', '0129_configured_owner_links'
        migration.migrate(schema, after)
        with migration.connect(schema) as db:
            fixture = migration.OfficialOutboxMigrationTests()
            fixture.connection = db
            endpoint = fixture.endpoint()
            db.execute("""INSERT INTO qq_official_inbound_events(inbox_id,account_key,endpoint_key,
                endpoint_generation,route_epoch,event_type,native_message_id,status,reason_code,echo_state,
                received_at,created_at,updated_at,control_actor_key,control_action,control_decision_code)
                VALUES ('TEST_BIND',%s,%s,1,1,'C2C_MESSAGE_CREATE','TEST_NATIVE','processed','binding_control',
                'not_echo',1,1,1,%s,'invalid','unavailable')""", (endpoint.account.key, endpoint.key, fixture.actor(endpoint).key))
        with self.assertRaisesRegex(RuntimeError, 'configured owner links require backup restore'):
            migration.migrate(schema, before, downgrade=True)
        with migration.connect(schema) as db:
            self.assertEqual(db.execute('SELECT control_decision_code FROM qq_official_inbound_events').fetchall(), [('unavailable',)])
            db.execute("UPDATE qq_official_inbound_events SET control_decision_code='invalid'")
        migration.migrate(schema, before, downgrade=True)
        with migration.connect(schema) as db:
            self.assertEqual(db.execute('SELECT control_action,control_decision_code FROM qq_official_inbound_events').fetchall(), [('invalid', 'invalid')])
