"""Background jobs run by Codex in the conversation VM, driven over its app-server protocol.

The host gives Codex its own tools (history, search, ask_main, ...) as dynamic tools,
relays its model calls with the host's key, steers it with the user's additions and
stops it at the deadline; Codex's last message is the report.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from pathlib import Path

from .tool_images import ToolImage

CODEX_VERSION = '0.153.4'
CODEX_URL = ('https://github.com/openai/codex/releases/download/'
             f'rust-v{CODEX_VERSION}/codex-x86_64-unknown-linux-musl.tar.gz')
CODEX_SHA256 = 'f479424eca092484dc40d87ae28c44f4cc40234a60045d6131e493800d814a30'
HOST = '10.0.2.2'
DEADLINE_NOTE = '[到点停了，下面是做到一半的]'
EFFORTS = ('low', 'medium', 'high', 'xhigh')


class BackgroundModelSettings:
    """The background model and effort, changed from the console."""

    def __init__(self, preferences, settings):
        self.preferences, self.settings = preferences, settings

    def get(self):
        model = self.preferences.get_explicit('background:model') or self.settings.background_model
        effort = self.preferences.get_explicit('background:effort') or self.settings.background_effort
        return model, effort

    def set(self, model, effort):
        model = model.strip()
        if not model or len(model) > 100 or effort not in EFFORTS:
            raise ValueError('模型名不能为空，推理强度只能是 ' + '/'.join(EFFORTS))
        self.preferences.set('background:model', model)
        self.preferences.set('background:effort', effort)


def upstream_profile(catalog, settings):
    """The profile whose endpoint and key relay background calls (cliproxy by default)."""
    wanted = settings.background_upstream_profile
    for profile in catalog.profiles():
        if (profile.name == wanted) if wanted else profile.provider == 'cliproxy':
            return profile
    raise RuntimeError('没有可用于后台任务的模型端点，请设置 AI_BACKGROUND_UPSTREAM_PROFILE。')


def codex_config(model, port):
    return '\n'.join([
        f'model = "{model}"', 'model_provider = "gaoji"', 'approval_policy = "never"',
        'sandbox_mode = "danger-full-access"', 'web_search = "disabled"', 'check_for_update_on_startup = false',
        '[model_providers.gaoji]', 'name = "gaoji"', f'base_url = "http://{HOST}:{port}/v1"',
        'env_key = "GAOJI_JOB_TOKEN"', 'wire_api = "responses"', 'stream_idle_timeout_ms = 300000', ''])


def launch_script(job_id, token, model, port):
    """Install the pinned Codex if missing, then start the bridge detached from the guest agent."""
    bridge = (Path(__file__).parent / 'sandbox_codex_bridge.py').read_bytes()
    encode = lambda data: base64.b64encode(data).decode()
    job_dir = f'/work/.gaoji/jobs/job-{job_id}'
    return f'''set -eu
if [ "$(codex --version 2>/dev/null || true)" != "codex-cli {CODEX_VERSION}" ]; then
  t=$(mktemp -d)
  curl -fsSL --retry 3 --max-time 600 -o "$t/codex.tgz" {CODEX_URL}
  echo "{CODEX_SHA256}  $t/codex.tgz" | sha256sum -c - >/dev/null
  tar -xzf "$t/codex.tgz" -C "$t"
  install -m 0755 "$t/codex-x86_64-unknown-linux-musl" /usr/local/bin/codex
  rm -rf "$t"
fi
mkdir -p {job_dir}/codex /usr/local/share/gaoji
printf %s {encode(codex_config(model, port).encode())} | base64 -d > {job_dir}/codex/config.toml
printf %s {encode(bridge)} | base64 -d > /usr/local/share/gaoji/sandbox_codex_bridge.py
GAOJI_JOB_TOKEN={token} CODEX_HOME={job_dir}/codex setsid nohup /usr/bin/python3 \\
  /usr/local/share/gaoji/sandbox_codex_bridge.py {HOST} {port + 1} >{job_dir}/bridge.log 2>&1 </dev/null &
'''


def _tool_spec(definition):
    function = definition['function']
    return {'type': 'function', 'name': function['name'], 'description': function['description'],
            'inputSchema': function['parameters']}


def _tool_output(result):
    if isinstance(result, ToolImage):
        return [{'type': 'inputText', 'text': result.text}, {'type': 'inputImage', 'imageUrl': result.data_url}]
    return [{'type': 'inputText', 'text': result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)}]


_TOOL_NAMES = {'commandExecution': 'shell', 'fileChange': 'apply_patch', 'imageView': 'view_image',
               'webSearch': 'web_search'}


class CodexRun:
    """One app-server conversation: requests, notifications and the host's tool answers."""

    def __init__(self, channel, session, progress, trace):
        self.channel, self.session, self.progress, self.trace = channel, session, progress, trace
        self.pending, self.next_id, self.handlers = {}, 0, set()
        self.thread = self.turn = None
        self.last_text, self.status, self.error = '', None, ''
        self.done = asyncio.Event()

    async def request(self, method, params):
        self.next_id += 1
        future = self.pending[self.next_id] = asyncio.get_running_loop().create_future()
        await self.channel.send({'jsonrpc': '2.0', 'id': self.next_id, 'method': method, 'params': params})
        reply = await future
        if 'error' in reply:
            raise RuntimeError(f"Codex {method} 失败：{reply['error'].get('message')}")
        return reply['result']

    async def read(self):
        try:
            while True:
                message = await self.channel.receive()
                if 'method' not in message:
                    future = self.pending.pop(message.get('id'), None)
                    if future is not None and not future.done():
                        future.set_result(message)
                elif 'id' in message:
                    task = asyncio.create_task(self.answer(message))
                    self.handlers.add(task)
                    task.add_done_callback(self.handlers.discard)
                else:
                    await self.notice(message['method'], message.get('params') or {})
        except (EOFError, ConnectionError, OSError) as exc:
            if not self.done.is_set():
                self.status, self.error = 'failed', str(exc) or 'Codex 连接中断。'
                self.done.set()

    async def answer(self, message):
        if message['method'] != 'item/tool/call':
            # approval_policy is never; nothing else here has anyone to answer it.
            await self.channel.send({'jsonrpc': '2.0', 'id': message['id'],
                                     'error': {'code': -32601, 'message': 'not supported in background jobs'}})
            return
        params = message['params']
        try:
            result = await self.session.execute_tool(params['tool'], params.get('arguments') or {})
            reply = {'success': True, 'contentItems': _tool_output(result)}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reply = {'success': False, 'contentItems': [{'type': 'inputText', 'text': f'{type(exc).__name__}: {exc}'}]}
        await self.channel.send({'jsonrpc': '2.0', 'id': message['id'], 'result': reply})

    async def notice(self, method, params):
        item = params.get('item') or {}
        if method == 'turn/started':
            self.turn = params['turn']['id']
        elif method == 'item/started' and item.get('type') in (*_TOOL_NAMES, 'dynamicToolCall', 'mcpToolCall'):
            await self.progress(item.get('tool') or _TOOL_NAMES.get(item['type'], item['type']))
        elif method == 'item/completed' and item.get('type') == 'agentMessage':
            self.last_text = item.get('text') or ''
        elif method == 'thread/tokenUsage/updated':
            total = params['tokenUsage']['total']
            self.trace.input_tokens, self.trace.output_tokens = total['inputTokens'], total['outputTokens']
            self.trace.cached_tokens = total.get('cachedInputTokens', 0)
            self.trace.total_tokens = total.get('totalTokens', 0)
        elif method == 'turn/completed':
            turn = params['turn']
            self.status = turn['status']
            self.error = ((turn.get('error') or {}).get('message') or '') if self.status == 'failed' else ''
            self.done.set()

    async def steer(self, text):
        with contextlib.suppress(RuntimeError):
            await self.request('turn/steer', {'threadId': self.thread, 'expectedTurnId': self.turn,
                                              'input': [{'type': 'text', 'text': text}]})

    async def interrupt(self):
        with contextlib.suppress(RuntimeError):
            await self.request('turn/interrupt', {'threadId': self.thread, 'turnId': self.turn})


class CodexEngine:
    def __init__(self, gateway, model_settings, *, start_vm, guest_exec, clock):
        # start_vm(row) -> sandbox handle; guest_exec(handle, script) runs a shell script as root.
        self.gateway, self.model_settings = gateway, model_settings
        self.start_vm, self.guest_exec, self.clock = start_vm, guest_exec, clock

    async def __call__(self, row, session, *, feedback, progress, trace):
        model, _ = self.model_settings.get()
        trace.provider, trace.model = 'codex', model
        await self.gateway.start()
        token, channel = self.gateway.open()
        run = CodexRun(channel, session, progress, trace)
        reader = None
        try:
            vm = await self.start_vm(row)
            await self.guest_exec(vm, launch_script(row['job_id'], token, model, self.gateway.port))
            await channel.wait(120)
            reader = asyncio.create_task(run.read())
            await run.request('initialize', {'clientInfo': {'name': 'gaoji', 'version': '1'},
                                             'capabilities': {'experimentalApi': True}})
            await channel.send({'jsonrpc': '2.0', 'method': 'initialized'})
            started = await run.request('thread/start', {
                'cwd': '/work', 'model': model, 'approvalPolicy': 'never', 'sandbox': 'danger-full-access',
                'developerInstructions': session.prompt, 'dynamicTools': [_tool_spec(item) for item in session.tools]})
            run.thread = started['thread']['id']
            turn = await run.request('turn/start', {'threadId': run.thread,
                                                    'input': [{'type': 'text', 'text': row['task']}]})
            run.turn = turn['turn']['id']
            stopped = False
            while not run.done.is_set():
                for message in await feedback():
                    await run.steer(message)
                if not stopped and self.clock() >= row['deadline_at']:
                    stopped = True
                    await run.interrupt()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(run.done.wait(), 2)
            if run.status == 'failed':
                raise RuntimeError(run.error or 'Codex 运行失败。')
            text = run.last_text.strip() or f"Codex 没有留下文字报告，会话记录在 /work/.gaoji/jobs/job-{row['job_id']}/codex。"
            return f'{DEADLINE_NOTE}\n{text}' if stopped else text
        except asyncio.CancelledError:
            if run.thread and run.turn and not run.done.is_set():
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(run.interrupt()), 5)
            raise
        finally:
            self.gateway.revoke(token)
            for task in (reader, *run.handlers):
                if task is not None:
                    task.cancel()
