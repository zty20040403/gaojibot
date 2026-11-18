"""Background jobs run by Pi (--print mode) in their own container systemd units."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re
import shlex
from pathlib import PurePosixPath

from .billing_context import drain

HOST = '10.0.2.2'
DEADLINE_NOTE = '[到点停了，下面是做到一半的]'
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
DEFAULT_EXTENSIONS = (
    '/etc/gaoji/pi/sandbox_pi_tools.ts',
    '/etc/gaoji/pi/sandbox_pi_agent.ts',
    '/etc/gaoji/pi/node_modules/@juicesharp/rpiv-todo/index.ts',
    '/etc/gaoji/pi/node_modules/pi-web-access/index.ts',
)
POLL_INTERVAL = 1.5
STOP_TIMEOUT = 35


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
    wanted = settings.background_upstream_profile.strip().lower()
    for profile in catalog.profiles:
        if (profile.name == wanted) if wanted else profile.provider == 'cliproxy':
            return profile
    raise RuntimeError('没有可用于后台任务的模型端点，请设置 AI_BACKGROUND_UPSTREAM_PROFILE。')


def notes_slug(workdir: str) -> str:
    raw = str(PurePosixPath('/' + (workdir or '/work').lstrip('/'))).lstrip('/')
    slug = re.sub(r'[^A-Za-z0-9]', '-', raw)[:120]
    return slug or 'root'


def pi_models_json(port: int, *, context_window: int = 200000) -> dict:
    return {'providers': {'gaoji': {
        'baseUrl': f'http://{HOST}:{port}/v1',
        'api': 'openai-responses',
        'apiKey': '$GAOJI_JOB_TOKEN',
        'authHeader': True,
        'compat': {'supportsLongCacheRetention': False, 'supportsMaxOutputTokens': False},
        'models': [{
            'id': 'gaoji-background',
            'name': 'gaoji-background',
            'reasoning': True,
            'thinkingLevelMap': {'xhigh': 'xhigh', 'max': 'max'},
            'input': ['text', 'image'],
            'contextWindow': context_window,
            'maxTokens': 32000,
        }],
    }}}


def pi_web_access_config() -> dict:
    """Keyless pi-web-access configuration: search stays on host-relayed Bocha."""
    return {
        'toolActivation': 'eager',
        'webSearch': {'enabled': False},
        'tools': {
            'webSearch': {'enabled': False},
            'sourceCheck': {'enabled': False},
            'fetchContent': {'enabled': True},
            'getSearchContent': {'enabled': True},
        },
    }


def pi_arguments(
    effort: str,
    conf: str,
    session_dir: str,
    extensions: tuple[str, ...] | str = DEFAULT_EXTENSIONS,
    *,
    session_id: str = '',
    title: str = '',
    fork_session: str = '',
) -> tuple[str, ...]:
    ext_list = (extensions,) if isinstance(extensions, str) else tuple(extensions)
    args = [
        '--print',
        '--no-approve',
        '--session-dir', session_dir,
    ]
    if session_id:
        args += ['--session-id', session_id]
    if title:
        args += ['--name', title]
    args += [
        '--provider', 'gaoji',
        '--model', 'gaoji-background',
        '--thinking', effort,
        '--append-system-prompt', f'{conf}/job-system.md',
    ]
    for ext in ext_list:
        args += ['--extension', ext]
    args += [
        '--no-extensions',
        '--no-mcp',
        '--no-skills',
        '--no-prompt-templates',
        '--no-themes',
    ]
    if fork_session:
        args += ['--fork', fork_session]
    return tuple(args)


_PREPARE_PY = """import json, os, re, sys
from pathlib import Path

conf = Path(sys.argv[1])
out_dir = Path(sys.argv[2])
meta = json.loads((conf / 'meta.json').read_text('utf-8'))
workdir = (meta.get('workdir') or '').strip()
title = (meta.get('title') or '').strip()
resume_id = meta.get('resume_job_id')
fork_path = ''

if resume_id is not None:
    jobs_root = Path('/work/.gaoji/jobs')
    candidates = sorted(jobs_root.glob(f'*_gaoji-job-{resume_id}.jsonl'))
    if not candidates:
        candidates = sorted((jobs_root / f'job-{resume_id}' / 'pi').glob('*.jsonl'))
    if not candidates:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / 'result.json').write_text(
            json.dumps({'text': '', 'error': f'找不到 job#{resume_id} 的历史会话记录，无法接着上次做。', 'stopped': False, 'usage': {}}, ensure_ascii=False),
            'utf-8',
        )
        raise SystemExit(1)
    fork_file = candidates[-1]
    fork_path = str(fork_file)
    try:
        lines = fork_file.read_text('utf-8', errors='replace').splitlines()
        if not workdir and lines:
            first = json.loads(lines[0])
            if isinstance(first, dict) and first.get('cwd'):
                workdir = str(first['cwd'])
        if not title:
            for line in reversed(lines):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get('type') == 'session_info' and rec.get('name'):
                    title = str(rec['name'])
                    break
    except OSError:
        pass

if not workdir:
    workdir = '/work'
elif not workdir.startswith('/'):
    workdir = '/work/' + workdir
workdir = os.path.normpath(workdir)
os.makedirs(workdir, exist_ok=True)
os.makedirs('/work/.gaoji/projects', exist_ok=True)

slug = re.sub(r'[^A-Za-z0-9]', '-', workdir.lstrip('/'))[:120] or 'root'
notes_file = Path(f'/work/.gaoji/projects/{slug}.md')
if notes_file.is_symlink():
    notes = '（没读到：它是个链接。）'
elif not notes_file.exists():
    notes = '（还没有，这次新建。）'
else:
    size = notes_file.stat().st_size
    if size > 1048576:
        notes = '（没读到：超过 1 MiB 了。）'
    elif size > 16384:
        chunk = notes_file.read_bytes()[:16384].decode('utf-8', errors='ignore')
        cut = chunk.rsplit('\\n', 1)[0] if '\\n' in chunk else chunk
        notes = cut + '\\n（笔记超过 16 KiB，后面的没加载：更新时精简一下。）'
    else:
        notes = notes_file.read_text('utf-8', errors='replace')

template = (conf / 'prompt-template.md').read_text('utf-8')
if '@NOTES@' in template:
    rendered = (
        template.replace('@WORKDIR@', workdir)
        .replace('@NOTES_FILE@', str(notes_file))
        .replace('@NOTES@', notes)
    )
else:
    rendered = (
        template
        + f'\\n\\n你在 {workdir} 下干活。目录笔记在 {notes_file}（16 KiB 以内，写报告前更新关键结论与命令）：\\n\\n[笔记]\\n{notes}'
    )
(conf / 'job-system.md').write_text(rendered, 'utf-8')
(conf / 'workdir').write_text(workdir, 'utf-8')
(conf / 'title').write_text(title or f"job-{meta['job_id']}", 'utf-8')
(conf / 'fork').write_text(fork_path, 'utf-8')
"""


_FINISH_PY = """import json, os, sys
from pathlib import Path
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
tmp = out / '.exit.json.tmp'
tmp.write_text(json.dumps({
    'service_result': os.environ.get('SERVICE_RESULT', ''),
    'exit_code': os.environ.get('EXIT_CODE', ''),
    'exit_status': os.environ.get('EXIT_STATUS', ''),
}), 'utf-8')
tmp.replace(out / 'exit.json')
"""


def launch_script(
    job_id: int,
    token: str,
    effort: str,
    port: int,
    prompt: str,
    *,
    task: str = '',
    title: str = '',
    workdir: str = '/work',
    resume_job_id: int | None = None,
    started_at: int | None = None,
    limit_sec: int = 21600,
    context_window: int = 200000,
    pi_bin: str = 'pi',
    extensions: tuple[str, ...] = DEFAULT_EXTENSIONS,
) -> str:
    if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0:
        raise ValueError('invalid background job id')
    encode = lambda data: base64.b64encode(data).decode()
    out_dir = f'/work/.gaoji/jobs/job-{job_id}'
    reports_dir = '/work/.gaoji/jobs'
    conf = f'/run/gaoji/jobs/job-{job_id}'
    unit = f'gaoji-job-{job_id}'
    models = json.dumps(pi_models_json(port, context_window=context_window), ensure_ascii=False).encode()
    settings = json.dumps({'steeringMode': 'all', 'retry': {'maxRetries': 8}}).encode()
    web_cfg = json.dumps(pi_web_access_config(), ensure_ascii=False).encode()
    meta = json.dumps({
        'job_id': job_id,
        'workdir': workdir,
        'title': title,
        'resume_job_id': resume_job_id,
    }, ensure_ascii=False).encode()
    base_args = pi_arguments(effort, conf, reports_dir, extensions, session_id=unit)
    quoted_base_args = ' '.join(shlex.quote(arg) for arg in (pi_bin, *base_args))
    limit_val = max(60, int(limit_sec))
    started_expr = str(int(started_at)) if started_at is not None else '$(date +%s)'
    return f'''set -eu
umask 077
mkdir -p {conf}/inbox {out_dir} {reports_dir} /work/.gaoji/projects
chmod 700 {conf}
rm -f {out_dir}/result.json {out_dir}/exit.json {conf}/stopped {conf}/asked {conf}/last_tool
printf '%s\\n' {shlex.quote(token)} > {conf}/token
printf '%s\\n' "{started_expr}" > {conf}/started
printf '%s\\n' {limit_val} > {conf}/limit
printf '0\\n' > {conf}/steered_count
printf %s {encode(models)} | base64 -d > {conf}/models.json
printf %s {encode(settings)} | base64 -d > {conf}/settings.json
printf %s {encode(web_cfg)} | base64 -d > {conf}/web-search.json
printf %s {encode(meta)} | base64 -d > {conf}/meta.json
printf %s {encode(prompt.encode())} | base64 -d > {conf}/prompt-template.md
printf %s {encode(task.encode())} | base64 -d > {conf}/task
printf %s {encode(_PREPARE_PY.encode())} | base64 -d > {conf}/prepare.py
printf %s {encode(_FINISH_PY.encode())} | base64 -d > {conf}/finish.py
cat <<'GAOJI_FINISH_EOF' > {conf}/finish.sh
#!/bin/sh
set -e
[ -r /etc/set-environment ] && . /etc/set-environment
set -u
export PATH="/run/wrappers/bin:/run/current-system/sw/bin:${{PATH:-/usr/bin:/bin}}"
exec python3 {conf}/finish.py {shlex.quote(out_dir)}
GAOJI_FINISH_EOF
chmod 700 {conf}/finish.sh
cat <<'GAOJI_JOB_EOF' > {conf}/run.sh
#!/bin/sh
set -e
[ -r /etc/set-environment ] && . /etc/set-environment
set -u
export PATH="/run/wrappers/bin:/run/current-system/sw/bin:${{PATH:-/usr/bin:/bin}}"
export HOME=/work
python3 {conf}/prepare.py {conf} {out_dir}
workdir=$(cat {conf}/workdir)
title=$(cat {conf}/title)
fork=$(cat {conf}/fork)
cd "$workdir"
export GAOJI_JOB_ID={job_id}
export GAOJI_JOB_UNIT={unit}
export GAOJI_JOB_DIR={conf}
export GAOJI_JOB_OUT_DIR={out_dir}
export GAOJI_JOB_REPORTS={reports_dir}
export GAOJI_JOB_TOKEN={shlex.quote(token)}
export GAOJI_HOST_URL={shlex.quote(f'http://{HOST}:{port}')}
export PI_CODING_AGENT_DIR={shlex.quote(conf)}
export PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0
if [ -n "$fork" ]; then
  exec {quoted_base_args} --name "$title" --fork "$fork" < {conf}/task
else
  exec {quoted_base_args} --name "$title" < {conf}/task
fi
GAOJI_JOB_EOF
chmod 700 {conf}/run.sh
if command -v systemd-run >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  systemctl stop {unit}.service 2>/dev/null || true
  systemctl reset-failed {unit}.service 2>/dev/null || true
  systemd-run --unit={unit} --collect --quiet --no-block \\
    --service-type=exec \\
    --property=RuntimeMaxSec={limit_val} \\
    --property=KillSignal=SIGUSR2 \\
    --property=KillMode=mixed \\
    --property=TimeoutStopSec=30 \\
    --property=StandardOutput=append:{out_dir}/stdout.log \\
    --property=StandardError=append:{out_dir}/agent.err \\
    --property=ExecStopPost={conf}/finish.sh \\
    {conf}/run.sh
else
  cd /work
  ( {conf}/run.sh >{out_dir}/stdout.log 2>{out_dir}/agent.err </dev/null; code=$?; SERVICE_RESULT=$([ "$code" = 0 ] && echo success || echo exit-code) EXIT_CODE=exited EXIT_STATUS=$code {conf}/finish.sh ) >/dev/null 2>&1 &
fi
'''


def inspect_script(job_id: int) -> str:
    conf = f'/run/gaoji/jobs/job-{job_id}'
    out_dir = f'/work/.gaoji/jobs/job-{job_id}'
    unit = f'gaoji-job-{job_id}.service'
    code = f"""import json, subprocess
from pathlib import Path
conf, out = Path({conf!r}), Path({out_dir!r})
def read_text(p):
    try: return p.read_text('utf-8', errors='replace').strip()
    except OSError: return ''
def read_json(p):
    try: return json.loads(p.read_text('utf-8'))
    except (OSError, ValueError): return None
active = False
try:
    st = subprocess.run(['systemctl', 'is-active', {unit!r}], capture_output=True, text=True, timeout=5).stdout.strip()
    active = st in ('active', 'activating', 'deactivating')
except Exception:
    pass
err_tail = ''
err_file = out / 'agent.err'
if err_file.is_file():
    try: err_tail = err_file.read_bytes()[-1000:].decode('utf-8', errors='replace').strip()
    except OSError: pass
steered_raw = read_text(conf / 'steered_count')
print(json.dumps({{
    'active': active,
    'token': read_text(conf / 'token'),
    'steered': int(steered_raw) if steered_raw.isdigit() else 0,
    'last_tool': read_text(conf / 'last_tool'),
    'result': read_json(out / 'result.json'),
    'exit': read_json(out / 'exit.json'),
    'err_tail': err_tail,
}}, ensure_ascii=False))
"""
    return f"python3 -c {shlex.quote(code)}"


def steer_script(job_id: int, messages: list[str], next_count: int) -> str:
    conf = f'/run/gaoji/jobs/job-{job_id}'
    payload = base64.b64encode(json.dumps(messages, ensure_ascii=False).encode()).decode()
    code = f"""import base64, json, time
from pathlib import Path
conf = Path({conf!r})
inbox = conf / 'inbox'
inbox.mkdir(parents=True, exist_ok=True)
msgs = json.loads(base64.b64decode({payload!r}).decode('utf-8'))
base = int(time.time() * 1000)
for idx, text in enumerate(msgs):
    name = f'{{base + idx:015d}}-{{idx:04d}}'
    tmp = inbox / f'.tmp-{{name}}'
    tmp.write_text(text + '\\n', 'utf-8')
    tmp.replace(inbox / name)
(conf / 'steered_count').write_text(str({int(next_count)}), 'utf-8')
"""
    return f"python3 -c {shlex.quote(code)}"


def stop_script(job_id: int) -> str:
    unit = f'gaoji-job-{job_id}.service'
    return f"if command -v systemctl >/dev/null 2>&1; then systemctl stop {shlex.quote(unit)} 2>/dev/null || true; fi"


class PiEngine:
    def __init__(
        self,
        gateway,
        model_settings,
        *,
        start_vm,
        guest_exec,
        clock,
        pi_bin: str = 'pi',
        context_window: int = 200000,
        extensions: tuple[str, ...] = DEFAULT_EXTENSIONS,
        poll_interval: float = POLL_INTERVAL,
    ):
        if not isinstance(context_window, int) or isinstance(context_window, bool) or context_window < 32000:
            raise ValueError('background context window must be at least 32000 tokens')
        self.gateway, self.model_settings = gateway, model_settings
        self.start_vm, self.guest_exec, self.clock = start_vm, guest_exec, clock
        self.pi_bin, self.context_window = pi_bin, context_window
        self.extensions = tuple(extensions)
        self.poll_interval = poll_interval

    async def _start_gateway(self, deadline):
        remaining = max(0, deadline - self.clock())
        if not remaining:
            return False
        task = asyncio.create_task(self.gateway.start())
        try:
            done, _ = await asyncio.wait({task}, timeout=remaining)
            if task in done:
                await task
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

    async def _inspect(self, vm: str, job_id: int) -> dict:
        raw = await self.guest_exec(vm, inspect_script(job_id))
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', errors='replace')
        if isinstance(raw, str) and raw.strip():
            line = raw.strip().splitlines()[-1]
            with contextlib.suppress(ValueError):
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    return parsed
        return {
            'active': False,
            'token': '',
            'steered': 0,
            'last_tool': '',
            'result': None,
            'exit': None,
            'err_tail': '',
        }

    @staticmethod
    def _apply_usage(trace, result: dict | None):
        if not isinstance(result, dict):
            return
        usage = result.get('usage')
        if not isinstance(usage, dict):
            return
        trace.input_tokens = int(usage.get('input') or 0)
        trace.output_tokens = int(usage.get('output') or 0)
        trace.cached_tokens = int(usage.get('cached') or 0)
        trace.total_tokens = int(usage.get('total') or 0)

    async def __call__(self, row, session, *, feedback, progress, trace, is_cancelled=None):
        model, effort = self.model_settings.get()
        trace.provider, trace.model = 'pi', model
        if not await self._start_gateway(row['deadline_at']):
            return f'{DEADLINE_NOTE}\nPi 未启动，未创建会话记录。'

        job_id = int(row['job_id'])
        token = None
        channel = None
        vm = None
        stopping = False
        stop_task = None
        seen_tool = ''
        delivered_steers = 0
        final_state: dict = {}

        class DeadlineReached(Exception):
            pass

        def ensure_running():
            if stopping:
                raise asyncio.CancelledError
            if self.clock() >= row['deadline_at']:
                raise DeadlineReached

        async def stop_unit():
            nonlocal stop_task
            if vm is None:
                return
            if stop_task is None:
                async def _do_stop():
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(self.guest_exec(vm, stop_script(job_id)), STOP_TIMEOUT)
                stop_task = asyncio.create_task(_do_stop())
            await drain(stop_task)

        async def sync_steers():
            nonlocal delivered_steers
            ensure_running()
            fresh = await feedback()
            ensure_running()
            if not fresh:
                return
            delivered_steers += len(fresh)
            await self.guest_exec(vm, steer_script(job_id, list(fresh), delivered_steers))
            ensure_running()

        async def drive():
            nonlocal vm, token, channel, seen_tool, delivered_steers, final_state
            ensure_running()
            vm = await self.start_vm(row)
            ensure_running()

            existing = await self._inspect(vm, job_id)
            ensure_running()

            if row.get('started_at') and (existing.get('result') is not None or existing.get('exit') is not None):
                # Completed while the host was restarting.
                final_state = existing
                return False

            if existing.get('active') and existing.get('token'):
                # Reconnect to an already-running container unit across host restart.
                token, channel = self.gateway.open(source=row, current=session.current, token=existing['token'])
                channel.session = session
                delivered_steers = int(existing.get('steered') or 0)
                if delivered_steers > 0:
                    # Advance host feedback cursor past steers already delivered to inbox.
                    already = await feedback()
                    if len(already) > delivered_steers:
                        extra = already[delivered_steers:]
                        delivered_steers = len(already)
                        await self.guest_exec(vm, steer_script(job_id, list(extra), delivered_steers))
            else:
                token, channel = self.gateway.open(source=row, current=session.current)
                channel.session = session
                now_ts = int(self.clock())
                limit_sec = max(60, int(row['deadline_at'] - now_ts))
                await self.guest_exec(
                    vm,
                    launch_script(
                        job_id,
                        token,
                        effort,
                        self.gateway.port,
                        session.prompt,
                        task=str(row.get('task') or ''),
                        title=str(row.get('title') or ''),
                        workdir=str(row.get('workdir') or '/work'),
                        resume_job_id=row.get('resume_job_id'),
                        started_at=now_ts,
                        limit_sec=limit_sec,
                        pi_bin=self.pi_bin,
                        context_window=self.context_window,
                        extensions=self.extensions,
                    ),
                )

            ensure_running()
            while True:
                await sync_steers()
                state = await self._inspect(vm, job_id)
                ensure_running()
                tool_name = state.get('last_tool') or ''
                if tool_name and tool_name != seen_tool:
                    seen_tool = tool_name
                    await progress(tool_name)
                if state.get('exit') is not None or (not state.get('active') and state.get('result') is not None):
                    final_state = state
                    return False
                if not state.get('active') and state.get('exit') is None and state.get('result') is None:
                    # Wait briefly for ExecStopPost to flush exit.json.
                    await asyncio.sleep(min(0.2, self.poll_interval))
                    recheck = await self._inspect(vm, job_id)
                    if not recheck.get('active'):
                        final_state = recheck
                        return False
                await asyncio.sleep(min(self.poll_interval, max(0.01, row['deadline_at'] - self.clock())))

        work = asyncio.create_task(drive())
        try:
            done, _ = await asyncio.wait({work}, timeout=max(0, row['deadline_at'] - self.clock()))
            if work not in done:
                stopping = True
                work.cancel()
                await stop_unit()
                await drain(asyncio.gather(work, return_exceptions=True))
                if vm is not None:
                    with contextlib.suppress(Exception):
                        final_state = await self._inspect(vm, job_id)
                stopped_by_deadline = True
            else:
                try:
                    stopped_by_deadline = await work
                except DeadlineReached:
                    stopping = True
                    await stop_unit()
                    if vm is not None:
                        with contextlib.suppress(Exception):
                            final_state = await self._inspect(vm, job_id)
                    stopped_by_deadline = True
        except asyncio.CancelledError:
            stopping = True
            work.cancel()
            user_cancelled = True
            if is_cancelled is not None:
                with contextlib.suppress(Exception):
                    user_cancelled = bool(await drain(asyncio.create_task(is_cancelled())))
            if user_cancelled:
                await stop_unit()
            if token is not None:
                self.gateway.revoke(token)
            await drain(asyncio.gather(work, return_exceptions=True))
            raise
        finally:
            if token is not None:
                relay_error = self.gateway.failure(token)
                self.gateway.revoke(token)
            else:
                relay_error = ''

        if channel is not None and channel.last_model:
            trace.model = channel.last_model
        fallback_note = ''
        if channel is not None and channel.fallbacks:
            steps = '；'.join(f'{src} → {dst}，原因：{why}' for src, dst, why in channel.fallbacks)
            fallback_note = f'\n\n（后台模型降级：{steps}）'

        result = final_state.get('result') if isinstance(final_state, dict) else None
        exit_info = final_state.get('exit') if isinstance(final_state, dict) else None
        err_tail = (final_state.get('err_tail') or '') if isinstance(final_state, dict) else ''
        self._apply_usage(trace, result)
        last_text = (result.get('text') or '').strip() if isinstance(result, dict) else ''
        pi_error = (result.get('error') or '').strip() if isinstance(result, dict) else ''

        if stopped_by_deadline or (isinstance(exit_info, dict) and exit_info.get('service_result') == 'timeout'):
            body = last_text or 'Pi 未产出结果。'
            return f'{DEADLINE_NOTE}\n{body}{fallback_note}'

        if pi_error or relay_error:
            if last_text and not relay_error:
                return last_text + fallback_note
            raise RuntimeError(relay_error or pi_error)

        if isinstance(exit_info, dict):
            svc_res = exit_info.get('service_result') or ''
            exit_status = str(exit_info.get('exit_status') or '')
            if svc_res not in ('', 'success') or (exit_status not in ('', '0') and not last_text):
                detail = err_tail or f'{svc_res} (status={exit_status})'
                if result is None:
                    raise RuntimeError(f'后台进程没能启动或异常退出：{detail}')
                if not last_text:
                    raise RuntimeError(f'Pi 异常退出：{detail}')

        if not last_text:
            if result is None and exit_info is None:
                raise RuntimeError('后台进程没能启动：容器单元未生成运行结果。')
            raise RuntimeError(relay_error or err_tail or 'Pi 已结束但没有返回报告。')
        return last_text + fallback_note
