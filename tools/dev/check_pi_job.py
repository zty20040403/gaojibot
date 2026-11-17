#!/usr/bin/env python3
"""Run real pi 1.5.0 through the real gateway against a loopback fake Responses API.

Requires Node >=22.19, npx, and the project Python environment. No VM or real
model credentials are used. Each run keeps its temporary files only until exit.
"""
import asyncio
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import time
from types import SimpleNamespace

from aiohttp import web
import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.plugins.ai_chat.agent_loop import AgentTrace  # noqa: E402
from src.plugins.ai_chat.job_gateway import JobGateway  # noqa: E402
from src.plugins.ai_chat.model_catalog import ModelProfile  # noqa: E402
from src.plugins.ai_chat.pi_engine import PiEngine, pi_arguments, pi_models_json  # noqa: E402


class RecordingGateway(JobGateway):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls = []
        self.token = None

    def open(self, **kwargs):
        self.token, channel = super().open(**kwargs)
        return self.token, channel

    async def _responses(self, request):
        self.calls.append(('responses', request.headers.get('authorization')))
        return await super()._responses(request)

    async def _job_tools(self, request):
        self.calls.append(('tools', request.headers.get('authorization')))
        return await super()._job_tools(request)

    async def _job_tool(self, request):
        self.calls.append(('tool', request.headers.get('authorization')))
        return await super()._job_tool(request)


class FakeModel:
    def __init__(self):
        self.requests = []

    async def respond(self, request):
        self.requests.append((request.headers.get('Authorization'), await request.json()))
        number = len(self.requests)
        if number > 2:
            return web.json_response({'error': 'unexpected extra request'}, status=400)
        if number == 1:
            item = {'type': 'function_call', 'id': 'fc_1', 'call_id': 'call_1',
                    'name': 'history', 'arguments': '{"query":"test"}', 'status': 'completed'}
            added = {**item, 'arguments': '', 'status': 'in_progress'}
            events = [
                {'type': 'response.output_item.added', 'output_index': 0, 'item': added},
                {'type': 'response.function_call_arguments.delta', 'output_index': 0, 'item_id': 'fc_1',
                 'delta': item['arguments']},
                {'type': 'response.function_call_arguments.done', 'output_index': 0, 'item_id': 'fc_1',
                 'arguments': item['arguments']},
                {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
            ]
        else:
            item = {'type': 'message', 'id': 'msg_2', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': 'TEST final report', 'annotations': []}]}
            events = [
                {'type': 'response.output_item.added', 'output_index': 0, 'item': {**item, 'content': []}},
                {'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0,
                 'item_id': 'msg_2', 'delta': 'TEST final report'},
                {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
            ]
        response = {'id': f'resp_{number}', 'object': 'response', 'status': 'completed', 'output': [item],
                    'usage': {'input_tokens': 12, 'output_tokens': 3, 'total_tokens': 15,
                              'input_tokens_details': {'cached_tokens': 2}, 'output_tokens_details': {}}}
        events.insert(0, {'type': 'response.created', 'response': {'id': response['id']}})
        events.append({'type': 'response.completed', 'response': response})
        body = ''.join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events) + 'data: [DONE]\n\n'
        return web.Response(text=body, content_type='text/event-stream')


class Receipt:
    def __init__(self, sink):
        self.sink, self.usage = sink, None

    async def start(self):
        pass

    def observe(self, usage):
        self.usage = usage

    async def finish(self):
        self.sink.append(self.usage)


class FakeSession:
    def __init__(self):
        self.prompt = 'Use history to answer the task.'
        self.calls = []
        self.tools = ({'function': {'name': 'history', 'description': 'Read earlier messages.',
                       'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}},
                                      'required': ['query']}}},)

    async def current(self):
        pass

    async def execute_tool(self, name, arguments):
        self.calls.append((name, arguments))
        await asyncio.sleep(0.06)  # Cross several host heartbeat intervals.
        return 'TEST historical context'


def available_port_pair():
    # Avoid the usual macOS/Linux ephemeral-client range: a new connection can
    # claim an apparently available port between this probe and gateway.start().
    for _ in range(100):
        port = 20000 + 2 * secrets.randbelow(14000)
        with socket.socket() as first, socket.socket() as second:
            try:
                first.bind(('127.0.0.1', port))
                second.bind(('127.0.0.1', port + 1))
            except OSError:
                continue
            return port
    raise RuntimeError('could not find two adjacent loopback ports')


def check(condition, label):
    if not condition:
        raise AssertionError(label)
    print('PASS', label, flush=True)


async def main():
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
    receipts, processes, progress = [], [], []
    gateway = RecordingGateway(port=available_port_pair(), upstream=lambda: profile,
        model=lambda: ('REAL_MODEL', 'high'), logger=logging.getLogger(__name__),
        billing=lambda _: SimpleNamespace(request=lambda _profile: Receipt(receipts)), tool_heartbeat_seconds=0.01)
    session, trace = FakeSession(), AgentTrace()
    with tempfile.TemporaryDirectory(prefix='gaoji-pi-check-') as temporary:
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

        async def mark(name):
            progress.append(name)

        try:
            engine = PiEngine(gateway, SimpleNamespace(get=lambda: ('REAL_MODEL', 'high')),
                start_vm=lambda _: asyncio.sleep(0, result='local'), guest_exec=guest_exec, clock=time.time)
            report = await asyncio.wait_for(engine({'job_id': 1, 'task': 'Read history.',
                'deadline_at': time.time() + 100}, session, feedback=lambda: asyncio.sleep(0, result=[]),
                progress=mark, trace=trace), 90)
            check(any(name == 'tools' for name, _ in gateway.calls)
                  and any(tool.get('name') == 'history' for tool in model.requests[0][1].get('tools', [])),
                  'plugin listed host tools to the real model client')
            check('history' in progress and session.calls == [('history', {'query': 'test'})], 'prompt and host tool executed')
            check(any(item.get('type') == 'function_call_output' and 'TEST historical context' in str(item.get('output'))
                      for item in model.requests[1][1].get('input', [])), 'tool result returned to model')
            check(report == 'TEST final report', 'final assistant report')
            check(len(model.requests) == 2 and all(auth == 'Bearer TEST_UPSTREAM_KEY' for auth, _ in model.requests),
                  'two model calls use host upstream credential')
            check(all(auth == 'Bearer ' + gateway.token for _, auth in gateway.calls), 'gateway calls use job token')
            check(len(receipts) == 2 and all(usage.known for usage in receipts), 'model usage receipts finished')
            check((trace.input_tokens, trace.cached_tokens, trace.output_tokens) == (24, 4, 6), 'pi usage recorded')
            async with httpx.AsyncClient(trust_env=False) as client:
                denied = await client.get(f'http://127.0.0.1:{gateway.port}/job/tools',
                                          headers={'Authorization': 'Bearer ' + gateway.token})
            check(not gateway.channels and denied.status_code == 401, 'job token revoked after completion')
        except BaseException:
            error_file = tmp / 'agent.err'
            if error_file.exists():
                print('pi stderr:', error_file.read_text(errors='replace')[-8000:], file=sys.stderr)
            raise
        finally:
            await gateway.close()
            for process in processes:
                if process.returncode is None:
                    try:
                        await asyncio.wait_for(process.wait(), 12)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
            await server.cleanup()

    from check_pi_rpc_lifecycle import run_lifecycle
    await run_lifecycle()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f'FAIL {type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(1) from exc
