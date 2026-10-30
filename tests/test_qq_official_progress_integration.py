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









class OfficialFileSayIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Actual Coordinator + file registry/guest capture + one real M3 send."""
    def setUp(self):
        from tests import test_qq_official_file_subagent_composition as fixtures
        self.f = fixtures.FileSubAgentCompositionTests(
            'test_document_pdf_real_coordinator_registry_capture_and_durable_registration')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    async def asyncSetUp(self):
        await self.f.asyncSetUp()
        self.addAsyncCleanup(self.f.asyncTearDown)
        self.f.configure_files()
        self.f.submit_task()
        self.f.window(source='TEST_INPUT')
        from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
        with self.f.tx() as cursor:
            observe_ready(cursor, self.f.token, {'op': 0, 't': 'READY', 'd': {
                'session_id': 'TEST_SESSION', 'user': {'id': 'TEST_BOT', 'bot': True}}}, self.f.now)
        self.fake = FakePlatform(clock=lambda: self.f.now)
        self.fake.app_id = self.f.account.app_id
        self.fake.proactive_allowed[('group',self.f.endpoint.native_conversation_id)]=True
        self.f.query("UPDATE qq_official_endpoints SET proactive_state='unknown'")
        await self.fake.start()
        self.addAsyncCleanup(self.fake.close)
        self.fake.add_window('group', self.f.endpoint.native_conversation_id, 'TEST_INPUT')
        self.tokens = TokenManager(Credentials(self.f.account.app_id, self.fake.app_secret),
            self.fake.base_url, allow_test_server=True)
        self.rest = RestClient(self.tokens, allow_test_server=True)
        self.addAsyncCleanup(self.tokens.aclose)
        self.addAsyncCleanup(self.rest.aclose)
        async def ensure():
            def check():
                with self.f.tx() as cursor:
                    assert_epoch(cursor, self.f.token)
            await self.f.calls(check)
        self.sender = OfficialSender(self.f.account, self.f.token, self.f.outbox,
            self.rest, self.f.ledger, database_calls=self.f.calls, ensure_current=ensure,
            wall_clock=lambda: self.f.now)
        self.addAsyncCleanup(self.sender.stop)
        self.f.runtime.sender = self.sender

    async def test_actual_file_agent_say_and_coordinator_callback_reach_m3_separately_from_final(self):
        import json
        observed = []
        async def model(prompt, messages, tools, execute_tool, **kwargs):
            self.assertIn('say', {tool['function']['name'] for tool in tools})
            if '独立验收人' not in prompt:
                # Coordinator emitted its real progress callback before the model.
                self.assertTrue(self.f.query("SELECT plan_id FROM delivery_plans WHERE purpose='progress'"))
                raw = await execute_tool('say', {'text': 'TEST live file progress'})
                self.assertEqual(json.loads(raw)['status'], 'enqueued')
                delivery = self.f.query("SELECT delivery_id FROM deliveries WHERE purpose='progress' AND body_json LIKE ?",
                    ('%TEST live file progress%',))[0]['delivery_id']
                self.assertEqual((await self.sender.send_delivery(delivery)).status, 'committed')
                self.assertEqual(self.f.tasks.get(self.f.task.task_id).status, 'running')
                observed.append(delivery)
            return await self.f.file_model(prompt, messages, tools, execute_tool, **kwargs)
        await self.f.run_model(model)
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.f.tasks.get(self.f.task.task_id).status, 'completed')
        self.assertEqual(len(self.f.query('SELECT * FROM qq_official_file_artifacts')), 1)
        self.assertEqual(len(self.f.query("SELECT * FROM delivery_plans WHERE purpose='background_result'")), 1)
        for row in self.f.query("SELECT delivery_id FROM deliveries WHERE purpose='progress' AND status='pending'"):
            self.assertEqual((await self.sender.send_delivery(row['delivery_id'])).status, 'committed')
        self.assertGreaterEqual(len(self.fake.received), 1)
        self.assertEqual(len(self.f.query('SELECT * FROM delivery_attempts')), len(self.fake.received))
