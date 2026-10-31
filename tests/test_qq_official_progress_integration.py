"""Real M3/PG + loopback fake; supplied for the root's serial PG run.

This module is intentionally not part of the candidate's pure test invocation.
"""
import unittest

from tests import test_qq_official_turn_platform as platform_fixture
from src.plugins.ai_chat.adapters.qq_official.sender import OfficialSender
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.qq_official.credentials import Credentials
from src.qq_official.http import RestClient
from src.qq_official.token import TokenManager
from src.qq_official.testing.fake_platform import FakePlatform


class OfficialProgressIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.p = platform_fixture.PlatformTests('test_ready_separates_credentials_auth_identity_binding_and_window')
        self.p.setUp()
        self.addCleanup(self.p.doCleanups)
        self.f = self.p.f
        self.turn_id = self.f.begin(self.p.handle)

    async def asyncSetUp(self):
        await self.p.asyncSetUp()
        self.fake = FakePlatform(clock=lambda: self.f.now)
        self.fake.app_id = self.f.account.app_id
        self.fake.proactive_allowed[('group',self.f.endpoint.native_conversation_id)]=True
        self.f.query("UPDATE qq_official_endpoints SET proactive_state='unknown'")
        await self.fake.start()
        self.fake.add_window('group', self.f.endpoint.native_conversation_id, 'TEST_INPUT')
        self.tokens = TokenManager(Credentials(self.f.account.app_id, self.fake.app_secret),
            self.fake.base_url, allow_test_server=True)
        self.rest = RestClient(self.tokens, allow_test_server=True)
        async def ensure():
            def check():
                with self.f.tx() as cursor:
                    assert_epoch(cursor, self.f.token)
            await self.p.calls(check)
        self.sender = OfficialSender(self.f.account, self.f.token, self.p.outbox,
            self.rest, self.f.ledger, database_calls=self.p.calls, ensure_current=ensure,
            journal=self.f.journal, wall_clock=lambda: self.f.now)

    async def asyncTearDown(self):
        await self.sender.stop()
        await self.rest.aclose()
        await self.tokens.aclose()
        await self.fake.close()
        await self.p.asyncTearDown()

    async def enqueue(self, text='PDF 已生成，正在检查页数。'):
        result = await self.p.platform.progress.say(text)
        self.assertEqual(result['status'], 'enqueued')
        return self.f.query("SELECT delivery_id FROM deliveries WHERE purpose='progress' ORDER BY delivery_id DESC")[0]['delivery_id']

    async def test_window_progress_uses_real_m3_and_does_not_complete_turn(self):
        delivery = await self.enqueue()
        self.assertEqual((await self.sender.send_delivery(delivery)).status, 'committed')
        self.assertGreaterEqual(len(self.fake.received), 1)
        self.assertNotIn('msg_seq', self.fake.received[0]['payload'])
        self.assertEqual(self.f.query('SELECT state FROM qq_official_turn_executions')[0]['state'], 'executing')
        self.assertEqual(self.f.query('SELECT COUNT(*) AS n FROM delivery_receipts')[0]['n'], 1)

    async def test_verified_proactive_without_window_uses_real_m3(self):
        self.f.query("UPDATE qq_official_reply_windows SET state='expired'")
        self.f.query("UPDATE qq_official_endpoints SET proactive_state='allowed'")
        self.fake.proactive_allowed[('group', self.f.endpoint.native_conversation_id)] = True
        delivery = await self.enqueue()
        self.assertEqual((await self.sender.send_delivery(delivery)).status, 'committed')
        self.assertEqual(self.f.query('SELECT send_mode FROM delivery_attempts')[0]['send_mode'], 'proactive')
        self.assertNotIn('msg_id', self.fake.received[0]['payload'])
