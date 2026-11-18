"""Lightweight container systemd unit manager (`gaoji units` and `gaoji wake`), stdlib-only."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time

UNIT_EXTS = ('.service', '.socket', '.timer', '.path', '.target')
CMD_UNIT_RE = re.compile(r'^gaoji-cmd-[a-z0-9-]+$')


def _format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f'{seconds} 秒'
    if seconds < 3600:
        return f'{seconds // 60} 分钟'
    return f'{seconds // 3600} 小时 {(seconds % 3600) // 60} 分钟'


def _run(argv: list[str], *, timeout: int = 30, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=check)


def apply_units(
    client,
    *,
    boot: bool = False,
    systemd_dir: Path = Path('/work/.gaoji/systemd'),
    runtime_dir: Path = Path('/run/systemd/system'),
    installed_dir: Path = Path('/run/gaoji/units_installed'),
    etc_systemd: Path = Path('/etc/systemd/system'),
    lock_path: Path = Path('/run/gaoji/units.lock'),
    run_cmd=_run,
) -> tuple[int, dict]:
    systemd_dir.mkdir(parents=True, exist_ok=True)
    installed_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open('w') as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        skipped: list[dict[str, str]] = []
        removed: list[str] = []
        wanted: list[str] = []
        keep: set[str] = set()

        for entry in sorted(systemd_dir.iterdir(), key=lambda p: p.name):
            name = entry.name
            if not name.endswith(UNIT_EXTS):
                continue
            if entry.is_symlink():
                skipped.append({'name': name, 'why': '符号链接不予安装，请直接放普通单元文件'})
                continue
            if not entry.is_file():
                continue
            if (etc_systemd / name).exists():
                skipped.append({'name': name, 'why': '与系统内置单元重名，请换一个名字'})
                continue
            wanted.append(name)
            keep.add(name)

        for old in sorted(installed_dir.iterdir(), key=lambda p: p.name):
            if not old.is_file() or old.name in keep:
                continue
            name = old.name
            run_cmd(['systemctl', 'disable', '--runtime', '--now', '--no-block', '--quiet', name])
            (runtime_dir / name).unlink(missing_ok=True)
            shutil.rmtree(runtime_dir / f'{name}.d', ignore_errors=True)
            old.unlink(missing_ok=True)
            removed.append(name)

        changed: list[str] = []
        for name in wanted:
            src = systemd_dir / name
            inst = installed_dir / name
            data = src.read_bytes()
            if inst.is_file() and inst.read_bytes() == data:
                continue
            if inst.is_file():
                run_cmd(['systemctl', 'disable', '--runtime', '--quiet', name])
            dst = runtime_dir / name
            dst.write_bytes(data)
            dst.chmod(0o644)
            if name.endswith('.service'):
                dropin = runtime_dir / f'{name}.d'
                dropin.mkdir(parents=True, exist_ok=True)
                (dropin / 'gaoji.conf').write_text('[Service]\nSlice=gaoji-units.slice\n', encoding='utf-8')
            inst.write_bytes(data)
            changed.append(name)

        run_cmd(['systemctl', 'daemon-reload'])
        for name in changed:
            run_cmd(['systemctl', 'try-restart', '--no-block', name])
        for name in wanted:
            text = (systemd_dir / name).read_text('utf-8', errors='replace')
            if any(line.strip() == '[Install]' for line in text.splitlines()):
                run_cmd(['systemctl', 'enable', '--runtime', '--now', '--no-block', '--quiet', name])

        for _ in range(50):
            busy = False
            for name in wanted:
                state = run_cmd(['systemctl', 'is-active', name]).stdout.strip()
                if state in {'activating', 'deactivating', 'reloading', 'refreshing'}:
                    busy = True
                    break
            if not busy:
                break
            time.sleep(0.1)

        units_out: list[dict[str, str]] = []
        for name in wanted:
            state = run_cmd(['systemctl', 'is-active', name]).stdout.strip() or 'inactive'
            item = {'name': name, 'state': state}
            if state == 'failed':
                log = run_cmd(['journalctl', '-u', name, '-n', '10', '--no-pager', '-o', 'cat']).stdout.strip()
                item['log'] = log
            units_out.append(item)

    said = ''
    try:
        st, resp = client.call_tool('units_keepalive', {'units': len(wanted)})
        if 200 <= st < 300 and isinstance(resp, dict):
            said = str(resp.get('said') or '')
    except Exception:
        if not boot:
            pass

    payload: dict[str, object] = {
        'ok': True,
        'units': units_out,
        'skipped': skipped,
        'removed': removed,
    }
    if said:
        payload['said'] = said
    return 200, payload


def start_command(
    *,
    command: str,
    title: str = '',
    workdir: str = '/work',
    limit: int = 21600,
    unit: str = '',
    run_root: Path = Path('/run/gaoji/units_run'),
    out_dir: Path = Path('/work/.gaoji/out'),
    run_cmd=_run,
) -> tuple[int, dict]:
    cmd = (command or '').strip()
    if not cmd:
        return 400, {'ok': False, 'error': '没有要执行的命令'}
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 86400:
        return 400, {'ok': False, 'error': 'limit 必须是 1..86400 秒'}
    unit_name = (unit or '').strip() or f'gaoji-cmd-{secrets.token_hex(6)}'
    if not CMD_UNIT_RE.fullmatch(unit_name):
        return 400, {'ok': False, 'error': f'单元名必须是 gaoji-cmd-... 格式：{unit_name}'}
    unit_dir = run_root / unit_name
    if unit_dir.exists():
        return 409, {'ok': False, 'error': f'单元 {unit_name} 已存在'}

    wdir = (workdir or '').strip() or '/work'
    if not wdir.startswith('/'):
        wdir = '/work/' + wdir
    wdir_path = Path(os.path.normpath(wdir))
    if not wdir_path.is_dir():
        return 400, {'ok': False, 'error': f'工作目录不存在：{wdir_path}'}

    if not title.strip():
        first_line = next((ln.strip() for ln in cmd.splitlines() if ln.strip()), cmd)
        title = '$ ' + (first_line[:60] + '…' if len(first_line) > 60 else first_line)

    unit_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f'{unit_name}.txt'
    started = int(time.time())
    (unit_dir / 'meta.json').write_text(
        json.dumps({
            'unit': unit_name,
            'title': title,
            'workdir': str(wdir_path),
            'limit': limit,
            'started': started,
        }, ensure_ascii=False),
        encoding='utf-8',
    )

    bash_bin = '/run/current-system/sw/bin/bash' if Path('/run/current-system/sw/bin/bash').exists() else '/bin/sh'
    gaoji_bin = '/run/current-system/sw/bin/gaoji'
    argv = [
        'systemd-run',
        '--no-block',
        '--quiet',
        '--collect',
        f'--unit={unit_name}',
        '--slice=gaoji-jobs.slice',
        f'--description={title}',
        '--expand-environment=no',
        f'--property=WorkingDirectory=-{wdir_path}',
        f'--property=RuntimeMaxSec={limit}',
        f'--property=StandardOutput=append:{out_file}',
        f'--property=StandardError=append:{out_file}',
        f'--property=ExecStopPost={gaoji_bin} units ended {unit_name}',
        '--',
        bash_bin,
        '-lc' if bash_bin.endswith('bash') else '-c',
        cmd,
    ]
    res = run_cmd(argv)
    if res.returncode != 0:
        shutil.rmtree(unit_dir, ignore_errors=True)
        return 502, {'ok': False, 'error': res.stderr.strip() or 'systemd-run 启动失败'}

    return 200, {
        'ok': True,
        'unit': unit_name,
        'limit': limit,
        'output_file': str(out_file),
        'said': (
            f'已在后台启动 {unit_name}（上限 {_format_duration(limit)}）。'
            f'结束时会自动通知前台 [{unit_name} 完成]，请勿循环等待；'
            f'实时输出写入 {out_file}，如需提前停止可运行 gaoji units stop {unit_name}。'
        ),
    }


def list_units(
    *,
    run_root: Path = Path('/run/gaoji/units_run'),
    jobs_root: Path = Path('/run/gaoji/jobs'),
    installed_dir: Path = Path('/run/gaoji/units_installed'),
    run_cmd=_run,
) -> tuple[int, dict]:
    now = int(time.time())
    items: list[dict[str, object]] = []
    raw = run_cmd([
        'systemctl', 'list-units', '--no-legend', '--plain',
        '--state=active,activating,deactivating',
        'gaoji-cmd-*.service', 'gaoji-job-*.service',
    ]).stdout
    for line in raw.splitlines():
        parts = line.split()
        if not parts:
            continue
        svc = parts[0]
        name = svc.removesuffix('.service')
        if name.startswith('gaoji-cmd-'):
            meta_file = run_root / name / 'meta.json'
            if meta_file.is_file():
                try:
                    meta = json.loads(meta_file.read_text('utf-8'))
                    items.append({
                        'name': name,
                        'kind': 'job',
                        'title': meta.get('title', ''),
                        'seconds': max(0, now - int(meta.get('started', now))),
                    })
                    continue
                except (OSError, ValueError):
                    pass
            items.append({'name': name, 'kind': 'job'})
        elif name.startswith('gaoji-job-'):
            job_num = name.removeprefix('gaoji-job-')
            jdir = jobs_root / f'job-{job_num}'
            title = (jdir / 'title').read_text('utf-8', errors='replace').strip() if (jdir / 'title').is_file() else ''
            started_s = (jdir / 'started').read_text('utf-8', errors='replace').strip() if (jdir / 'started').is_file() else ''
            entry: dict[str, object] = {'name': name, 'kind': 'agent'}
            if started_s.isdigit():
                entry['title'] = title
                entry['seconds'] = max(0, now - int(started_s))
            items.append(entry)

    if installed_dir.is_dir():
        for f in sorted(installed_dir.iterdir(), key=lambda p: p.name):
            if not f.is_file():
                continue
            st = run_cmd(['systemctl', 'is-active', f.name]).stdout.strip() or 'inactive'
            items.append({'name': f.name, 'kind': 'unit', 'state': st})

    return 200, {'ok': True, 'units': items}


def stop_units(
    target: str,
    *,
    run_root: Path = Path('/run/gaoji/units_run'),
    run_cmd=_run,
) -> tuple[int, dict]:
    if target == '--all':
        raw = run_cmd([
            'systemctl', 'list-units', '--no-legend', '--plain',
            '--state=active,activating', 'gaoji-cmd-*.service',
        ]).stdout
        names = [ln.split()[0].removesuffix('.service') for ln in raw.splitlines() if ln.split()]
    else:
        name = target.removesuffix('.service').strip()
        if not name:
            return 400, {'ok': False, 'error': '请指定要停止的单元名或 --all'}
        names = [name]

    for name in names:
        udir = run_root / name
        if udir.is_dir():
            (udir / 'stopped').write_text('1', encoding='utf-8')
        run_cmd(['systemctl', 'stop', f'{name}.service'])
    return 200, {'ok': True, 'stopped': names}


def ended_hook(
    unit: str,
    *,
    run_root: Path = Path('/run/gaoji/units_run'),
    run_cmd=_run,
) -> tuple[int, dict]:
    name = unit.removesuffix('.service')
    udir = run_root / name
    if not udir.is_dir():
        return 200, {'ok': True}
    svc_result = os.environ.get('SERVICE_RESULT', '')
    exit_code = os.environ.get('EXIT_CODE', '')
    exit_status = os.environ.get('EXIT_STATUS', '')
    tmp = udir / '.result.tmp'
    tmp.write_text(
        json.dumps({
            'service_result': svc_result,
            'exit_code': exit_code,
            'exit_status': exit_status,
            'ended': int(time.time()),
        }),
        encoding='utf-8',
    )
    tmp.replace(udir / 'result.json')
    if (udir / 'stopped').exists() and svc_result != 'timeout':
        shutil.rmtree(udir, ignore_errors=True)
        return 200, {'ok': True, 'stopped': True}
    run_cmd(['systemctl', 'start', '--no-block', f'gaoji-notify@{name}.service'])
    return 200, {'ok': True, 'notifying': True}


def notify_hook(
    client,
    unit: str,
    *,
    run_root: Path = Path('/run/gaoji/units_run'),
    out_dir: Path = Path('/work/.gaoji/out'),
) -> tuple[int, dict]:
    name = unit.removesuffix('.service')
    udir = run_root / name
    res_file = udir / 'result.json'
    meta_file = udir / 'meta.json'
    if not res_file.is_file() or not meta_file.is_file():
        return 200, {'ok': True}
    res = json.loads(res_file.read_text('utf-8'))
    meta = json.loads(meta_file.read_text('utf-8'))
    svc_result = res.get('service_result', '')
    exit_code = res.get('exit_code', '')
    exit_status = res.get('exit_status', '')
    title = meta.get('title', name)
    limit = int(meta.get('limit', 21600))
    ran = _format_duration(int(res.get('ended', time.time())) - int(meta.get('started', time.time())))

    head = f'[{name} 完成] {title}'
    if svc_result in {'success', 'exit-code'}:
        line = f'退出码 {exit_status}，运行 {ran}'
        if exit_code == 'killed':
            line = f'收到信号 SIG{exit_status} 结束，运行 {ran}'
    elif svc_result == 'timeout':
        head = f'[{name} 到点了，下面是做到一半的] {title}'
        line = f'超过时限 {_format_duration(limit)} 已自动停止'
    elif svc_result in {'signal', 'core-dump', 'oom-kill'}:
        head = f'[{name} 失败] {title}'
        line = f'因 {svc_result} ({exit_status}) 终止，运行 {ran}'
    else:
        head = f'[{name} 失败] {title}：{svc_result}'
        line = f'未能正常运行：{exit_code} {exit_status}'

    out_file = out_dir / f'{name}.txt'
    tail_text = ''
    total_bytes = 0
    if out_file.is_file():
        total_bytes = out_file.stat().st_size
        with out_file.open('rb') as f:
            if total_bytes > 12000:
                f.seek(total_bytes - 12000)
            tail_text = f.read().decode('utf-8', errors='replace').rstrip('\n')
        if total_bytes > 12000 or len(tail_text) > 3000:
            tail_text = '…' + tail_text[-3000:]

    parts = [head, line]
    if total_bytes == 0:
        parts.append('(无输出)')
    else:
        parts.append(tail_text)
        parts.append(f'(完整输出: {out_file})')
    notice = '\n'.join(parts)

    status, value = client.call_tool('units_wake', {'text': notice, 'unit': name})
    if 200 <= status < 300:
        shutil.rmtree(udir, ignore_errors=True)
    return status, value
