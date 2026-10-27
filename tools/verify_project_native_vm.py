"""Boot disposable Ubuntu/NixOS guests for M0, without SSH or host mounts.

Images and the read-only fixture ISO must be prepared explicitly. Ubuntu uses
QEMU NAT only for cloud-init's public package installation; NixOS is offline.
Neither guest gets credentials, forwarded ports or any production directory.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

from verify_project_guest_discard import Console


def wait_shell(console, timeout, log):
    deadline = time.monotonic() + timeout
    last_log = 0.0
    while time.monotonic() < deadline:
        recent = re.sub(rb'\x1b\[[0-?]*[ -/]*[@-~]', b'', bytes(console.output[-1000:]))
        if re.search(rb'(?:root@[^\r\n]+|\[root@[^\r\n]+\])[^\r\n]*# ',
                     recent):
            return
        if time.monotonic() - last_log >= 5:
            log.write_text(console.output.decode(errors='replace'))
            last_log = time.monotonic()
        for key, _ in console.reader.select(timeout=0.2):
            chunk = os.read(key.fileobj.fileno(), 65536)
            if not chunk:
                raise RuntimeError('Guest exited: ' + console.output[-4000:].decode(errors='replace'))
            console.output.extend(chunk)
    raise TimeoutError('Native guest boot deadline: ' + console.output[-4000:].decode(errors='replace'))


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    process.stdin.close()
    process.stdout.close()


def run(args):
    report = {'kind': args.kind, 'acceleration': args.accel, 'network':
              'QEMU NAT, no forwarded ports' if args.kind == 'ubuntu' else 'none'}
    logs = []
    with tempfile.TemporaryDirectory(prefix='gaoji-m0-native-') as temporary:
        root = Path(temporary) / 'root.qcow2'
        base = args.assets / ('noble-server-cloudimg-arm64.img' if args.kind == 'ubuntu'
                              else 'root.qcow2')
        subprocess.run([args.qemu_img, 'create', '-f', 'qcow2', '-F', 'qcow2',
                        '-b', str(base), str(root), '6G'], check=True, capture_output=True)
        cmd = [args.qemu, '-machine', 'virt', '-accel', args.accel, '-cpu',
               'host' if args.accel == 'hvf' else 'max', '-m', '2048', '-smp', '2',
               '-nographic', '-no-reboot',
               '-drive', f'file={root},format=qcow2,if=none,id=root,discard=unmap',
               '-device', 'virtio-blk-pci,drive=root,addr=0x1,serial=m0-root',
               '-drive', f'file={args.fixtures},format=raw,if=none,id=fixtures,readonly=on',
               '-device', 'virtio-blk-pci,drive=fixtures,addr=0x3,serial=m0-fixtures']
        if args.kind == 'ubuntu':
            variables = Path(temporary) / 'vars.fd'
            shutil.copyfile(args.firmware / 'edk2-arm-vars.fd', variables)
            cmd += ['-drive', f'file={args.firmware}/edk2-aarch64-code.fd,if=pflash,format=raw,readonly=on',
                    '-drive', f'file={variables},if=pflash,format=raw',
                    '-drive', f'file={args.assets}/seed.iso,format=raw,if=none,id=seed,readonly=on',
                    '-device', 'virtio-blk-pci,drive=seed,addr=0x2,serial=m0-seed',
                    '-netdev', 'user,id=nat', '-device', 'virtio-net-pci,netdev=nat']
        else:
            cmd += ['-kernel', str(args.assets / 'kernel'), '-initrd', str(args.assets / 'initrd'),
                    '-append', (args.assets / 'kernel-params').read_text(), '-nic', 'none',
                    '-drive', f'file={args.assets}/store.img,format=raw,if=none,id=store,readonly=on',
                    '-device', 'virtio-blk-pci,drive=store,addr=0x2,serial=m0-store']
        for boot in range(2):
            process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT)
            console = Console(process)
            try:
                wait_shell(console, timeout=900 if args.kind == 'ubuntu' and boot == 0 else 180,
                           log=args.output.with_suffix('.console.log'))
                console.command('test "$(cat /etc/gaoji-m0-disposable)" = gaoji-m0')
                console.command('systemctl is-active docker', timeout=120)
                if boot == 0:
                    console.command('mkdir -p /mnt/fixtures')
                    console.command('mount -o ro /dev/disk/by-label/M0FIXTURES /mnt/fixtures')
                    console.command('docker --host=unix:///var/run/docker.sock load -i /mnt/fixtures/python-worker.tar',
                                    timeout=180)
                    result = console.command('python3 /mnt/fixtures/verify_project_native_host.py', timeout=300)
                    values = [line for line in result['output'].splitlines() if line.startswith('{')]
                    report['probe'] = json.loads(values[-1])
                    console.command('printf retained-proof > /var/tmp/m0-reboot-proof')
                    console.command('sync')
                else:
                    console.command('test "$(cat /var/tmp/m0-reboot-proof)" = retained-proof')
                    console.command('test -z "$(docker ps -aq --filter label=io.gaoji.m0)"')
                    console.command('test -z "$(docker volume ls -q --filter label=io.gaoji.m0)"')
                    report['host_reboot_verified'] = True
                process.stdin.write(b'systemctl poweroff\n')
                process.stdin.flush()
                # Keep draining the serial console while systemd powers off.
                deadline = time.monotonic() + 90
                while process.poll() is None and time.monotonic() < deadline:
                    for key, _ in console.reader.select(timeout=0.2):
                        console.output.extend(os.read(key.fileobj.fileno(), 65536))
                if process.poll() != 0:
                    raise RuntimeError('Guest did not power off cleanly')
            except Exception as exc:
                report['error'] = f'{type(exc).__name__}: {exc}'
            finally:
                logs.append(console.output.decode(errors='replace'))
                report.setdefault('events', []).extend(console.events)
                console.close()
                stop(process)
            if 'error' in report:
                break
        checked = subprocess.run([args.qemu_img, 'check', '--output=json', str(root)],
                                 capture_output=True, text=True, timeout=30)
        report['image_check_returncode'] = checked.returncode
        report['image_check'] = checked.stdout
    report['temporary_root_removed'] = not root.exists()
    args.output.with_suffix('.console.log').write_text('\n'.join(logs))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=['ubuntu', 'nixos'], required=True)
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--fixtures', type=Path, required=True)
    parser.add_argument('--firmware', type=Path)
    parser.add_argument('--qemu', required=True)
    parser.add_argument('--qemu-img', required=True)
    parser.add_argument('--accel', choices=['hvf', 'tcg'], default='tcg')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.assets = args.assets.resolve(strict=True)
    args.fixtures = args.fixtures.resolve(strict=True)
    if args.kind == 'ubuntu':
        if not args.firmware:
            parser.error('Ubuntu UEFI firmware directory required')
        args.firmware = args.firmware.resolve(strict=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = run(args)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: value for key, value in report.items() if key != 'events'}, indent=2))
    return 0 if (not report.get('error') and report.get('host_reboot_verified')
        and report.get('probe', {}).get('hard_quota_verified')
        and report['image_check_returncode'] == 0 and report['temporary_root_removed']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
