"""Frozen attribution from real principal rows, no guessed owner."""
from dataclasses import replace
import json
import unittest

from src.bot_storage import DatabaseError
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.model_usage import TokenUsage
from tests import test_model_billing as billing_fixture


class HumanBillingSourcesTests(unittest.TestCase):
    setUp = billing_fixture.ModelBillingTests.setUp
    row = billing_fixture.ModelBillingTests.row

    def test_person_snapshot_is_namespaced_and_survives_later_renaming(self):
        ledger = MessageLedger(self.store.database)
        self.addCleanup(ledger.close)

        def record(endpoint, actor, display, native):
            return ledger.record_message(
                endpoint.scope, native_message_id=native, sender_native_user_id=actor.native_user_id,
                sender_display=display, body=MessageBody((TextNode(0, 'TEST'),)),
                occurred_at=self.now, actor=actor, source_endpoint=endpoint,
            )

        original = record(self.endpoint, self.actor, 'TEST_ORIGINAL', 'TEST_SOURCE')
        foreign = replace(self.actor, identity_namespace=self.other.native_conversation_id)
        record(self.other, foreign, 'TEST_FOREIGN', 'TEST_FOREIGN_SOURCE')
        call = self.store.begin(self.source, self.profile)
        saved = json.loads(self.row(call)['source_metadata_json'])
        self.assertEqual(saved, dict(person=original.sender_principal_id, display='TEST_ORIGINAL', programs=[], commands=[]))
        record(self.endpoint, self.actor, 'TEST_RENAMED', 'TEST_LATER_SOURCE')
        self.store.finish(call, TokenUsage(10, 2, 0, 0))
        self.assertEqual(json.loads(self.row(call)['source_metadata_json']), saved)
        self.assertNotIn(self.actor.native_user_id, json.dumps(saved))
        with self.assertRaises(DatabaseError), self.store.transaction() as cursor:
            cursor.execute("UPDATE model_usage_calls SET source_metadata_json='{}' WHERE call_id=?", (call,))

    def test_missing_principal_is_explicit_and_pending_metadata_cannot_be_reassigned(self):
        call = self.store.begin(self.source, self.profile)
        saved = json.loads(self.row(call)['source_metadata_json'])
        self.assertEqual(saved, dict(person=None, display=None, programs=[], commands=[]))
        with self.assertRaises(DatabaseError), self.store.transaction() as cursor:
            cursor.execute("UPDATE model_usage_calls SET source_metadata_json='{}' WHERE call_id=?", (call,))
        self.assertIsNone(self.row(call)['finished_at'])
