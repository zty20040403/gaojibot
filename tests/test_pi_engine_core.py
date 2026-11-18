"""Pi --print engine launch, usage, deadline, and bounded stop contract without a real container."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.plugins.ai_chat.agent_loop import AgentTrace
from src.plugins.ai_chat.pi_engine import DEADLINE_NOTE, PiEngine, launch_script, pi_models_json
from tests.test_pi_engine import FakePiGuest


class PiCoreTests(unittest.IsolatedAsyncioTestCase):
    def test_launch_has_private_config_and_virtual_model(self):
        config = pi_models_json(18640, context_window=123456)['providers']['gaoji']
        self.assertEqual(config['apiKey'], '$GAOJI_JOB_TOKEN')
        self.assertEqual(config['models'][0]['id'], 'gaoji-background')
        self.assertEqual(config['models'][0]['contextWindow'], 123456)
        self.assertEqual(config['models'][0]['thinkingLevelMap'], {'xhigh': 'xhigh', 'max': 'max'})
        self.assertEqual(config['compat'], {'supportsLongCacheRetention': False, 'supportsMaxOutputTokens': False})
        script = launch_script(9, 'secret token', 'high', 18640, 'private prompt', task='do research')
        for item in (
            'PI_CODING_AGENT_DIR=/run/gaoji/jobs/job-9',
            '--print',
            '--no-approve',
            '--session-dir /work/.gaoji/jobs',
            '--session-id gaoji-job-9',
            '/etc/gaoji/pi/sandbox_pi_tools.ts',
            '/etc/gaoji/pi/sandbox_pi_agent.ts',
            'HOME=/work',
            'PI_OFFLINE=1',
            'KillSignal=SIGUSR2',
        ):
            self.assertIn(item, script)
        self.assertNotIn('sandbox_agent_bridge.py', script)
        self.assertNotIn('--no-context-files', script)
        self.assertNotIn('private prompt', script)
        self.assertNotIn('--no-session', script)

    async def test_inspect_tracks_progress_usage_and_report(self):
        guest = FakePiGuest(final_text='result')
        gateway = SimpleNamespace(
            start=AsyncMock(),
            open=lambda **_: ('token', guest),
            revoke=Mock(),
            port=18640,
            failure=lambda _: '',
        )
        progress = AsyncMock()
        trace = AgentTrace()
        engine = PiEngine(
            gateway,
            SimpleNamespace(get=lambda: ('model', 'high')),
            start_vm=AsyncMock(return_value='vm'),
            guest_exec=AsyncMock(side_effect=guest.exec),
            clock=lambda: 0,
            poll_interval=0.01,
        )
        report = await engine(
            {'job_id': 1, 'task': 'test', 'deadline_at': 100},
            SimpleNamespace(prompt='p', current=AsyncMock()),
            feedback=AsyncMock(return_value=[]),
            progress=progress,
            trace=trace,
        )
        self.assertEqual(report, 'result')
        self.assertEqual((trace.input_tokens, trace.cached_tokens, trace.output_tokens), (120, 100, 7))
        progress.assert_awaited_once_with('history')
        gateway.revoke.assert_called_once_with('token')

    async def test_deadline_stops_unit_and_revokes(self):
        guest = FakePiGuest(partial_text='partial', wait_for_steer=True)
        gateway = SimpleNamespace(
            start=AsyncMock(),
            open=lambda **_: ('token', guest),
            revoke=Mock(),
            port=18640,
            failure=lambda _: '',
        )
        engine = PiEngine(
            gateway,
            SimpleNamespace(get=lambda: ('model', 'high')),
            start_vm=AsyncMock(return_value='vm'),
            guest_exec=AsyncMock(side_effect=guest.exec),
            clock=lambda: 10 if guest.launched.is_set() else 9,
            poll_interval=0.01,
        )
        report = await engine(
            {'job_id': 1, 'task': 'test', 'deadline_at': 10},
            SimpleNamespace(prompt='p', current=AsyncMock()),
            feedback=AsyncMock(return_value=[]),
            progress=AsyncMock(),
            trace=AgentTrace(),
        )
        self.assertEqual(report, DEADLINE_NOTE + '\npartial')
        self.assertTrue(guest.stop_called.is_set())
        gateway.revoke.assert_called_once_with('token')

    async def test_unresponsive_stop_has_bounded_cleanup(self):
        guest = FakePiGuest(wait_for_steer=True, ignore_stop=True)

        async def slow_exec(vm, script):
            if script.startswith('if command -v systemctl'):
                guest.stop_called.set()
                await asyncio.Future()
            return await guest.exec(vm, script)

        gateway = SimpleNamespace(
            start=AsyncMock(),
            open=lambda **_: ('token', guest),
            revoke=Mock(),
            port=18640,
            failure=lambda _: '',
        )
        engine = PiEngine(
            gateway,
            SimpleNamespace(get=lambda: ('model', 'high')),
            start_vm=AsyncMock(return_value='vm'),
            guest_exec=AsyncMock(side_effect=slow_exec),
            clock=lambda: 10 if guest.launched.is_set() else 9.9,
            poll_interval=0.01,
        )
        with patch('src.plugins.ai_chat.pi_engine.STOP_TIMEOUT', 0.03):
            report = await asyncio.wait_for(
                engine(
                    {'job_id': 2, 'task': 'test', 'deadline_at': 10},
                    SimpleNamespace(prompt='p', current=AsyncMock()),
                    feedback=AsyncMock(return_value=[]),
                    progress=AsyncMock(),
                    trace=AgentTrace(),
                ),
                1,
            )
        self.assertTrue(report.startswith(DEADLINE_NOTE))
        self.assertTrue(guest.stop_called.is_set())
        gateway.revoke.assert_called_once_with('token')
