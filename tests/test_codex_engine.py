"""The Codex engine against a scripted app-server: tools, steering, deadline and failure."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from src.plugins.ai_chat.agent_loop import AgentTrace
from src.plugins.ai_chat.codex_engine import DEADLINE_NOTE, CodexEngine, launch_script


class FakeAppServer:
    """Answers the client's requests the way codex app-server 0.153 does."""

    def __init__(self, *, finish='completed', steer_error=None, complete_on_rejection=False):
        self.finish, self.outbox = finish, asyncio.Queue()
        self.steer_error, self.complete_on_rejection = steer_error, complete_on_rejection
        self.requests, self.tool_replies = [], []

    async def wait(self, timeout):
        pass

    async def receive(self):
        return await self.outbox.get()

    def push(self, message):
        self.outbox.put_nowait(message)

    def complete(self, text, status):
        self.push({'method': 'item/completed', 'params': {'item': {'type': 'agentMessage', 'text': text}}})
        self.push({'method': 'thread/tokenUsage/updated', 'params': {'tokenUsage': {'total': {
            'inputTokens': 120, 'cachedInputTokens': 100, 'outputTokens': 7, 'totalTokens': 127}}}})
        turn = {'id': 'TURN', 'status': status, 'error': {'message': 'TEST upstream down'} if status == 'failed' else None}
        self.push({'method': 'turn/completed', 'params': {'turn': turn}})

    async def send(self, message):
        method = message.get('method')
        if method is None:
            self.tool_replies.append(message)
            return
        self.requests.append(message)
        if method == 'turn/steer' and self.steer_error is not None:
            if self.complete_on_rejection:
                self.complete('TEST original task only', 'completed')
            self.push({'id': message['id'], 'error': {'code': -32000, 'message': self.steer_error}})
            return
        reply = {'initialize': {}, 'thread/start': {'thread': {'id': 'THREAD'}},
                 'turn/start': {'turn': {'id': 'TURN'}}, 'turn/steer': {'turnId': 'TURN'},
                 'turn/interrupt': {}}.get(method)
        if 'id' in message:
            self.push({'id': message['id'], 'result': reply})
        if method == 'turn/start':
            self.push({'method': 'turn/started', 'params': {'turn': {'id': 'TURN'}}})
            self.push({'method': 'item/started', 'params': {'item': {'type': 'dynamicToolCall', 'tool': 'history'}}})
            self.push({'id': 900, 'method': 'item/tool/call', 'params': {
                'tool': 'history', 'arguments': {'query': 'TEST'}, 'callId': 'C', 'threadId': 'THREAD', 'turnId': 'TURN'}})
        elif method == 'turn/steer':
            self.complete('TEST final report', self.finish)
        elif method == 'turn/interrupt':
            self.complete('TEST partial', 'interrupted')

    def sent(self, method):
        return [item['params'] for item in self.requests if item.get('method') == method]


class CodexEngineTests(unittest.IsolatedAsyncioTestCase):
    def engine(self, server, *, now=0):
        self.gateway = SimpleNamespace(start=AsyncMock(), open=lambda: ('TEST_TOKEN', server), revoke=Mock(), port=18640,
                                       failure=lambda token: '')
        self.exec = AsyncMock()
        settings = SimpleNamespace(get=lambda: ('TEST_MODEL', 'high'))
        return CodexEngine(self.gateway, settings, start_vm=AsyncMock(return_value='s123abc'),
                           guest_exec=self.exec, clock=lambda: now)

    def session(self):
        tool = {'type': 'function', 'function': {'name': 'history', 'description': 'TEST', 'parameters': {'type': 'object'}}}
        return SimpleNamespace(prompt='TEST prompt', tools=(tool,), execute_tool=AsyncMock(return_value='TEST lines'))

    async def test_tool_steer_and_report(self):
        server, session, trace, progress = FakeAppServer(), self.session(), AgentTrace(), AsyncMock()
        feedback = AsyncMock(side_effect=[['TEST also memory'], [], [], []])
        report = await self.engine(server)({'job_id': 7, 'task': 'TEST task', 'deadline_at': 7200}, session,
                                           feedback=feedback, progress=progress, trace=trace)
        self.assertEqual(report, 'TEST final report')
        session.execute_tool.assert_awaited_once_with('history', {'query': 'TEST'})
        self.assertEqual(server.tool_replies[0]['result']['contentItems'][0]['text'], 'TEST lines')
        self.assertEqual(server.sent('turn/steer')[0]['input'][0]['text'], 'TEST also memory')
        start = server.sent('thread/start')[0]
        self.assertEqual(start['dynamicTools'][0]['name'], 'history')
        self.assertEqual(start['developerInstructions'], 'TEST prompt')
        self.assertTrue(server.sent('initialize')[0]['capabilities']['experimentalApi'])
        progress.assert_awaited_with('history')
        self.assertEqual((trace.input_tokens, trace.cached_tokens, trace.output_tokens), (120, 100, 7))
        script = self.exec.await_args.args[1]
        self.assertIn('GAOJI_JOB_TOKEN=TEST_TOKEN', script)
        self.assertIn('job-7', script)
        self.gateway.revoke.assert_called_once_with('TEST_TOKEN')

    async def test_rejected_steer_fails_instead_of_returning_the_original_report(self):
        for completed in (False, True):
            with self.subTest(original_turn_completed=completed):
                server = FakeAppServer(steer_error='TEST turn no longer accepts steering',
                                       complete_on_rejection=completed)
                feedback = AsyncMock(return_value=['TEST additional model', 'TEST later requirement'])
                with self.assertRaisesRegex(RuntimeError, '追加要求未被 Codex 接受') as caught:
                    await asyncio.wait_for(self.engine(server)(
                        {'job_id': 11, 'task': 'TEST task', 'deadline_at': 7200}, self.session(),
                        feedback=feedback, progress=AsyncMock(), trace=AgentTrace()), 1)
                self.assertIn('TEST turn no longer accepts steering', str(caught.exception))
                self.assertIn('请带上补充要求重新发起', str(caught.exception))
                self.assertEqual(len(server.sent('turn/steer')), 1)
                feedback.assert_awaited_once()
                self.gateway.revoke.assert_called_once_with('TEST_TOKEN')

    async def test_deadline_interrupts_and_hands_over_the_partial_report(self):
        server = FakeAppServer()
        report = await self.engine(server, now=7200)({'job_id': 8, 'task': 'TEST', 'deadline_at': 7200},
            self.session(), feedback=AsyncMock(return_value=[]), progress=AsyncMock(), trace=AgentTrace())
        self.assertEqual(report, DEADLINE_NOTE + '\nTEST partial')
        self.assertEqual(len(server.sent('turn/interrupt')), 1)

    async def test_failed_turn_raises_the_reason(self):
        server = FakeAppServer(finish='failed')
        with self.assertRaisesRegex(RuntimeError, 'TEST upstream down'):
            await self.engine(server)({'job_id': 9, 'task': 'TEST', 'deadline_at': 7200}, self.session(),
                feedback=AsyncMock(side_effect=[['TEST go'], [], []]), progress=AsyncMock(), trace=AgentTrace())
        self.gateway.revoke.assert_called_once()

    async def test_relay_failure_is_reported_instead_of_codex_generic_error(self):
        server = FakeAppServer(finish='failed')
        engine = self.engine(server)
        self.gateway.failure = lambda token: 'TEST 模型端点返回 404'
        with self.assertRaisesRegex(RuntimeError, 'TEST 模型端点返回 404'):
            await engine({'job_id': 10, 'task': 'TEST', 'deadline_at': 7200}, self.session(),
                feedback=AsyncMock(side_effect=[['TEST go'], [], []]), progress=AsyncMock(), trace=AgentTrace())

    def test_upstream_profile_reads_the_real_catalog(self):
        from src.plugins.ai_chat.codex_engine import upstream_profile
        from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelProfile
        def profile(name, provider):
            return ModelProfile(name=name, provider=provider, protocol='openai-chat', model=name,
                                base_url='http://127.0.0.1/v1', api_key_required=False)
        catalog = ModelCatalog({'TEST_DS': profile('TEST_DS', 'deepseek'), 'TEST_CP': profile('TEST_CP', 'cliproxy')},
                               default_profile='TEST_DS')
        self.assertEqual(upstream_profile(catalog, SimpleNamespace(background_upstream_profile='')).name, 'test_cp')
        self.assertEqual(upstream_profile(catalog, SimpleNamespace(background_upstream_profile='TEST_DS')).name, 'test_ds')
        with self.assertRaises(RuntimeError):
            upstream_profile(catalog, SimpleNamespace(background_upstream_profile='TEST_NONE'))

    def test_launch_script_pins_codex_and_keeps_the_key_out(self):
        script = launch_script(3, 'TEST_TOKEN', 'TEST_MODEL', 18640)
        self.assertIn('codex-cli 0.153.4', script)
        self.assertIn('sha256sum -c', script)
        self.assertIn('10.0.2.2 18641', script)
        self.assertNotIn('CLIPROXY', script)


if __name__ == '__main__':
    unittest.main()
