"""The Pi engine against a scripted RPC server and a real agent bridge tunnel."""
from __future__ import annotations

import asyncio
import logging
import inspect
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from src.plugins.ai_chat.agent_loop import AgentTrace
from src.plugins.ai_chat.job_gateway import JobGateway
from src.plugins.ai_chat.pi_engine import (
    DEADLINE_NOTE,
    BackgroundModelSettings,
    PiEngine,
    launch_script,
    pi_models_json,
    upstream_profile,
)
from tests.test_job_gateway import free_port
from tests.test_llm_gateway import profile


class FakePiRpcServer:
    def __init__(self, *, steer_error: str | None = None, fail_error: str | None = None) -> None:
        self.steer_error = steer_error
        self.fail_error = fail_error
        self.outbox: asyncio.Queue[dict] = asyncio.Queue()
        self.requests: list[dict] = []
        self.ui_responses: list[dict] = []
        self.session = None

    async def wait(self, timeout: float) -> None:
        return None

    async def receive(self) -> dict:
        return await self.outbox.get()

    def push(self, record: dict) -> None:
        self.outbox.put_nowait(record)

    def _finish(self, text: str, *, stop_reason: str = 'stop', aborted: bool = False) -> None:
        msg: dict = {
            'role': 'assistant',
            'content': [{'type': 'text', 'text': text}],
            'usage': {'input': 20, 'output': 7, 'cacheRead': 100, 'cacheWrite': 0, 'totalTokens': 127},
            'stopReason': stop_reason,
        }
        if self.fail_error and stop_reason == 'error':
            msg['errorMessage'] = self.fail_error
        self.push({'type': 'message_end', 'message': msg})
        self.push({'type': 'agent_settled', 'aborted': aborted})

    async def send(self, message: dict) -> None:
        if message.get('type') == 'extension_ui_response':
            self.ui_responses.append(message)
            return
        self.requests.append(message)
        cmd_id = message.get('id')
        cmd_type = message.get('type')
        if cmd_type == 'steer' and self.steer_error is not None:
            self.push({'id': cmd_id, 'type': 'response', 'command': 'steer', 'success': False, 'error': self.steer_error})
            return
        self.push({
            'id': cmd_id,
            'type': 'response',
            'command': cmd_type,
            'success': True,
            'data': {'disposition': 'started'} if cmd_type == 'prompt' else None,
        })
        if cmd_type == 'prompt':
            self.push({'type': 'tool_execution_start', 'toolCallId': 'c1', 'toolName': 'history', 'args': {'query': 'TEST'}})
            if self.fail_error:
                self._finish('', stop_reason='error')
        elif cmd_type == 'steer':
            self._finish('TEST final pi report')
        elif cmd_type == 'abort':
            self._finish('TEST partial pi report', stop_reason='aborted', aborted=True)


class PiEngineTests(unittest.IsolatedAsyncioTestCase):
    def _engine(self, server: FakePiRpcServer, *, now: int = 0) -> PiEngine:
        self.gateway = SimpleNamespace(
            start=AsyncMock(),
            open=lambda **kwargs: ('TEST_TOKEN', server),
            revoke=Mock(),
            port=18640,
            failure=lambda token: '',
        )
        self.exec = AsyncMock()
        settings = SimpleNamespace(get=lambda: ('TEST_PI_MODEL', 'high'))
        return PiEngine(
            self.gateway,
            settings,
            start_vm=AsyncMock(return_value='s123abc'),
            guest_exec=self.exec,
            clock=lambda: now,
        )

    def _session(self):
        tool = {'type': 'function', 'function': {'name': 'history', 'description': 'TEST', 'parameters': {'type': 'object'}}}
        return SimpleNamespace(prompt='TEST pi prompt', tools=(tool,), current=AsyncMock(), execute_tool=AsyncMock(return_value='ok'))

    def test_models_json_and_launch_script(self) -> None:
        self.assertIn('context_window', inspect.signature(pi_models_json).parameters)
        self.assertIn('context_window', inspect.signature(launch_script).parameters)
        cfg = pi_models_json(18640, context_window=123456)
        provider = cfg['providers']['gaoji']
        self.assertEqual(provider['baseUrl'], 'http://10.0.2.2:18640/v1')
        self.assertEqual(provider['api'], 'openai-responses')
        self.assertEqual(provider['apiKey'], '$GAOJI_JOB_TOKEN')
        self.assertTrue(provider['authHeader'])
        self.assertEqual(provider['models'][0]['id'], 'gaoji-background')
        self.assertEqual(provider['models'][0]['contextWindow'], 123456)
        self.assertEqual(provider['compat'], {'supportsLongCacheRetention': False, 'supportsMaxOutputTokens': False})

        script = launch_script(9, 'TOK', 'high', 18640, 'hello prompt', context_window=123456)
        self.assertIn('PI_CODING_AGENT_DIR=/run/gaoji/jobs/job-9', script)
        self.assertIn('GAOJI_HOST_URL=http://10.0.2.2:18640', script)
        self.assertIn('/etc/gaoji/pi/sandbox_pi_tools.ts', script)
        self.assertIn('/etc/gaoji/pi/sandbox_agent_bridge.py', script)
        self.assertIn('--thinking high', script)
        self.assertIn('--append-system-prompt /run/gaoji/jobs/job-9/job-system.md', script)
        self.assertIn('--session-dir /work/.gaoji/jobs/job-9/pi', script)
        self.assertIn('--no-approve', script)
        self.assertIn('PI_OFFLINE=1', script)
        self.assertIn('PI_SKIP_VERSION_CHECK=1', script)
        self.assertIn('PI_TELEMETRY=0', script)
        self.assertIn('HOME=/work', script)
        self.assertIn('systemd-run --unit=gaoji-job-9 --collect', script)
        self.assertNotIn('--no-session', script)
        self.assertNotIn('hello prompt', script)

    async def test_channel_wait_timeout_reports_startup_failure(self) -> None:
        class NeverConnects(FakePiRpcServer):
            async def wait(self, timeout: float) -> None:
                raise TimeoutError('bridge never connected')

        with self.assertRaisesRegex(RuntimeError, '后台进程没能启动'):
            await self._engine(NeverConnects())(
                {'job_id': 21, 'task': 'TEST', 'deadline_at': 7200},
                self._session(),
                feedback=AsyncMock(return_value=[]),
                progress=AsyncMock(),
                trace=AgentTrace(),
            )
        self.gateway.revoke.assert_called_once_with('TEST_TOKEN')

    async def test_steer_progress_usage_and_final_report(self) -> None:
        server = FakePiRpcServer()
        session = self._session()
        trace = AgentTrace()
        progress = AsyncMock()
        feedback = AsyncMock(side_effect=[['TEST extra detail'], [], []])
        report = await self._engine(server)(
            {'job_id': 7, 'task': 'TEST task', 'deadline_at': 7200},
            session,
            feedback=feedback,
            progress=progress,
            trace=trace,
        )
        self.assertEqual(report, 'TEST final pi report')
        self.assertEqual(trace.provider, 'pi')
        self.assertEqual(trace.model, 'TEST_PI_MODEL')
        self.assertEqual((trace.input_tokens, trace.cached_tokens, trace.output_tokens, trace.total_tokens), (120, 100, 7, 127))
        progress.assert_awaited_with('history')
        self.assertEqual([r['type'] for r in server.requests], ['prompt', 'steer'])
        self.gateway.revoke.assert_called_once_with('TEST_TOKEN')

    async def test_deadline_aborts_and_prefixes_deadline_note(self) -> None:
        server = FakePiRpcServer()
        engine = self._engine(server)
        engine.clock = lambda: 7200 if any(r['type'] == 'prompt' for r in server.requests) else 7199
        report = await engine(
            {'job_id': 8, 'task': 'TEST', 'deadline_at': 7200},
            self._session(),
            feedback=AsyncMock(return_value=[]),
            progress=AsyncMock(),
            trace=AgentTrace(),
        )
        self.assertEqual(report, DEADLINE_NOTE + '\nTEST partial pi report')
        self.assertEqual([r['type'] for r in server.requests], ['prompt', 'abort'])

    async def test_steer_rejection_and_model_error_raise(self) -> None:
        server = FakePiRpcServer(steer_error='cannot steer')
        with self.assertRaisesRegex(RuntimeError, '追加要求未被 Pi 接受'):
            await self._engine(server)(
                {'job_id': 9, 'task': 'TEST', 'deadline_at': 7200},
                self._session(),
                feedback=AsyncMock(return_value=['more']),
                progress=AsyncMock(),
                trace=AgentTrace(),
            )
        err_server = FakePiRpcServer(fail_error='upstream down')
        with self.assertRaisesRegex(RuntimeError, 'upstream down'):
            await self._engine(err_server)(
                {'job_id': 10, 'task': 'TEST', 'deadline_at': 7200},
                self._session(),
                feedback=AsyncMock(return_value=[]),
                progress=AsyncMock(),
                trace=AgentTrace(),
            )

    async def test_fallback_model_and_reason_are_reported(self) -> None:
        server = FakePiRpcServer()
        server.last_model = 'fallback-model'
        server.fallbacks = [('primary-model', 'fallback-model', 'HTTP 404')]
        trace = AgentTrace()
        report = await self._engine(server)({'job_id': 16, 'task': 'TEST', 'deadline_at': 7200},
            self._session(), feedback=AsyncMock(return_value=['continue']), progress=AsyncMock(), trace=trace)
        self.assertEqual(trace.model, 'fallback-model')
        self.assertIn('primary-model → fallback-model，原因：HTTP 404', report)

    async def test_deadline_does_not_hang_when_pi_ignores_abort(self) -> None:
        class Stuck(FakePiRpcServer):
            async def send(self, message):
                if message.get('type') == 'abort':
                    self.requests.append(message)
                    return
                await super().send(message)

        server = Stuck()
        engine = self._engine(server)
        engine.clock = lambda: 7200 if any(r['type'] == 'prompt' for r in server.requests) else 7199
        with patch('src.plugins.ai_chat.pi_engine.ABORT_TIMEOUT', 0.05, create=True), \
             patch('src.plugins.ai_chat.pi_engine.STOP_GRACE', 0.05, create=True):
            try:
                report = await asyncio.wait_for(engine(
                    {'job_id': 15, 'task': 'TEST', 'deadline_at': 7200}, self._session(),
                    feedback=AsyncMock(return_value=[]), progress=AsyncMock(), trace=AgentTrace()), 1)
            except TimeoutError:
                self.fail('the engine waited indefinitely for the abort acknowledgement')
        self.assertTrue(report.startswith(DEADLINE_NOTE))
        self.assertIn('abort', [request['type'] for request in server.requests])
        self.gateway.revoke.assert_called_once_with('TEST_TOKEN')

    async def test_early_disconnect_fails_and_revokes_token(self) -> None:
        class Disconnected(FakePiRpcServer):
            async def receive(self):
                record = await super().receive()
                if record is None:
                    raise EOFError('connection closed')
                return record

            async def send(self, message):
                await super().send(message)
                if message.get('type') == 'prompt':
                    self.outbox.put_nowait(None)

        with self.assertRaisesRegex(RuntimeError, 'connection closed'):
            await self._engine(Disconnected())(
                {'job_id': 11, 'task': 'TEST', 'deadline_at': 7200}, self._session(),
                feedback=AsyncMock(return_value=[]), progress=AsyncMock(), trace=AgentTrace())
        self.gateway.revoke.assert_called_once_with('TEST_TOKEN')

    async def test_cancel_aborts_and_revokes_token(self) -> None:
        server = FakePiRpcServer()
        started = asyncio.Event()
        async def progress(_tool):
            started.set()
        task = asyncio.create_task(self._engine(server)(
            {'job_id': 12, 'task': 'TEST', 'deadline_at': 7200}, self._session(),
            feedback=AsyncMock(return_value=[]), progress=progress, trace=AgentTrace()))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIn('abort', [request['type'] for request in server.requests])
        self.gateway.revoke.assert_called_once_with('TEST_TOKEN')

    async def test_repeated_cancel_drains_the_same_ui_handler(self) -> None:
        entered, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        finished = []
        class CleanupServer(FakePiRpcServer):
            async def send(self, message):
                if message.get('type') == 'extension_ui_response':
                    entered.set()
                    try:
                        await asyncio.Future()
                    finally:
                        draining.set()
                        await release.wait()
                        finished.append(True)
                    return
                await super().send(message)
                if message.get('type') == 'prompt':
                    self.push({'type': 'extension_ui_request', 'id': 'ui-1'})

        server = CleanupServer()
        task = asyncio.create_task(self._engine(server)(
            {'job_id': 17, 'task': 'TEST', 'deadline_at': 7200}, self._session(),
            feedback=AsyncMock(return_value=[]), progress=AsyncMock(), trace=AgentTrace()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            await asyncio.wait_for(draining.wait(), 2)
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done(), 'second cancellation interrupted handler cleanup')
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(finished, [True])
        self.gateway.revoke.assert_called_once_with('TEST_TOKEN')

    async def test_relay_error_preferred_and_partial_text_preserved(self) -> None:
        server = FakePiRpcServer(fail_error='generic pi error')
        engine = self._engine(server)
        self.gateway.failure = lambda _: 'precise relay error'
        with self.assertRaisesRegex(RuntimeError, 'precise relay error'):
            await engine({'job_id': 13, 'task': 'TEST', 'deadline_at': 7200}, self._session(),
                         feedback=AsyncMock(return_value=[]), progress=AsyncMock(), trace=AgentTrace())

        class Partial(FakePiRpcServer):
            async def send(self, message):
                await super().send(message)
                if message.get('type') == 'prompt':
                    self._finish('useful partial result', stop_reason='error')

        report = await self._engine(Partial())({'job_id': 14, 'task': 'TEST', 'deadline_at': 7200},
            self._session(), feedback=AsyncMock(return_value=[]), progress=AsyncMock(), trace=AgentTrace())
        self.assertEqual(report, 'useful partial result')

    async def test_real_bridge_subprocess_roundtrip_over_gateway_tunnel(self) -> None:
        gateway = JobGateway(
            port=free_port(),
            upstream=lambda: profile('relay', base_url='http://upstream.test/v1', api_key='KEY'),
            model=lambda: ('TEST_MODEL', 'high'),
            logger=logging.getLogger(__name__),
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b''))),
        )
        await gateway.start()
        self.addAsyncCleanup(gateway.close)

        with tempfile.TemporaryDirectory(prefix='pi_bridge_') as tmp:
            fake_pi = Path(tmp) / 'fake_pi.py'
            fake_pi.write_text(
                "import json, sys\n"
                "for line in sys.stdin:\n"
                "    req = json.loads(line)\n"
                "    sys.stdout.write(json.dumps({'id': req['id'], 'type': 'response', 'command': req['type'], 'success': True, 'data': {'disposition': 'started'}}) + '\\n')\n"
                "    sys.stdout.write(json.dumps({'type': 'message_end', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'bridge ok'}], 'usage': {'input': 5, 'output': 2, 'cacheRead': 0, 'totalTokens': 7}, 'stopReason': 'stop'}}) + '\\n')\n"
                "    sys.stdout.write(json.dumps({'type': 'agent_settled', 'aborted': False}) + '\\n')\n"
                "    sys.stdout.flush()\n",
                encoding='utf-8',
            )
            bridge_path = Path(__file__).resolve().parents[1] / 'src' / 'plugins' / 'ai_chat' / 'sandbox_agent_bridge.py'
            procs: list[asyncio.subprocess.Process] = []

            async def guest_exec(_vm, _script):
                token = next(iter(gateway.channels))
                env = {**os.environ, 'GAOJI_JOB_TOKEN': token, 'GAOJI_PI_CWD': tmp,
                       'GAOJI_AGENT_ERR': str(Path(tmp) / 'agent.err')}
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(bridge_path),
                    '127.0.0.1',
                    str(gateway.port + 1),
                    '--',
                    sys.executable,
                    str(fake_pi),
                    env=env,
                    cwd=tmp,
                )
                procs.append(proc)

            engine = PiEngine(
                gateway,
                SimpleNamespace(get=lambda: ('TEST_MODEL', 'high')),
                start_vm=AsyncMock(return_value='s123abc'),
                guest_exec=guest_exec,
                clock=lambda: 0,
            )
            session = self._session()
            trace = AgentTrace()
            report = await engine(
                {'job_id': 12, 'task': 'TEST bridge', 'deadline_at': 7200},
                session,
                feedback=AsyncMock(return_value=[]),
                progress=AsyncMock(),
                trace=trace,
            )
            self.assertEqual(report, 'bridge ok')
            self.assertEqual(trace.total_tokens, 7)
            for proc in procs:
                await asyncio.wait_for(proc.wait(), 5)

    def test_upstream_profile_reads_the_real_catalog(self) -> None:
        from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelProfile

        def make_profile(name, provider):
            return ModelProfile(name=name, provider=provider, protocol='openai-chat', model=name,
                                base_url='http://127.0.0.1/v1', api_key_required=False)

        catalog = ModelCatalog({'TEST_DS': make_profile('TEST_DS', 'deepseek'),
                                'TEST_CP': make_profile('TEST_CP', 'cliproxy')},
                               default_profile='TEST_DS')
        self.assertEqual(upstream_profile(catalog, SimpleNamespace(background_upstream_profile='')).name, 'test_cp')
        self.assertEqual(upstream_profile(catalog, SimpleNamespace(background_upstream_profile='TEST_DS')).name, 'test_ds')
        with self.assertRaises(RuntimeError):
            upstream_profile(catalog, SimpleNamespace(background_upstream_profile='TEST_NONE'))

    def test_background_model_settings_chain_and_validation(self) -> None:
        from src.plugins.ai_chat.config import Settings

        prefs_store = {}
        prefs = SimpleNamespace(
            get_explicit=lambda k: prefs_store.get(k),
            set=lambda k, v: prefs_store.__setitem__(k, v),
        )
        cfg = Settings.from_env({
            'AI_BACKGROUND_MODELS': 'm1, m2 , m3',
            'AI_BACKGROUND_EFFORT': 'medium',
        })
        settings = BackgroundModelSettings(prefs, cfg)
        self.assertEqual(settings.chain(), ['m1', 'm2', 'm3'])
        self.assertEqual(Settings.from_env({}).background_models, ('gpt-6.1-sol',))
        self.assertEqual(Settings.from_env({'AI_BACKGROUND_MODEL': 'legacy'}).background_models, ('legacy',))
        for bad in (',,,', 'a, ,b', ','.join(f'm{i}' for i in range(9)), 'x' * 101):
            with self.assertRaises(ValueError):
                Settings.from_env({'AI_BACKGROUND_MODELS': bad})
        self.assertEqual(settings.get(), ('m1', 'medium'))
        settings.set('alpha, beta', 'high')
        self.assertEqual(settings.chain(), ['alpha', 'beta'])
        self.assertEqual(settings.get(), ('alpha', 'high'))
        for bad in ('', 'a, , b', ','.join(f'm{i}' for i in range(9)), 'x' * 101):
            with self.assertRaises(ValueError):
                settings.set(bad, 'high')


if __name__ == '__main__':
    unittest.main()
