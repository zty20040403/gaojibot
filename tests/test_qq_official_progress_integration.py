"""Real M3/PG + loopback fake; supplied for the root's serial PG run.

This module is intentionally not part of the candidate's pure test invocation.
"""
import unittest
from unittest.mock import patch

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
        self.assertEqual(len(self.fake.received), 1)
        self.assertEqual(self.fake.received[0]['payload']['msg_seq'], 1)
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

    async def test_final_slot_and_unknown_proactive_do_not_create_plan_or_attempt(self):
        self.f.query('UPDATE qq_official_reply_windows SET used_count=4,next_seq=5')
        self.p.outbox.allow_proactive_probe = True
        result = await self.p.platform.progress.say('test progress')
        self.assertEqual(result['status'], 'suppressed')
        self.assertFalse(self.f.query('SELECT * FROM delivery_plans'))
        self.assertFalse(self.f.query('SELECT * FROM delivery_attempts'))
        self.assertFalse(self.fake.received)

    async def test_revocation_between_prepare_and_http_suppresses_progress(self):
        delivery = await self.enqueue()
        original = self.p.outbox.prepare
        def prepare(*args):
            reservation = original(*args)
            self.f.admitted = False
            return reservation
        with patch.object(self.p.outbox, 'prepare', side_effect=prepare):
            await self.sender.send_delivery(delivery)
        self.assertFalse(self.fake.received)
        self.assertEqual(self.f.query('SELECT state FROM delivery_attempts')[0]['state'], 'cancelled_unsent')
        self.assertEqual(self.f.query('SELECT status FROM deliveries')[0]['status'], 'cancelled')

    async def test_restart_cancels_unsent_and_same_text_never_recreates(self):
        delivery = await self.enqueue()
        self.p.outbox._progress.clear()
        await self.sender.recover()
        self.assertEqual((await self.sender.send_delivery(delivery)).status, 'cancelled')
        result = await self.p.platform.progress.say('PDF 已生成，正在检查页数。')
        self.assertEqual(result['reason'], 'already_reported')
        self.assertFalse(self.fake.received)
        self.assertFalse(self.f.query('SELECT * FROM delivery_attempts'))

    async def test_unknown_is_not_replayed_and_late_receipt_needs_no_live_authority(self):
        delivery = await self.enqueue()
        saved = []
        original = self.p.outbox.prepare
        def prepare(*args):
            result = original(*args)
            saved.append(result)
            return result
        self.fake.drop_next_response = True
        with patch.object(self.p.outbox, 'prepare', side_effect=prepare):
            self.assertEqual((await self.sender.send_delivery(delivery)).status, 'ambiguous')
        self.p.outbox._progress.clear()
        self.f.admitted = False
        self.assertEqual((await self.sender.send_delivery(delivery)).status, 'ambiguous')
        receipt = self.p.outbox.capture_receipt(saved[0], self.fake.received[0]['receipt'])
        result = self.p.outbox.finalize(self.f.token, receipt, ledger=self.f.ledger)
        self.assertEqual(result.status, 'committed')
        self.assertEqual(len(self.fake.received), 1)
        self.assertEqual(len(self.f.query('SELECT * FROM delivery_attempts')), 1)

    async def test_expired_progress_is_cancelled_before_a_later_window(self):
        delivery = await self.enqueue()
        self.f.now += 31
        self.f.window(source='TEST_LATER')
        self.p.outbox.allow_newer_windows = True
        self.assertEqual((await self.sender.send_delivery(delivery)).status, 'cancelled')
        self.assertFalse(self.fake.received)
        self.assertFalse(self.f.query('SELECT * FROM delivery_attempts'))

    async def test_one_progress_preserves_all_four_final_parts_and_blocks_later_progress(self):
        first = await self.enqueue()
        self.assertEqual((await self.sender.send_delivery(first)).status, 'committed')
        self.assertEqual((await self.p.platform.progress.say('TEST different later progress'))['status'], 'suppressed')
        self.assertEqual(len(self.f.query('SELECT * FROM delivery_attempts')), 1)
        from src.plugins.ai_chat.message_ir import MessageBody, TextNode
        from src.plugins.ai_chat.adapters.qq_official import execution
        paragraphs = (
            '第一段：文件已经生成，所有页数均已核对。',
            '第二段：内容检查已经完成，图表和正文相互一致。',
            '第三段：附件正在使用原始回复窗口交付。',
            '第四段：这条消息确认完整结果，不省略任何结论。',
        )
        final_text = '\n\n'.join(paragraphs)
        self.p.body = MessageBody((TextNode(0, final_text),))
        # This fixture began a real executing turn, unlike PlatformTests.freeze
        # (prepared/no-journal). Preserve the issued journal identity and finish
        # the actual journal in the same transaction as its captured result.
        with self.f.tx() as cursor:
            execution.capture_result(cursor, **self.f.args(self.p.handle), body=self.p.body,
                turn_id=self.turn_id, status='succeeded')
            self.f.journal.finish_turn(self.turn_id, status='succeeded', final_text=final_text,
                finished_at=self.f.now, transaction_cursor=cursor)
            self.assertEqual(execution.load_result(cursor, **self.f.args(self.p.handle)), self.p.body)
        journal_rows = self.f.query('SELECT final_text FROM agent_turns WHERE turn_id=?',
            (self.turn_id,))
        # The existing journal stores a whitespace-normalized preview; the
        # official execution above retains the exact multiline result body.
        self.assertEqual([dict(row) for row in journal_rows], [{'final_text': ' '.join(paragraphs)}])
        result = await self.p.platform.send(self.p.request())
        deliveries = result.receipt['delivery_ids']
        self.assertEqual(len(deliveries), 4)
        for delivery in deliveries:
            self.assertEqual((await self.sender.send_delivery(delivery)).status, 'committed')
        self.assertEqual(len(self.fake.received), 5)
        self.assertEqual([r['payload']['msg_seq'] for r in self.fake.received], [1, 2, 3, 4, 5])
        self.assertEqual([r['payload']['markdown']['content'] for r in self.fake.received[1:]], list(paragraphs))

    async def test_cancelled_execution_and_new_account_epoch_deny_before_http(self):
        delivery = await self.enqueue()
        self.f.query("UPDATE qq_official_turn_executions SET state='cancelled'")
        self.assertEqual((await self.sender.send_delivery(delivery)).status, 'cancelled')
        self.assertFalse(self.fake.received)
        self.assertFalse(self.f.query('SELECT * FROM delivery_attempts'))
        self.f.account_lease.close()
        new_token = self.f.lease().acquire()
        self.assertIsNotNone(new_token)
        result = await self.p.platform.progress.say('TEST stale account source')
        self.assertEqual(result['status'], 'suppressed')
        self.assertEqual(len(self.f.query('SELECT * FROM delivery_plans')), 1)
        self.assertFalse(self.fake.received)


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
            self.assertEqual((await self.sender.send_delivery(row['delivery_id'])).status, 'cancelled')
        self.assertEqual(len(self.fake.received), 1)
        self.assertEqual(len(self.f.query('SELECT * FROM delivery_attempts')), 1)
