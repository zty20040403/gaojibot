import unittest

from src.plugins.ai_chat.adapters.qq_official import execution
from tests import test_qq_official_execution as fixture
from src.plugins.ai_chat.adapters.qq_official.turn_input import load_turn_input
from src.plugins.ai_chat.message_ir import MessageBody, TextNode


class TurnInputTests(fixture.ExecutionTests):
    def load(self, handle):
        with self.tx() as cursor:
            return load_turn_input(cursor, execution_api=execution, **self.args(handle))

    def test_input_uses_durable_body_identity_and_original_times_without_raw_payload(self):
        window = self.window(source="TEST_INPUT")
        handle = self.claim().handle
        item = self.load(handle)
        self.assertEqual(item.endpoint, self.endpoint)
        self.assertEqual(item.actor, self.actor)
        self.assertEqual(item.body, self.message.body)
        self.assertEqual(item.occurred_at, self.now - 5)
        self.assertEqual(item.route_epoch, 1)
        stored = self.query("SELECT * FROM qq_official_reply_windows WHERE window_id=?", (window,))[0]
        self.assertEqual(item.reply_credential.expires_at, stored["expires_at"])
        self.assertEqual(item.reply_credential.received_at, stored["received_at"])

    def test_input_original_expired_window_is_not_refreshed_or_rejected(self):
        window = self.window(source="TEST_INPUT", expires=self.now + 5)
        handle = self.claim().handle
        self.now += 6
        item = self.load(handle)
        self.assertFalse(item.reply_credential.valid_at(self.now))
        self.assertEqual(item.reply_credential.expires_at,
                         self.query("SELECT expires_at FROM qq_official_reply_windows WHERE window_id=?", (window,))[0]["expires_at"])

    def test_input_route_or_admission_change_does_not_reconstruct_a_fresh_request(self):
        handle = self.claim().handle
        self.admitted = False
        with self.assertRaises(execution.ExecutionFenceLost):
            self.load(handle)

    def test_input_conflicting_index_invalidates_reply_even_for_at_turn(self):
        target = self.ledger.record_message(self.endpoint.scope, native_message_id="TEST_QUOTED",
            sender_native_user_id=self.actor.native_user_id, sender_display="test",
            body=MessageBody((TextNode(0, "TEST quoted context"),)), occurred_at=self.now - 10,
            actor=self.actor, source_endpoint=self.endpoint)
        with self.tx() as cursor:
            cursor.execute("UPDATE messages SET reply_to_native_message_id='TEST_QUOTED' WHERE canonical_message_id=?",
                           (self.message.canonical_message_id,))
            for key, native, canonical, reference in (
                ("TEST_SOURCE", "TEST_INPUT", self.message.canonical_message_id, "TEST_INDEX"),
                ("TEST_TARGET", "TEST_QUOTED", target.canonical_message_id, None),
            ):
                cursor.execute("""INSERT INTO qq_official_messages
                    (message_id,account_key,endpoint_key,native_message_id,canonical_message_id,direction,ref_msg_idx,created_at)
                    VALUES (?,?,?,?,?,'inbound',?,?)""",
                    (key, self.account.key, self.endpoint.key, native, canonical, reference, self.now))
            cursor.execute("""INSERT INTO qq_official_message_indexes
                (endpoint_key,account_key,index_kind,index_value,native_message_id,canonical_message_id,ambiguous,created_at,updated_at)
                VALUES (?,?,'msg_idx','TEST_INDEX','TEST_QUOTED',?,FALSE,?,?)""",
                (self.endpoint.key, self.account.key, target.canonical_message_id, self.now, self.now))
        handle = self.claim().handle
        self.assertEqual(self.load(handle).reply_to_native_message_id, "TEST_QUOTED")
        other = self.ledger.record_message(self.endpoint.scope, native_message_id="TEST_OTHER",
            sender_native_user_id=self.actor.native_user_id, sender_display="test",
            body=MessageBody((TextNode(0, "TEST other context"),)), occurred_at=self.now - 8,
            actor=self.actor, source_endpoint=self.endpoint)
        with self.tx() as cursor:
            cursor.execute("""INSERT INTO qq_official_messages
                (message_id,account_key,endpoint_key,native_message_id,canonical_message_id,direction,created_at)
                VALUES ('TEST_OTHER_MAPPING',?,?,'TEST_OTHER',?,'inbound',?)""",
                (self.account.key, self.endpoint.key, other.canonical_message_id, self.now))
            cursor.execute("""INSERT INTO qq_official_message_indexes
                (endpoint_key,account_key,index_kind,index_value,native_message_id,canonical_message_id,ambiguous,created_at,updated_at)
                VALUES (?,?,'ref_idx','TEST_INDEX','TEST_OTHER',?,FALSE,?,?)""",
                (self.endpoint.key, self.account.key, other.canonical_message_id, self.now, self.now))
        self.assertIsNone(self.load(handle).reply_to_native_message_id)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(TurnInputTests(name) for name in dir(TurnInputTests) if name.startswith("test_input_"))


if __name__ == "__main__":
    unittest.main()
