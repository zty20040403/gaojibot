"""Real source/Coordinator/M3/FakePlatform. Root runs the isolated PG batch."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from tests import test_qq_official_subagents as fixtures
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.progress import ProgressContext, ProgressEmitter
from src.plugins.ai_chat.adapters.qq_official.sender import OfficialSender
from src.plugins.ai_chat.adapters.qq_official.subagent_commands import list_for_endpoint
from src.plugins.ai_chat.adapters.qq_official.subagent_runtime import (
    TaskToolSession, assert_current, freeze_and_enqueue, job_kind,
)
from src.plugins.ai_chat.message_ir import body_from_json
from src.qq_official.credentials import Credentials
from src.qq_official.http import RestClient
from src.qq_official.testing.fake_platform import FakePlatform
from src.qq_official.token import TokenManager


class SubagentParagraphDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.r = fixtures.RuntimeTests('test_301_second_real_subagent_and_worker_send_proactive_through_m3')
        self.r.setUp()
        self.addCleanup(self.r.doCleanups)
        await self.r.asyncSetUp()
        self.addAsyncCleanup(self.r.asyncTearDown)
        self.f, self.outbox = self.r.f, self.r.outbox
        self.f.window(source='TEST_INPUT')
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
            await self.r.calls(check)
        self.sender = OfficialSender(self.f.account, self.f.token, self.outbox, self.rest,
            self.f.ledger, database_calls=self.r.calls, ensure_current=ensure,
            wall_clock=lambda: self.f.now)
        self.addAsyncCleanup(self.sender.stop)

    async def run_answer(self, answer, *, progress=True):
        self.task = self.r.submission()
        self.job = self.f.jobs.claim_due('TEST_PARAGRAPHS', kinds=(job_kind(self.f.account),), now=self.f.now)[0]
        dispatcher = self.r.dispatcher()

        def current(cursor, now):
            binding, task = assert_current(cursor, token=self.f.token, task_id=self.task.task_id,
                now=now, admission=self.f.admission, job=self.job)
            if task['status'] in {'completed', 'partial', 'failed', 'cancelled'}:
                raise PermissionError('progress_task_unavailable')
            return ProgressContext(f'task:{self.task.task_id}', self.f.endpoint, self.f.actor,
                task['scope_key'], binding['execution_deadline_at'],
                binding['source_native_message_id'], binding['source_canonical_message_id'])
        emitter = ProgressEmitter(token=self.f.token, outbox=self.outbox,
            current=current, database_calls=self.r.calls)

        async def session(*_):
            return TaskToolSession(tools=(), execute_tool=None, progress=emitter if progress else None)
        dispatcher.execution_factory = session
        emitted = False

        async def model(*args, **kwargs):
            nonlocal emitted
            if progress and not emitted:
                emitted = True
                # The real Coordinator invokes progress before the model.
                rows = self.f.query("SELECT delivery_id FROM deliveries WHERE purpose='progress'")
                self.assertTrue(rows)
                self.assertEqual((await self.sender.send_delivery(rows[0]['delivery_id'])).status, 'committed')
                self.assertEqual((await emitter.say('第二条进度'))['status'], 'suppressed')
            return '{"status":"success","summary":"TEST analysis","artifacts":[]}'
        plan = {'steps': [{'id': 'analysis', 'agent': 'analyst', 'objective': '分析文字', 'depends_on': []}]}
        with patch('src.plugins.ai_chat.subagents.ask_deepseek_json', new=AsyncMock(return_value=plan)), \
             patch('src.plugins.ai_chat.subagents.ask_deepseek_with_tools', new=model), \
             patch('src.plugins.ai_chat.subagents.ask_deepseek', new=AsyncMock(return_value=answer)):
            result = await dispatcher.execute(self.job)
        self.assertEqual(self.r.store.get(self.task.task_id).status, 'completed')
        return result

    def final_parts(self):
        row = self.f.query("SELECT manifest_json FROM delivery_plans WHERE purpose='background_result'")[0]
        return json.loads(json.loads(row['manifest_json'])['rendered'])['parts']

    def final_ids(self):
        return [r['delivery_id'] for r in self.f.query(
            "SELECT delivery_id FROM deliveries WHERE purpose='background_result' ORDER BY part_index")]

    async def test_progress_plus_five_short_paragraphs_send_three_final_parts_and_preserve_one_slot(self):
        result = await self.run_answer('一\n\n二\n\n三\n\n四[split]五')
        self.assertIsNotNone(result['plan_id'])
        self.assertEqual([p['text_content'] for p in self.final_parts()],
            [f'task#{self.task.task_id}\n一', '二', '三\n\n四\n\n五'])
        ids = self.final_ids()
        self.assertEqual(len(ids), 3)
        for delivery in ids:
            self.assertEqual((await self.sender.send_delivery(delivery)).status, 'committed')
        self.assertEqual([r['payload']['msg_seq'] for r in self.fake.received], [1, 2, 3, 4])
        window = self.f.query('SELECT used_count,reserved_count,max_replies FROM qq_official_reply_windows')[0]
        self.assertEqual((window['used_count'], window['reserved_count'], window['max_replies']), (4, 0, 5))


    async def test_hard_overflow_freezes_whole_result_with_visible_reason_and_no_partial_plan(self):
        answer = '甲' * 8001
        result = await self.run_answer(answer, progress=False)
        self.assertEqual((result['plan_id'], result['delivery_status'], result['reason']),
            (None, 'not_sent', 'result_needs_attachment'))
        before = self.f.query('SELECT * FROM qq_official_subagent_dispatches')[0]
        self.assertEqual(body_from_json(before['result_body_json']).nodes[0].text,
            f'task#{self.task.task_id}\n{answer}')
        self.assertFalse(self.f.query('SELECT * FROM delivery_plans'))
        self.assertFalse(self.f.query('SELECT * FROM delivery_attempts'))
        self.assertFalse(self.fake.received)
        self.f.now += 1
        with self.f.tx() as cursor:
            self.assertIsNone(freeze_and_enqueue(cursor, token=self.f.token, task_id=self.task.task_id,
                now=self.f.now, outbox=self.outbox, admission=self.f.admission, job=self.job))
            views = list_for_endpoint(cursor, token=self.f.token, endpoint=self.f.endpoint, actor=self.f.actor,
                now=self.f.now, admission=self.f.admission)
        after = self.f.query('SELECT * FROM qq_official_subagent_dispatches')[0]
        self.assertEqual(dict(before), dict(after))
        self.assertIn('未发送：结果超过消息上限，完整结果已保留', views[0].summary)
