"""Group owner binding must not grant access to another group's history."""
import unittest
from tests import test_qq_official_binding as fixture
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode


class SimpleBindingTests(unittest.TestCase):
    setUp = fixture.OfficialBindingTests.setUp
    tx = fixture.OfficialBindingTests.tx
    guard = staticmethod(fixture.OfficialBindingTests.guard)
    seed_endpoint = fixture.OfficialBindingTests.seed_endpoint
    context = fixture.OfficialBindingTests.context
    issue = fixture.OfficialBindingTests.issue
    consume = fixture.OfficialBindingTests.consume
    configure = fixture.OfficialBindingTests.configure

    def test_owner_code_keeps_new_group_history_separate(self):
        ledger = MessageLedger(self.database)
        self.addCleanup(ledger.close)
        old_scope = ConversationScope('onebot-v11', 'group', '1234567')
        old = ledger.record_message(old_scope, native_message_id='TEST_OLD', sender_native_user_id='TEST_OLD_AUTHOR',
            sender_display='TEST', body=MessageBody((TextNode(0, 'TEST old history'),)), occurred_at=self.now-20)
        issued = self.issue()
        self.assertEqual(self.consume(issued.effect.code).status, 'confirmed')
        with self.tx() as cursor:
            endpoint = cursor.execute('SELECT binding_state,canonical_scope_key FROM qq_official_endpoints WHERE endpoint_key=?',
                                      (self.group.key,)).fetchone()
            self.assertEqual((endpoint['canonical_scope_key'], endpoint['binding_state']),
                             (self.group.scope.key, 'active'))
        self.assertIsNone(ledger.get_in_scope(self.group.scope, old.canonical_message_id))
        self.assertIsNotNone(ledger.get_in_scope(old_scope, old.canonical_message_id))

    def test_nonowner_cannot_issue_a_binding_code(self):
        self.configure()
        self.assertEqual(self.issue().status, 'denied')
