from tests.qq_official_public_history_fixture import PublicHistoryFixture
from src.plugins.ai_chat.adapters.qq_official.migration_history import HistoryUnavailable
from src.plugins.ai_chat.adapters.qq_official.migration_history_clear import clear_public_history,lock_history_revocation
from src.plugins.ai_chat.platform_types import EndpointKey
from src.plugins.ai_chat.conversation_scope import ConversationScope

class PublicHistoryClearTests(PublicHistoryFixture):
    def setUp(self):
        super().setUp()
        self.ledger.public_history_clear=self.clear

    def clear(self,cursor,scope):
        return clear_public_history(cursor,scope,lock_routes=lock_history_revocation)

    def empty_target(self):
        self.endpoint=EndpointKey(self.account,'group','TEST_EMPTY_TARGET')
        self.provision(self.endpoint)
        self.query('UPDATE draft_history_approvals SET target_endpoint=?',(self.endpoint.key,))
        self.query('UPDATE migration_routes SET target_endpoint_key=?,target_scope_key=?',(self.endpoint.key,self.endpoint.scope.key))

    def test_empty_target_and_same_second_repeated_clear_revoke_each_grant(self):
        self.empty_target()
        note=self.note()
        first=self.prepare(notes=(note.id,));self.activate(first)
        self.assertTrue(self.read(first).public_notes)
        self.assertEqual(self.ledger.hide_history(self.endpoint.scope),0)
        with self.assertRaises(HistoryUnavailable):self.read(first)
        second=self.prepare(notes=(note.id,));self.activate(second)
        self.assertTrue(self.read(second).public_notes)
        self.assertEqual(self.ledger.hide_history(self.endpoint.scope),0)
        with self.assertRaises(HistoryUnavailable):self.read(second)
        self.assertEqual(self.query("SELECT * FROM qq_migration_public_history_grants WHERE state='active'"),[])

    def test_clear_failure_rolls_back_grant_and_physical_visibility_together(self):
        preview=self.prepare();self.activate(preview)
        def failed(cursor,scope):
            self.clear(cursor,scope)
            raise RuntimeError('TEST rollback')
        self.ledger.public_history_clear=failed
        with self.assertRaises(RuntimeError):self.ledger.hide_history(self.endpoint.scope)
        self.assertEqual(len(self.read(preview).messages),2)
        self.assertEqual(len(self.ledger.recent_in_scope(self.endpoint.scope)),1)

    def test_clear_of_source_revokes_manual_notes_even_at_unchanged_clock(self):
        note=self.note()
        preview=self.prepare(notes=(note.id,));self.activate(preview)
        self.ledger.hide_history(self.source)
        with self.assertRaises(HistoryUnavailable):self.read(preview)
        self.assertEqual(len(self.ledger.recent_in_scope(self.endpoint.scope)),1)

    def test_unrelated_scope_clear_preserves_approved_source(self):
        preview=self.prepare();self.activate(preview)
        self.ledger.hide_history(ConversationScope('onebot-v11','group','888'))
        self.assertEqual(len(self.read(preview).messages),2)

    def test_false_host_lock_port_cannot_clear_or_silently_proceed(self):
        preview=self.prepare();self.activate(preview)
        self.ledger.public_history_clear=lambda cursor,scope:clear_public_history(cursor,scope,lock_routes=lambda *a,**kw:False)
        with self.assertRaises(PermissionError):self.ledger.hide_history(self.endpoint.scope)
        self.assertEqual(len(self.read(preview).messages),2)
        self.assertEqual(len(self.ledger.recent_in_scope(self.endpoint.scope)),1)
