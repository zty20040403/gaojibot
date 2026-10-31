"""A tool lease loss must not strand a shared delivery-store transaction."""
from src.plugins.ai_chat.execution_control import LeaseLost
from src.plugins.ai_chat.delivery import DeliveryStore
from tests.qq_official_outbox_fixture import OutboxFixture


class DeliveryTransactionRollbackTests(OutboxFixture):
    def test_baseexception_rolls_back_and_leaves_store_usable(self):
        deliveries = DeliveryStore(self.database, recover_interrupted=False)
        self.addCleanup(deliveries.close)
        with self.assertRaises(LeaseLost), deliveries.transaction() as cursor:
            cursor.execute("UPDATE qq_official_endpoints SET generation=generation+1 WHERE endpoint_key=?",
                           (self.endpoint.key,))
            raise LeaseLost("TEST_LEASE_LOST")
        # Opening another public transaction would fail while the first one
        # remained active; no private connection repair is allowed here.
        with deliveries.transaction() as cursor:
            row = cursor.execute("SELECT generation FROM qq_official_endpoints WHERE endpoint_key=?",
                                 (self.endpoint.key,)).fetchone()
        self.assertEqual(row["generation"], 1)
