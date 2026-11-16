"""Exercise real pi steering and abort over the normal gateway and RPC tunnel."""
import asyncio
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web

from check_pi_job import FakeModel, FakeSession, RecordingGateway, available_port_pair, check
from src.plugins.ai_chat.agent_loop import AgentTrace
from src.plugins.ai_chat.codex_engine import DEADLINE_NOTE
from src.plugins.ai_chat.model_catalog import ModelProfile
from src.plugins.ai_chat.pi_engine import PiEngine, PiRun, pi_arguments, pi_models_json

ROOT = Path(__file__).resolve().parents[2]


class WaitingSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.started, self.release, self.cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def execute_tool(self, name, arguments):
        self.calls.append((name, arguments))
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return 'TEST historical context'


async def exercise(mode):
    model = FakeModel()
    app = web.Application()
    app.router.add_post('/v1/responses', model.respond)
    server = web.AppRunner(app, access_log=None)
    await server.setup()
    site = web.TCPSite(server, '127.0.0.1', 0)
    await site.start()
    model_port = site._server.sockets[0].getsockname()[1]
    profile = ModelProfile(name='test', provider='test', protocol='openai-responses', model='REAL_MODEL',
                           base_url=f'http://127.0.0.1:{model_port}/v1', api_key='TEST_UPSTREAM_KEY')
    gateway = RecordingGateway(port=available_port_pair(), upstream=lambda: profile,
        model=lambda: ('REAL_MODEL', 'high'), logger=logging.getLogger(__name__), tool_heartbeat_seconds=0.05)
    session, trace, events, processes = WaitingSession(), AgentTrace(), [], []
    sent = False
    release_task = None

    class RecordingRun(PiRun):
        async def _handle_event(self, event):
            events.append(event)
            await super()._handle_event(event)

    with tempfile.TemporaryDirectory(prefix=f'gaoji-pi-{mode}-') as temporary:
        tmp = Path(temporary)
        conf = tmp / 'config'
        conf.mkdir(mode=0o700)
        (conf / 'job-system.md').write_text(session.prompt, encoding='utf-8')
        (conf / 'settings.json').write_text(json.dumps({'steeringMode': 'all', 'retry': {'maxRetries': 8}}))
        config = pi_models_json(gateway.port)
        config['providers']['gaoji']['baseUrl'] = f'http://127.0.0.1:{gateway.port}/v1'
        (conf / 'models.json').write_text(json.dumps(config), encoding='utf-8')
        args = pi_arguments('high', str(conf), str(tmp / 'sessions'),
                            str(ROOT / 'src/plugins/ai_chat/sandbox_pi_tools.ts'))

        async def guest_exec(_vm, _script):
            env = {'PATH': os.environ['PATH'], 'HOME': temporary, 'TMPDIR': temporary,
                   'NPM_CONFIG_CACHE': str(Path.home() / '.npm'), 'PI_CODING_AGENT_DIR': str(conf),
                   'PI_OFFLINE': '1', 'PI_SKIP_VERSION_CHECK': '1', 'PI_TELEMETRY': '0',
                   'GAOJI_HOST_URL': f'http://127.0.0.1:{gateway.port}', 'GAOJI_JOB_TOKEN': gateway.token,
                   'GAOJI_PI_CWD': temporary, 'GAOJI_AGENT_ERR': str(tmp / 'agent.err')}
            command = [sys.executable, str(ROOT / 'src/plugins/ai_chat/sandbox_agent_bridge.py'),
                       '127.0.0.1', str(gateway.port + 1), '--',
                       'npx', '--yes', '@earendil-works/pi-coding-agent@1.5.0', *args]
            processes.append(await asyncio.create_subprocess_exec(*command, cwd=tmp, env=env,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL))

        async def feedback():
            nonlocal sent, release_task
            if mode == 'steer' and session.started.is_set() and not sent:
                sent = True
                async def release_after_steer():
                    await asyncio.sleep(0.4)
                    session.release.set()
                release_task = asyncio.create_task(release_after_steer())
                return ['TEST steer instruction']
            return []

        try:
            engine = PiEngine(gateway, SimpleNamespace(get=lambda: ('REAL_MODEL', 'high')),
                start_vm=lambda _: asyncio.sleep(0, result='local'), guest_exec=guest_exec, clock=time.time)
            row = {'job_id': 1, 'task': 'Read history.', 'deadline_at': time.time() + (6 if mode == 'abort' else 90)}
            with patch('src.plugins.ai_chat.pi_engine.PiRun', RecordingRun):
                report = await asyncio.wait_for(engine(row, session, feedback=feedback,
                    progress=lambda _: asyncio.sleep(0), trace=trace), 90)
            if mode == 'steer':
                check(sent and len(model.requests) == 2 and
                      'TEST steer instruction' in json.dumps(model.requests[1][1].get('input', [])),
                      'steer appears in the next real model request')
                check(report == 'TEST final report', 'steered job completed')
            else:
                check(session.started.is_set() and report.startswith(DEADLINE_NOTE), 'deadline sends abort')
                check(any(event.get('type') == 'agent_settled' and event.get('aborted') for event in events),
                      'real pi settled with aborted=true')
                await asyncio.wait_for(session.cancelled.wait(), 5)
                check(True, 'abort cancelled the host tool')
            check(not gateway.channels, f'{mode} job token revoked')
        except BaseException:
            error_file = tmp / 'agent.err'
            if error_file.exists():
                print('pi stderr:', error_file.read_text(errors='replace')[-8000:], file=sys.stderr)
            raise
        finally:
            if release_task:
                release_task.cancel()
                await asyncio.gather(release_task, return_exceptions=True)
            await gateway.close()
            for process in processes:
                if process.returncode is None:
                    try:
                        await asyncio.wait_for(process.wait(), 12)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
            await server.cleanup()


async def run_lifecycle():
    await exercise('steer')
    await exercise('abort')


if __name__ == '__main__':
    asyncio.run(run_lifecycle())
