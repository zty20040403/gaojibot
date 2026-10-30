"""Private owner code executes once even without a recent passive reply window."""
from src.bot_security.store import SecurityError
from src.plugins.ai_chat.adapters.qq_official.approval import OwnerApprovalStore
from tests.qq_official_approval_fixture import ApprovalLoopbackFixture, SECRET


class OwnerApprovalTests(ApprovalLoopbackFixture):
    def setUp(self):
        super().setUp()
        self.store = OwnerApprovalStore(self.database, SECRET, clock=lambda: self.now, transport=self.transport)
        self.addCleanup(self.store.close)

    async def test_one_owner_code_without_window_executes_once(self):
        self.query('DELETE FROM qq_official_reply_windows')
        request, code = self.propose()
        self.assertEqual(request['quorum'], 1)
        self.assertIsNotNone(code)
        await self.delivered(request, code)
        self.assertEqual(len(self.fake.received), 1)
        self.assertNotIn('msg_id', self.fake.received[0]['payload'])
        with self.assertRaises(SecurityError):
            self.store.confirm(request['approval_id'], None, None, code, recipient=self.recipient_b)
        confirmed = self.store.confirm(request['approval_id'], None, None, code, recipient=self.recipient)
        self.assertEqual(confirmed['status'], 'queued')
        calls = []
        async def execute(row):
            calls.append(row['approval_id'])
            return {'ok': True}
        self.mobile.executors['server_task'] = execute
        self.assertTrue(await self.mobile.run_once())
        self.assertFalse(await self.mobile.run_once())
        self.assertEqual(calls, [request['approval_id']])
