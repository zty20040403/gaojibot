"""Background jobs run by Pi in the conversation VM over its RPC protocol."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import shlex

from .billing_context import drain
from .codex_engine import DEADLINE_NOTE, HOST

ABORT_TIMEOUT = 5
STOP_GRACE = 10


def pi_models_json(port: int, *, context_window: int = 200000) -> dict:
    return {'providers': {'gaoji': {
        'baseUrl': f'http://{HOST}:{port}/v1',
        'api': 'openai-responses',
        'apiKey': '$GAOJI_JOB_TOKEN',
        'authHeader': True,
        'compat': {'supportsLongCacheRetention': False, 'supportsMaxOutputTokens': False},
        'models': [{'id': 'gaoji-background', 'name': 'gaoji-background', 'reasoning': True,
                    'input': ['text', 'image'], 'contextWindow': context_window, 'maxTokens': 32000}],
    }}}


def pi_arguments(effort: str, conf: str, session_dir: str, extension: str) -> tuple[str, ...]:
    return ('--mode', 'rpc', '--no-approve', '--session-dir', session_dir,
            '--provider', 'gaoji', '--model', 'gaoji-background', '--thinking', effort,
            '--append-system-prompt', f'{conf}/job-system.md', '--extension', extension,
            '--no-extensions', '--no-mcp', '--no-skills', '--no-prompt-templates', '--no-themes', '--no-context-files')


def launch_script(job_id: int, token: str, effort: str, port: int, prompt: str, *,
                  context_window: int = 200000, pi_bin: str = 'pi') -> str:
    if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0:
        raise ValueError('invalid background job id')
    encode = lambda data: base64.b64encode(data).decode()
    job_dir = f'/work/.gaoji/jobs/job-{job_id}'
    conf = f'/run/gaoji/jobs/job-{job_id}'
    models = json.dumps(pi_models_json(port, context_window=context_window), ensure_ascii=False).encode()
    settings = json.dumps({'steeringMode': 'all', 'retry': {'maxRetries': 8}}).encode()
    args = ' '.join(shlex.quote(arg) for arg in (pi_bin, *pi_arguments(
        effort, conf, f'{job_dir}/pi', '/etc/gaoji/pi/sandbox_pi_tools.ts')))
    return f'''set -eu
umask 077
mkdir -p {conf} {job_dir}/pi
chmod 700 {conf}
printf %s {encode(models)} | base64 -d > {conf}/models.json
printf %s {encode(settings)} | base64 -d > {conf}/settings.json
printf %s {encode(prompt.encode())} | base64 -d > {conf}/job-system.md
cd /work
GAOJI_JOB_TOKEN={shlex.quote(token)} GAOJI_HOST_URL={shlex.quote(f'http://{HOST}:{port}')} \\
  PI_CODING_AGENT_DIR={shlex.quote(conf)} PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0 HOME=/work \\
  GAOJI_AGENT_ERR={job_dir}/agent.err setsid nohup python3 /etc/gaoji/pi/sandbox_agent_bridge.py \\
  {HOST} {port + 1} -- {args} >{job_dir}/bridge.log 2>&1 </dev/null &
'''


class PiRun:
    """One Pi RPC session over the gateway channel."""

    def __init__(self, channel, progress, trace):
        self.channel, self.progress, self.trace = channel, progress, trace
        self.pending: dict[str, asyncio.Future] = {}
        self.next_id = 0
        self.handlers: set[asyncio.Task] = set()
        self.last_text = ''
        self.error = ''
        self.stop_reason = ''
        self.aborted = False
        self.settled = asyncio.Event()

    async def request(self, command: dict):
        self.next_id += 1
        req_id = f'req-{self.next_id}'
        payload = {'id': req_id, **command}
        future = self.pending[req_id] = asyncio.get_running_loop().create_future()
        try:
            await self.channel.send(payload)
            reply = await future
        finally:
            self.pending.pop(req_id, None)
        if not reply.get('success'):
            raise RuntimeError(f"Pi {command.get('type')} 失败：{reply.get('error')}")
        return reply.get('data')

    async def read(self):
        try:
            while True:
                record = await self.channel.receive()
                rtype = record.get('type')
                if rtype == 'response' and record.get('id') in self.pending:
                    future = self.pending.pop(record['id'])
                    if not future.done():
                        future.set_result(record)
                elif rtype == 'extension_ui_request' and 'id' in record:
                    task = asyncio.create_task(
                        self.channel.send({'type': 'extension_ui_response', 'id': record['id'], 'cancelled': True})
                    )
                    self.handlers.add(task)
                    task.add_done_callback(self.handlers.discard)
                else:
                    await self._handle_event(record)
        except Exception as exc:
            if not self.settled.is_set():
                self.error = self.error or str(exc) or 'Pi 连接中断。'
                self.settled.set()
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(RuntimeError(self.error or 'Pi 连接中断。'))
            self.pending.clear()

    async def _handle_event(self, event: dict):
        etype = event.get('type')
        if etype == 'tool_execution_start':
            tool_name = event.get('toolName') or ''
            if tool_name:
                await self.progress(tool_name)
        elif etype == 'message_end':
            message = event.get('message') or {}
            if message.get('role') == 'assistant':
                parts = [
                    block.get('text', '')
                    for block in (message.get('content') or ())
                    if isinstance(block, dict) and block.get('type') == 'text'
                ]
                joined = ''.join(parts)
                if joined.strip():
                    self.last_text = joined
                usage = message.get('usage') or {}
                if isinstance(usage, dict):
                    self.trace.input_tokens += sum(int(usage.get(key) or 0) for key in ('input', 'cacheRead', 'cacheWrite'))
                    self.trace.output_tokens += int(usage.get('output') or 0)
                    self.trace.cached_tokens += int(usage.get('cacheRead') or 0)
                    self.trace.total_tokens += int(usage.get('totalTokens') or 0)
                reason = message.get('stopReason') or ''
                if reason:
                    self.stop_reason = reason
                if reason == 'error':
                    self.error = message.get('errorMessage') or self.error or 'Pi 运行失败。'
                elif reason:
                    self.error = ''
        elif etype == 'agent_settled':
            self.aborted = bool(event.get('aborted'))
            self.settled.set()

    async def steer(self, text: str):
        try:
            await self.request({'type': 'steer', 'message': text})
        except RuntimeError as exc:
            raise RuntimeError(f'追加要求未被 Pi 接受，任务已停止；请带上补充要求重新发起。原因：{exc}') from exc

    async def abort(self):
        with contextlib.suppress(RuntimeError):
            await self.request({'type': 'abort'})


class PiEngine:
    def __init__(self, gateway, model_settings, *, start_vm, guest_exec, clock, pi_bin: str = 'pi', context_window=200000):
        if not isinstance(context_window, int) or isinstance(context_window, bool) or context_window < 32000:
            raise ValueError('background context window must be at least 32000 tokens')
        self.gateway, self.model_settings = gateway, model_settings
        self.start_vm, self.guest_exec, self.clock = start_vm, guest_exec, clock
        self.pi_bin, self.context_window = pi_bin, context_window

    async def __call__(self, row, session, *, feedback, progress, trace):
        model, effort = self.model_settings.get()
        trace.provider, trace.model = 'pi', model
        await self.gateway.start()
        token, channel = self.gateway.open(source=row, current=session.current)
        channel.session = session
        run = PiRun(channel, progress, trace)
        reader = None
        try:
            vm = await self.start_vm(row)
            await self.guest_exec(
                vm,
                launch_script(
                    row['job_id'],
                    token,
                    effort,
                    self.gateway.port,
                    session.prompt,
                    pi_bin=self.pi_bin,
                    context_window=self.context_window,
                ),
            )
            await channel.wait(120)
            reader = asyncio.create_task(run.read())
            data = await run.request({'type': 'prompt', 'message': row['task']})
            if isinstance(data, dict) and data.get('disposition') == 'handled':
                run.settled.set()
            stopped = False
            while not run.settled.is_set():
                for message in await feedback():
                    await run.steer(message)
                if self.clock() >= row['deadline_at']:
                    stopped = True
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(run.abort(), ABORT_TIMEOUT)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(run.settled.wait(), STOP_GRACE)
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(run.settled.wait(), 2)
            if run.error and not stopped and not run.last_text.strip():
                raise RuntimeError(self.gateway.failure(token) or run.error or 'Pi 运行失败。')
            trace.model = getattr(channel, 'last_model', None) or trace.model
            text = run.last_text.strip() or f"Pi 没有留下文字报告，会话记录在 /work/.gaoji/jobs/job-{row['job_id']}/pi。"
            fallbacks = getattr(channel, 'fallbacks', ())
            if fallbacks:
                notes = '；'.join(f'{old} → {new}，原因：{reason}' for old, new, reason in fallbacks)
                text = f'{text}\n（后台模型降级：{notes}）'
            return f'{DEADLINE_NOTE}\n{text}' if stopped else text
        except asyncio.CancelledError:
            if not run.settled.is_set():
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(run.abort(), ABORT_TIMEOUT)
            raise
        finally:
            self.gateway.revoke(token)
            tasks = [task for task in (reader, *run.handlers) if task is not None]
            for task in tasks:
                task.cancel()
            await drain(asyncio.gather(*tasks, return_exceptions=True))
