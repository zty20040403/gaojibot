"""Owner code binds the group and continues the original ledger."""
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
    apply = fixture.OfficialBindingTests.apply
    bootstrap = fixture.OfficialBindingTests.bootstrap

    def test_owner_private_code_links_old_history_and_grants_group_identity(self):
        self.bootstrap()
        ledger = MessageLedger(self.database)
        self.addCleanup(ledger.close)
        scope = ConversationScope('onebot-v11', 'group', '1234567')
        old = ledger.record_message(scope, native_message_id='TEST_OLD', sender_native_user_id='TEST_OLD_AUTHOR',
            sender_display='TEST', body=MessageBody((TextNode(0, 'TEST old history'),)), occurred_at=self.now-20)
        issued = self.apply('/bind 1234567')
        self.assertEqual(issued.status, 'sending')
        with self.tx() as cursor:
            self.reducer.mark_issued(cursor, account=self.account, challenge_id=issued.challenge_id,
                outcome='success', lease_token=self.token)
        bound = self.apply('/bind '+issued.effect.code, endpoint=self.group, actor=self.member)
        self.assertEqual(bound.status, 'confirmed')
        with self.tx() as cursor:
            endpoint = cursor.execute('SELECT binding_state,canonical_scope_key FROM qq_official_endpoints WHERE endpoint_key=?', (self.group.key,)).fetchone()
            self.assertEqual(endpoint['canonical_scope_key'], scope.key)
            self.assertEqual(endpoint['binding_state'], 'active')
            self.assertIsNotNone(self.authority.owner_for_actor(cursor, self.group, self.member))
        self.assertIsNotNone(ledger.get_in_scope(scope, old.canonical_message_id))
        duplicate = self.apply('/bind '+issued.effect.code, endpoint=self.group, actor=self.member)
        self.assertEqual(duplicate.status, 'invalid')

    def test_nonowner_cannot_issue_a_binding_code(self):
        self.assertEqual(self.apply('/bind create').status, 'denied')
