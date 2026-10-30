"""Whoami goes through the actual gateway/sender, with identity hidden from context."""
import unittest
from tests import test_qq_official_file_composition as fixture


class WhoamiTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.FileCompositionTests.setUp
    asyncSetUp = fixture.FileCompositionTests.asyncSetUp
    runtime = fixture.FileCompositionTests.runtime
    query = fixture.FileCompositionTests.query
    wait_db = fixture.FileCompositionTests.wait_db
    application = fixture.FileCompositionTests.application
    emit = fixture.FileCompositionTests.emit
    config = fixture.FileCompositionTests.config
    open = fixture.FileCompositionTests.open

    async def test_private_and_unbound_group_reply_without_model_or_identity_in_history(self):
        runtime = await self.open(self.application())
        try:
            for group in (False, True):
                self.fake.proactive_allowed[('group', 'TEST_GROUP')] = True
                native = 'TEST_WHOAMI_' + str(group)
                await self.emit(self.fake, native=native, content='/whoami', group=group)
                rows = await self.wait_db("SELECT * FROM deliveries WHERE reply_to_native_message_id=? AND status='committed'", (native,))
                expected = 'TEST_MEMBER' if group else 'TEST_RUNTIME_OWNER'
                self.assertIn(expected, rows[0]['body_json'])
                sent = self.query("SELECT * FROM messages WHERE direction='outbound' ORDER BY canonical_message_id DESC LIMIT 1")[0]
                self.assertNotIn(expected, sent['body_json'])
                self.assertIn('身份查询已回复', sent['rendered_text'])
            self.assertEqual(self.query('SELECT * FROM qq_official_turn_executions'), [])
        finally:
            await runtime.stop()
