"""Pi RPC event, request, and launch contract without a real VM."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.plugins.ai_chat.agent_loop import AgentTrace
from src.plugins.ai_chat.codex_engine import DEADLINE_NOTE
from src.plugins.ai_chat.pi_engine import PiEngine, PiRun, launch_script, pi_models_json


class Channel:
    def __init__(self):
        self.inbox = asyncio.Queue()
        self.requests = []
        self.session = None

    async def wait(self, _timeout):
        pass

    async def receive(self):
        message = await self.inbox.get()
        if isinstance(message, Exception):
            raise message
        return message

    async def send(self, message):
        self.requests.append(message)
        if message['type'] == 'extension_ui_response':
            return
        self.inbox.put_nowait({'type': 'response', 'id': message['id'], 'success': True,
                               'data': {'disposition': 'started'}})
        if message['type'] == 'prompt':
            self.inbox.put_nowait({'type': 'tool_execution_start', 'toolName': 'history'})
        if message['type'] == 'abort':
            self.inbox.put_nowait({'type': 'message_end', 'message': {'role': 'assistant',
                'content': [{'type': 'text', 'text': 'partial'}], 'usage': {}, 'stopReason': 'aborted'}})
            self.inbox.put_nowait({'type': 'agent_settled', 'aborted': True})


class PiCoreTests(unittest.IsolatedAsyncioTestCase):
    def test_launch_has_private_config_and_virtual_model(self):
        config = pi_models_json(18640, context_window=123456)['providers']['gaoji']
        self.assertEqual(config['apiKey'], '$GAOJI_JOB_TOKEN')
        self.assertEqual(config['models'][0]['id'], 'gaoji-background')
        self.assertEqual(config['models'][0]['contextWindow'], 123456)
        self.assertEqual(config['compat'], {'supportsLongCacheRetention': False, 'supportsMaxOutputTokens': False})
        script = launch_script(9, 'secret token', 'high', 18640, 'private prompt')
        for item in ('PI_CODING_AGENT_DIR=/run/gaoji/jobs/job-9', '--no-approve',
                     '--session-dir /work/.gaoji/jobs/job-9/pi', '/etc/gaoji/pi/sandbox_pi_tools.ts',
                     '/etc/gaoji/pi/sandbox_agent_bridge.py', 'HOME=/work', 'PI_OFFLINE=1'):
            self.assertIn(item, script)
        self.assertNotIn('private prompt', script)
        self.assertNotIn('--no-session', script)

    async def test_events_track_progress_usage_and_settled(self):
        channel, trace, progress = Channel(), AgentTrace(), AsyncMock()
        run = PiRun(channel, progress, trace)
        reader = asyncio.create_task(run.read())
        await run.request({'type': 'prompt', 'message': 'test'})
        channel.inbox.put_nowait({'type': 'message_end', 'message': {'role': 'assistant',
            'content': [{'type': 'text', 'text': 'result'}], 'stopReason': 'stop',
            'usage': {'input': 20, 'output': 7, 'cacheRead': 100, 'cacheWrite': 3, 'totalTokens': 130}}})
        channel.inbox.put_nowait({'type': 'agent_settled', 'aborted': False})
        await asyncio.wait_for(run.settled.wait(), 2)
        self.assertEqual(run.last_text, 'result')
        self.assertEqual((trace.input_tokens, trace.cached_tokens, trace.output_tokens), (123, 100, 7))
        progress.assert_awaited_once_with('history')
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)

    async def test_connection_loss_unblocks_pending_request(self):
        channel, trace = Channel(), AgentTrace()
        run = PiRun(channel, AsyncMock(), trace)
        async def send_without_response(message):
            channel.inbox.put_nowait(EOFError('closed'))
        channel.send = send_without_response
        reader = asyncio.create_task(run.read())
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            await asyncio.wait_for(run.request({'type': 'prompt'}), 2)
        self.assertTrue(run.settled.is_set())
        await reader

    async def test_deadline_aborts_and_revokes(self):
        channel = Channel()
        gateway = SimpleNamespace(start=AsyncMock(), open=lambda **_: ('token', channel), revoke=Mock(),
                                  port=18640, failure=lambda _: '')
        engine = PiEngine(gateway, SimpleNamespace(get=lambda: ('model', 'high')),
                          start_vm=AsyncMock(return_value='vm'), guest_exec=AsyncMock(), clock=lambda: 10)
        report = await engine({'job_id': 1, 'task': 'test', 'deadline_at': 10},
            SimpleNamespace(prompt='p', current=AsyncMock()), feedback=AsyncMock(return_value=[]),
            progress=AsyncMock(), trace=AgentTrace())
        self.assertEqual(report, DEADLINE_NOTE + '\npartial')
        self.assertIn('abort', [item['type'] for item in channel.requests])
        gateway.revoke.assert_called_once_with('token')

    async def test_unresponsive_abort_has_bounded_cleanup(self):
        channel = Channel()
        old_send = channel.send
        async def send(message):
            if message['type'] != 'abort':
                await old_send(message)
        channel.send = send
        gateway = SimpleNamespace(start=AsyncMock(), open=lambda **_: ('token', channel), revoke=Mock(),
                                  port=18640, failure=lambda _: '')
        engine = PiEngine(gateway, SimpleNamespace(get=lambda: ('model', 'high')),
                          start_vm=AsyncMock(return_value='vm'), guest_exec=AsyncMock(), clock=lambda: 10)
        with patch('src.plugins.ai_chat.pi_engine.ABORT_TIMEOUT', 0.02), patch('src.plugins.ai_chat.pi_engine.STOP_GRACE', 0.02):
            report = await asyncio.wait_for(engine({'job_id': 2, 'task': 'test', 'deadline_at': 10},
                SimpleNamespace(prompt='p', current=AsyncMock()), feedback=AsyncMock(return_value=[]),
                progress=AsyncMock(), trace=AgentTrace()), 1)
        self.assertTrue(report.startswith(DEADLINE_NOTE))
        gateway.revoke.assert_called_once_with('token')
