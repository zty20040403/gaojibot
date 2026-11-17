"""Background jobs run by Pi in the conversation container over its RPC protocol."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import shlex

from .billing_context import drain

HOST = '10.0.2.2'
DEADLINE_NOTE = '[到点停了，下面是做到一半的]'
EFFORTS = ('low', 'medium', 'high', 'xhigh')
ABORT_TIMEOUT = 5
STOP_GRACE = 10


def parse_model_chain(value: str) -> list[str]:
    text = value.strip()
    if not text:
        raise ValueError('模型名不能为空，最多 8 个，每个不超过 100 字，推理强度只能是 ' + '/'.join(EFFORTS))
    parts = [item.strip() for item in text.split(',')]
    if any(not item or len(item) > 100 for item in parts) or len(parts) > 8:
        raise ValueError('模型名不能为空，最多 8 个，每个不超过 100 字，推理强度只能是 ' + '/'.join(EFFORTS))
    return parts


class BackgroundModelSettings:
    """The background model chain and effort, changed from the console."""

    def __init__(self, preferences, settings):
        self.preferences, self.settings = preferences, settings

    def chain(self) -> list[str]:
        raw = self.preferences.get_explicit('background:models') or self.preferences.get_explicit('background:model')
        if raw:
            return parse_model_chain(raw)
        models = getattr(self.settings, 'background_models', None)
        if models:
            return list(models)
        return [self.settings.background_model]

    def get(self):
        effort = self.preferences.get_explicit('background:effort') or self.settings.background_effort
        return self.chain()[0], effort

    def set(self, model, effort):
        models = parse_model_chain(model)
        if effort not in EFFORTS:
            raise ValueError('模型名不能为空，最多 8 个，每个不超过 100 字，推理强度只能是 ' + '/'.join(EFFORTS))
        self.preferences.set('background:models', ', '.join(models))
        self.preferences.set('background:model', models[0])
        self.preferences.set('background:effort', effort)


def upstream_profile(catalog, settings):
    """The profile whose endpoint and key relay background calls (cliproxy by default)."""
    wanted = settings.background_upstream_profile.strip().lower()  # the catalog lower-cases names
    for profile in catalog.profiles:
        if (profile.name == wanted) if wanted else profile.provider == 'cliproxy':
            return profile
    raise RuntimeError('没有可用于后台任务的模型端点，请设置 AI_BACKGROUND_UPSTREAM_PROFILE。')


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

    async def _start_gateway(self, deadline):
        remaining = max(0, deadline - self.clock())
        if not remaining:
            return False
        task = asyncio.create_task(self.gateway.start())
        try:
            done, _ = await asyncio.wait({task}, timeout=remaining)
            if task in done:
                await task  # An unrelated startup error is not a deadline.
                return self.clock() < deadline
            if not task.cancelling():
                task.cancel()
            await drain(asyncio.gather(task, return_exceptions=True))
            return False
        except asyncio.CancelledError:
            if not task.done() and not task.cancelling():
                task.cancel()
            await drain(asyncio.gather(task, return_exceptions=True))
            raise

    async def __call__(self, row, session, *, feedback, progress, trace):
        model, effort = self.model_settings.get()
        trace.provider, trace.model = 'pi', model
        if not await self._start_gateway(row['deadline_at']):
            return f'{DEADLINE_NOTE}\nPi 未启动，未创建会话记录。'
        token, channel = self.gateway.open(source=row, current=session.current)
        channel.session = session
        run = PiRun(channel, progress, trace)
        reader = None
        stopping = False
        stop_task = None

        class DeadlineReached(Exception):
            pass

        def ensure_running():
            # A cancelled startup or database call may take time to drain; it
            # must not start Pi or send a command after the deadline meanwhile.
            if stopping:
                raise asyncio.CancelledError
            if self.clock() >= row['deadline_at']:
                raise DeadlineReached

        async def drive():
            nonlocal reader
            ensure_running()
            vm = await self.start_vm(row)
            ensure_running()
            await self.guest_exec(
                vm,
                launch_script(
                    row['job_id'], token, effort, self.gateway.port, session.prompt,
                    pi_bin=self.pi_bin, context_window=self.context_window,
                ),
            )
            ensure_running()
            await channel.wait(120)
            ensure_running()
            reader = asyncio.create_task(run.read())
            data = await run.request({'type': 'prompt', 'message': row['task']})
            ensure_running()
            if isinstance(data, dict) and data.get('disposition') == 'handled':
                run.settled.set()
            while not run.settled.is_set():
                ensure_running()
                for message in await feedback():
                    ensure_running()
                    await run.steer(message)
                    ensure_running()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(run.settled.wait(), 2)
            return False

        async def stop_once():
            nonlocal stop_task
            if reader is None or run.settled.is_set():
                return
            if stop_task is None:
                async def stop():
                    with contextlib.suppress(TimeoutError, OSError, EOFError):
                        await asyncio.wait_for(run.abort(), ABORT_TIMEOUT)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(run.settled.wait(), STOP_GRACE)
                stop_task = asyncio.create_task(stop())
            await drain(stop_task)

        work = asyncio.create_task(drive())
        try:
            # Do not await a cancelled drive before sending abort: an in-flight
            # DB or VM operation may need to drain its own cleanup first.
            done, _ = await asyncio.wait({work}, timeout=max(0, row['deadline_at'] - self.clock()))
            try:
                stopped = await work if work in done else True
            except DeadlineReached:
                stopped = True
            if stopped:
                stopping = True
                if not work.done() and not work.cancelling():
                    work.cancel()
                await stop_once()
            if run.error and not stopped and not run.last_text.strip():
                raise RuntimeError(self.gateway.failure(token) or run.error or 'Pi 运行失败。')
            trace.model = getattr(channel, 'last_model', None) or trace.model
            text = run.last_text.strip() or f"Pi 没有留下文字报告；如已创建会话，记录位于 /work/.gaoji/jobs/job-{row['job_id']}/pi。"
            fallbacks = getattr(channel, 'fallbacks', ())
            if fallbacks:
                notes = '；'.join(f'{old} → {new}，原因：{reason}' for old, new, reason in fallbacks)
                text = f'{text}\n（后台模型降级：{notes}）'
            return f'{DEADLINE_NOTE}\n{text}' if stopped else text
        except asyncio.CancelledError:
            stopping = True
            if not work.done() and not work.cancelling():
                work.cancel()
            await stop_once()
            raise
        finally:
            self.gateway.revoke(token)
            tasks = [task for task in (work, reader, *run.handlers) if task is not None]
            for task in tasks:
                if not task.done() and not task.cancelling():
                    task.cancel()
            await drain(asyncio.gather(*tasks, return_exceptions=True))
