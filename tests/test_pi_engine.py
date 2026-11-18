"""Pi --print engine tests: launch config, notes, --fork, inbox steering, deadlines, and host-restart reconnect."""
from __future__ import annotations

import asyncio
import inspect
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from src.plugins.ai_chat.agent_loop import AgentTrace
from src.plugins.ai_chat.pi_engine import (
    DEADLINE_NOTE,
    BackgroundModelSettings,
    PiEngine,
    _PREPARE_PY,
    launch_script,
    notes_slug,
    pi_arguments,
    pi_models_json,
    upstream_profile,
)


class FakePiGuest:
    """Simulates a container job unit controlled by launch_script / inspect_script / steer_script / stop_script."""

    def __init__(
        self,
        *,
        final_text: str = 'TEST final pi report',
        partial_text: str = 'TEST partial pi report',
        fail_error: str | None = None,
        wait_for_steer: bool = False,
        crash_on_start: bool = False,
        ignore_stop: bool = False,
    ) -> None:
        self.final_text = final_text
        self.partial_text = partial_text
        self.fail_error = fail_error
        self.wait_for_steer = wait_for_steer
        self.crash_on_start = crash_on_start
        self.ignore_stop = ignore_stop
        self.active = False
        self.token = ''
        self.steered = 0
        self.steers: list[str] = []
        self.last_tool = ''
        self.result: dict | None = None
        self.exit: dict | None = None
        self.err_tail = ''
        self.launched_scripts: list[str] = []
        self.stop_called = asyncio.Event()
        self.launched = asyncio.Event()
        self.steered_event = asyncio.Event()
        self.last_model: str | None = None
        self.fallbacks: list[tuple[str, str, str]] = []
        self.session = None
        self._polls = 0

    async def exec(self, _vm: str, script: str) -> str:
        if 'GAOJI_JOB_EOF' in script:
            self.launched_scripts.append(script)
            for line in script.splitlines():
                if line.startswith('export GAOJI_JOB_TOKEN='):
                    self.token = line.split('=', 1)[1].strip("'\"")
            if self.crash_on_start:
                self.active = False
                self.result = None
                self.exit = {'service_result': 'exit-code', 'exit_code': 'exited', 'exit_status': '1'}
                self.err_tail = 'pi failed to start'
            else:
                self.active = True
                self.last_tool = 'history'
            self.launched.set()
            return ''
        if script.startswith('if command -v systemctl'):
            self.stop_called.set()
            if not self.ignore_stop:
                self.active = False
                self.result = {
                    'text': self.partial_text,
                    'error': '',
                    'stopped': True,
                    'usage': {'input': 11, 'output': 4, 'cached': 0, 'total': 15},
                }
                self.exit = {'service_result': 'success', 'exit_code': 'killed', 'exit_status': 'USR2'}
            return ''
        if 'steered_count' in script and 'inbox' in script:
            # Execute the real base64 decode logic to capture steers accurately.
            with tempfile.TemporaryDirectory() as tmp:
                patched = script.replace('/run/gaoji/jobs/job-', f'{tmp}/job-')
                subprocess.run(['/bin/sh', '-c', patched], check=True)
                job_dirs = list(Path(tmp).glob('job-*'))
                if job_dirs:
                    inbox = job_dirs[0] / 'inbox'
                    for f in sorted(inbox.iterdir()):
                        if not f.name.startswith('.'):
                            self.steers.append(f.read_text('utf-8').strip())
                    self.steered = int((job_dirs[0] / 'steered_count').read_text('utf-8').strip())
            self.steered_event.set()
            if self.wait_for_steer and self.active:
                self._complete()
            return ''
        # inspect_script
        self._polls += 1
        if self.active and not self.wait_for_steer and self._polls >= 1:
            self._complete()
        return json.dumps({
            'active': self.active,
            'token': self.token,
            'steered': self.steered,
            'last_tool': self.last_tool,
            'result': self.result,
            'exit': self.exit,
            'err_tail': self.err_tail,
        })

    def _complete(self) -> None:
        self.active = False
        if self.fail_error:
            self.result = {
                'text': self.final_text if self.final_text != 'TEST final pi report' else '',
                'error': self.fail_error,
                'stopped': False,
                'usage': {'input': 20, 'output': 7, 'cached': 100, 'total': 127},
            }
            self.exit = {'service_result': 'exit-code', 'exit_code': 'exited', 'exit_status': '1'}
        else:
            self.result = {
                'text': self.final_text,
                'error': '',
                'stopped': False,
                'usage': {'input': 120, 'output': 7, 'cached': 100, 'total': 127},
            }
            self.exit = {'service_result': 'success', 'exit_code': 'exited', 'exit_status': '0'}


# Compatibility alias for tests importing FakePiRpcServer.
FakePiRpcServer = FakePiGuest


class PiEngineTests(unittest.IsolatedAsyncioTestCase):
    def _engine(self, guest: FakePiGuest, *, now: int = 0) -> PiEngine:
        def open_channel(**kwargs):
            tok = kwargs.get('token') or 'TEST_TOKEN'
            return tok, guest

        self.gateway = SimpleNamespace(
            start=AsyncMock(),
            open=Mock(side_effect=open_channel),
            revoke=Mock(),
            port=18640,
            failure=lambda token: '',
        )
        self.exec = AsyncMock(side_effect=guest.exec)
        settings = SimpleNamespace(get=lambda: ('TEST_PI_MODEL', 'high'))
        return PiEngine(
            self.gateway,
            settings,
            start_vm=AsyncMock(return_value='s123abc'),
            guest_exec=self.exec,
            clock=lambda: now,
            poll_interval=0.02,
        )

    def _session(self):
        tool = {'type': 'function', 'function': {'name': 'history', 'description': 'TEST', 'parameters': {'type': 'object'}}}
        return SimpleNamespace(prompt='TEST pi prompt', tools=(tool,), current=AsyncMock(), execute_tool=AsyncMock(return_value='ok'))

    def test_models_json_thinking_map_and_launch_script(self) -> None:
        self.assertIn('context_window', inspect.signature(pi_models_json).parameters)
        self.assertIn('context_window', inspect.signature(launch_script).parameters)
        cfg = pi_models_json(18640, context_window=123456)
        provider = cfg['providers']['gaoji']
        self.assertEqual(provider['baseUrl'], 'http://10.0.2.2:18640/v1')
        self.assertEqual(provider['api'], 'openai-responses')
        self.assertEqual(provider['apiKey'], '$GAOJI_JOB_TOKEN')
        self.assertTrue(provider['authHeader'])
        model_entry = provider['models'][0]
        self.assertEqual(model_entry['id'], 'gaoji-background')
        self.assertEqual(model_entry['contextWindow'], 123456)
        self.assertEqual(model_entry['thinkingLevelMap'], {'xhigh': 'xhigh', 'max': 'max'})
        self.assertEqual(provider['compat'], {'supportsLongCacheRetention': False, 'supportsMaxOutputTokens': False})

        args = pi_arguments('xhigh', '/run/gaoji/jobs/job-9', '/work/.gaoji/jobs', session_id='gaoji-job-9', title='Demo', fork_session='/work/.gaoji/jobs/prev.jsonl')
        self.assertIn('--print', args)
        self.assertNotIn('--mode', args)
        self.assertNotIn('--no-context-files', args)
        self.assertIn('--no-extensions', args)
        self.assertIn('--fork', args)

        script = launch_script(9, 'TOK', 'xhigh', 18640, 'hello prompt', task='@do_not_treat_as_file', title='My Job', context_window=123456)
        self.assertIn('PI_CODING_AGENT_DIR=/run/gaoji/jobs/job-9', script)
        self.assertIn('GAOJI_HOST_URL=http://10.0.2.2:18640', script)
        self.assertIn('/etc/gaoji/pi/sandbox_pi_tools.ts', script)
        self.assertIn('/etc/gaoji/pi/sandbox_pi_agent.ts', script)
        self.assertNotIn('sandbox_agent_bridge.py', script)
        self.assertIn('--print', script)
        self.assertIn('--thinking xhigh', script)
        self.assertIn('--session-dir /work/.gaoji/jobs', script)
        self.assertIn('--session-id gaoji-job-9', script)
        self.assertIn('KillSignal=SIGUSR2', script)
        self.assertIn('< /run/gaoji/jobs/job-9/task', script)
        self.assertIn('set -e\n[ -r /etc/set-environment ] && . /etc/set-environment\nset -u', script)
        self.assertNotIn('hello prompt', script)
        self.assertNotIn('@do_not_treat_as_file', script)

    def test_prepare_py_notes_truncation_symlink_and_fork_resolution(self) -> None:
        self.assertEqual(notes_slug('/work/my_repo/sub'), 'work-my-repo-sub')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / 'work'
            conf = root / 'conf'
            out = root / 'out'
            (work / '.gaoji' / 'projects').mkdir(parents=True)
            (work / '.gaoji' / 'jobs').mkdir(parents=True)
            conf.mkdir()
            out.mkdir()

            patched_py = _PREPARE_PY.replace("'/work", f"'{work}")
            prepare_file = conf / 'prepare.py'
            prepare_file.write_text(patched_py, 'utf-8')
            (conf / 'prompt-template.md').write_text('Dir=@WORKDIR@ File=@NOTES_FILE@\n[笔记]\n@NOTES@', 'utf-8')

            # 1) Create an >16 KiB notes file and a previous session JSONL for --fork.
            slug = notes_slug(f'{work}/app')
            notes_path = work / '.gaoji' / 'projects' / f'{slug}.md'
            big_notes = '\n'.join(f'line-{i:04d}-' + ('x' * 40) for i in range(500))
            self.assertGreater(len(big_notes.encode()), 16384)
            notes_path.write_text(big_notes, 'utf-8')

            prev_session = work / '.gaoji' / 'jobs' / '20261118_gaoji-job-5.jsonl'
            prev_session.write_text(
                json.dumps({'type': 'header', 'cwd': f'{work}/app'}) + '\n'
                + json.dumps({'type': 'session_info', 'name': 'Inherited Title'}) + '\n',
                'utf-8',
            )
            (conf / 'meta.json').write_text(
                json.dumps({'job_id': 6, 'workdir': '', 'title': '', 'resume_job_id': 5}),
                'utf-8',
            )
            subprocess.run([sys.executable, str(prepare_file), str(conf), str(out)], check=True)
            self.assertEqual((conf / 'workdir').read_text('utf-8'), f'{work}/app')
            self.assertEqual((conf / 'title').read_text('utf-8'), 'Inherited Title')
            self.assertEqual((conf / 'fork').read_text('utf-8'), str(prev_session))
            rendered = (conf / 'job-system.md').read_text('utf-8')
            self.assertIn('笔记超过 16 KiB，后面的没加载', rendered)
            self.assertLess(len(rendered.encode()), 17500)

            # 2) Symlink notes file is rejected safely.
            notes_path.unlink()
            notes_path.symlink_to('/etc/passwd')
            subprocess.run([sys.executable, str(prepare_file), str(conf), str(out)], check=True)
            self.assertIn('它是个链接', (conf / 'job-system.md').read_text('utf-8'))

    async def test_startup_crash_reports_distinct_startup_error(self) -> None:
        guest = FakePiGuest(crash_on_start=True)
        with self.assertRaisesRegex(RuntimeError, '后台进程没能启动'):
            await self._engine(guest)(
                {'job_id': 21, 'task': 'TEST', 'deadline_at': 7200},
                self._session(),
                feedback=AsyncMock(return_value=[]),
                progress=AsyncMock(),
                trace=AgentTrace(),
            )
        self.gateway.revoke.assert_called_once_with('TEST_TOKEN')

    async def test_steer_progress_usage_and_final_report(self) -> None:
        guest = FakePiGuest(wait_for_steer=True)
        session = self._session()
        trace = AgentTrace()
        progress = AsyncMock()
        feedback = AsyncMock(side_effect=[['TEST extra detail'], []])
        report = await self._engine(guest)(
            {'job_id': 7, 'task': 'TEST task', 'deadline_at': 7200},
            session,
            feedback=feedback,
            progress=progress,
            trace=trace,
        )
        self.assertEqual(report, 'TEST final pi report')
        self.assertEqual(guest.steers, ['TEST extra detail'])
        self.assertEqual(trace.provider, 'pi')
        self.assertEqual(trace.model, 'TEST_PI_MODEL')
        self.assertEqual((trace.input_tokens, trace.cached_tokens, trace.output_tokens, trace.total_tokens), (120, 100, 7, 127))
        progress.assert_awaited_with('history')
        self.gateway.revoke.assert_called_once_with('TEST_TOKEN')

    async def test_deadline_stops_with_sigusr2_and_prefixes_deadline_note(self) -> None:
        guest = FakePiGuest(wait_for_steer=True)
        engine = self._engine(guest)
        engine.clock = lambda: 7200 if guest.launched.is_set() else 7199
        report = await engine(
            {'job_id': 8, 'task': 'TEST', 'deadline_at': 7200},
            self._session(),
            feedback=AsyncMock(return_value=[]),
            progress=AsyncMock(),
            trace=AgentTrace(),
        )
        self.assertEqual(report, DEADLINE_NOTE + '\nTEST partial pi report')
        self.assertTrue(guest.stop_called.is_set())
        self.gateway.revoke.assert_called_once_with('TEST_TOKEN')

    async def test_model_error_and_relay_error_prefer_precise_message(self) -> None:
        guest = FakePiGuest(fail_error='upstream down')
        with self.assertRaisesRegex(RuntimeError, 'upstream down'):
            await self._engine(guest)(
                {'job_id': 10, 'task': 'TEST', 'deadline_at': 7200},
                self._session(),
                feedback=AsyncMock(return_value=[]),
                progress=AsyncMock(),
                trace=AgentTrace(),
            )
        relay_guest = FakePiGuest(fail_error='generic pi error')
        engine = self._engine(relay_guest)
        self.gateway.failure = lambda _: 'precise relay error'
        with self.assertRaisesRegex(RuntimeError, 'precise relay error'):
            await engine(
                {'job_id': 13, 'task': 'TEST', 'deadline_at': 7200},
                self._session(),
                feedback=AsyncMock(return_value=[]),
                progress=AsyncMock(),
                trace=AgentTrace(),
            )
        partial_guest = FakePiGuest(final_text='useful partial result', fail_error='late error')
        report = await self._engine(partial_guest)(
            {'job_id': 14, 'task': 'TEST', 'deadline_at': 7200},
            self._session(),
            feedback=AsyncMock(return_value=[]),
            progress=AsyncMock(),
            trace=AgentTrace(),
        )
        self.assertEqual(report, 'useful partial result')

    async def test_fallback_model_and_reason_are_reported(self) -> None:
        guest = FakePiGuest()
        guest.last_model = 'fallback-model'
        guest.fallbacks = [('primary-model', 'fallback-model', 'HTTP 404')]
        trace = AgentTrace()
        report = await self._engine(guest)(
            {'job_id': 16, 'task': 'TEST', 'deadline_at': 7200},
            self._session(),
            feedback=AsyncMock(return_value=[]),
            progress=AsyncMock(),
            trace=trace,
        )
        self.assertEqual(trace.model, 'fallback-model')
        self.assertIn('primary-model → fallback-model，原因：HTTP 404', report)

    async def test_host_restart_leaves_container_unit_running_and_reconnects(self) -> None:
        guest = FakePiGuest(wait_for_steer=True)
        engine1 = self._engine(guest)
        task1 = asyncio.create_task(
            engine1(
                {'job_id': 31, 'task': 'long work', 'deadline_at': 7200},
                self._session(),
                feedback=AsyncMock(return_value=['first steer']),
                progress=AsyncMock(),
                trace=AgentTrace(),
                is_cancelled=AsyncMock(return_value=False),
            )
        )
        await asyncio.wait_for(guest.launched.wait(), 2)
        # Wait until 'first steer' was written before host restarts, without completing.
        guest.wait_for_steer = False
        guest.active = True
        guest.result = None
        guest.exit = None
        task1.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task1
        # Container unit MUST NOT be stopped when host restarts (is_cancelled=False)!
        self.assertFalse(guest.stop_called.is_set())
        self.assertTrue(guest.active)
        self.assertEqual(len(guest.launched_scripts), 1)

        # Now host restarts and engine2 reconnects to the running container unit.
        guest.wait_for_steer = True
        engine2 = self._engine(guest)
        trace2 = AgentTrace()
        report = await engine2(
            {'job_id': 31, 'task': 'long work', 'started_at': 100, 'deadline_at': 7200},
            self._session(),
            feedback=AsyncMock(side_effect=[['first steer', 'second steer after restart'], []]),
            progress=AsyncMock(),
            trace=trace2,
            is_cancelled=AsyncMock(return_value=False),
        )
        self.assertEqual(report, 'TEST final pi report')
        # Did NOT relaunch run.sh; reused the existing container unit and token!
        self.assertEqual(len(guest.launched_scripts), 1)
        self.gateway.open.assert_called_once()
        self.assertEqual(self.gateway.open.call_args.kwargs['token'], guest.token)
        self.assertIn('second steer after restart', guest.steers)

    async def test_user_cancel_stops_unit_and_revokes_token(self) -> None:
        guest = FakePiGuest(wait_for_steer=True)
        engine = self._engine(guest)
        task = asyncio.create_task(
            engine(
                {'job_id': 12, 'task': 'TEST', 'deadline_at': 7200},
                self._session(),
                feedback=AsyncMock(return_value=[]),
                progress=AsyncMock(),
                trace=AgentTrace(),
                is_cancelled=AsyncMock(return_value=True),
            )
        )
        await asyncio.wait_for(guest.launched.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(guest.stop_called.is_set())
        self.gateway.revoke.assert_called_with('TEST_TOKEN')

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
        settings.set('alpha, beta', 'max')
        self.assertEqual(settings.chain(), ['alpha', 'beta'])
        self.assertEqual(settings.get(), ('alpha', 'max'))
        for bad in ('', 'a, , b', ','.join(f'm{i}' for i in range(9)), 'x' * 101):
            with self.assertRaises(ValueError):
                settings.set(bad, 'high')


if __name__ == '__main__':
    unittest.main()
