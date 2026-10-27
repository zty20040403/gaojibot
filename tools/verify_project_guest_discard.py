"""M0 local no-network QEMU guest discard probe using a supplied read-only root.

The root image must contain /bin/sh and ext4/trim tools. Only a newly created
128 MiB QCOW2 device is formatted. Does not use libvirt, host directories, SSH,
credentials or any production VM. Neither supplied image nor kernel is changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import subprocess
import sys
import tempfile
import time


class Console:
    def __init__(self, process):
        self.process = process
        self.reader = selectors.DefaultSelector()
        self.reader.register(process.stdout, selectors.EVENT_READ)
        self.output = bytearray()
        self.events = []

    def wait_for_shell(self):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if re.search(rb'(?:sh-[\d.]+|/)?# $', bytes(self.output[-200:])):
                return
            for key, _ in self.reader.select(timeout=0.2):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    raise RuntimeError('Guest exited before shell: ' +
                                       bytes(self.output[-4000:]).decode(errors='replace'))
                self.output.extend(chunk)
        raise TimeoutError('Guest shell did not start: ' +
                           bytes(self.output[-4000:]).decode(errors='replace'))

    def command(self, command: str, *, timeout=30):
        token = 'M0_' + secrets.token_hex(8)
        start = len(self.output)
        self.process.stdin.write((command + '\nprintf "\\n' + token + ':%s\\n" "$?"\n').encode())
        self.process.stdin.flush()
        pattern = re.compile(rb'(?:\r?\n)' + token.encode() + rb':(\d+)\r?\n')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = bytes(self.output[start:])
            match = pattern.search(data)
            if match:
                event = {'command': command, 'returncode': int(match[1]),
                         'output': data[:match.start()].decode(errors='replace')[-8000:]}
                self.events.append(event)
                if event['returncode']:
                    raise RuntimeError(json.dumps(event))
                return event
            for key, _ in self.reader.select(timeout=0.2):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    raise RuntimeError('Guest console ended: ' + data.decode(errors='replace')[-4000:])
                self.output.extend(chunk)
        raise TimeoutError('Guest console deadline: ' + bytes(self.output[start:]).decode(errors='replace')[-4000:])

    def close(self):
        self.reader.close()


def run(args):
    report = {'guest_fstrim_verified': False, 'root_read_only': True, 'network': 'none',
              'vm_type': 'local QEMU TCG Linux guest; not production KVM/libvirt'}
    with tempfile.TemporaryDirectory(prefix='gaoji-m0-guest-') as directory:
        disk = Path(directory) / 'project.qcow2'
        base = Path(directory) / 'base.qcow2'
        root = Path(directory) / 'root.img'
        copy = (['/bin/cp', '-c'] if sys.platform == 'darwin' else ['cp', '--reflink=auto'])
        copied = subprocess.run([*copy, str(args.root_image), str(root)],
                                capture_output=True, timeout=60)
        if copied.returncode:
            subprocess.run(['cp', str(args.root_image), str(root)], check=True, timeout=60)
        report['private_root_copy'] = True
        subprocess.run([args.qemu_img, 'create', '-f', 'qcow2', str(base), '128M'],
                       check=True, capture_output=True, timeout=30)
        base.chmod(0o444)
        base_hash = hashlib.sha256(base.read_bytes()).hexdigest()
        subprocess.run([args.qemu_img, 'create', '-f', 'qcow2', '-F', 'qcow2',
                        '-b', str(base), str(disk)], check=True, capture_output=True, timeout=30)
        report['qcow2_backing_chain'] = True
        command = [args.qemu, '-machine', 'virt', '-accel', 'tcg', '-cpu', 'max',
            '-m', '768', '-smp', '1', '-nographic', '-no-reboot', '-nic', 'none',
            '-kernel', str(args.kernel), '-append',
            'console=ttyAMA0 root=/dev/vda rootwait ro init=/bin/sh panic=-1',
            '-drive', f'file={root},format=raw,if=none,id=root,readonly=on',
            '-device', 'virtio-blk-pci,drive=root,addr=0x1,serial=m0-root',
            '-drive', f'file={disk},format=qcow2,if=none,id=project,discard=unmap',
            '-device', 'virtio-blk-pci,drive=project,addr=0x2,serial=m0-project']
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
        console = Console(process)
        try:
            console.wait_for_shell()
            console.command('export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin')
            console.command('uname -a', timeout=90)
            for step in (
                'mount -t proc proc /proc', 'mount -t sysfs sysfs /sys',
                'test -b /dev/vdb', 'mount -t tmpfs tmpfs /tmp',
                'test "$(cat /sys/class/block/vdb/size)" = 262144',
                'test "$(cat /sys/class/block/vdb/serial)" = m0-project',
                'command -v mkfs.ext4', 'command -v fstrim', 'mkfs.ext4 -F -m 0 /dev/vdb',
                'mkdir -p /tmp/project', 'mount -t ext4 -o nosuid,nodev /dev/vdb /tmp/project',
                'printf retained-proof > /tmp/project/keep',
                'dd if=/dev/urandom of=/tmp/project/fill bs=1048576 count=48', 'sync',
            ):
                console.command(step)
            report['allocated_written'] = disk.stat().st_blocks * 512
            console.command('rm /tmp/project/fill')
            console.command('sync')
            report['allocated_after_delete'] = disk.stat().st_blocks * 512
            console.command('fstrim -v /tmp/project')
            console.command('sync')
            report['allocated_after_trim'] = disk.stat().st_blocks * 512
            console.command('test "$(cat /tmp/project/keep)" = retained-proof')
            console.command('umount /tmp/project')
            console.command('mount -t ext4 -o nosuid,nodev /dev/vdb /tmp/project')
            console.command('test "$(cat /tmp/project/keep)" = retained-proof')
            console.command('umount /tmp/project')
            report['guest_fstrim_verified'] = (report['allocated_after_trim'] <
                                               report['allocated_written'] - 32 * 1024 * 1024)
            if not report['guest_fstrim_verified']:
                raise RuntimeError('Trim did not release the expected physical space')
        except (RuntimeError, TimeoutError, BrokenPipeError) as exc:
            report['error'] = str(exc)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            console.close()
            process.stdin.close()
            process.stdout.close()
            report['guest_events'] = console.events
            report['qemu_stopped'] = process.poll() is not None
        checked = subprocess.run([args.qemu_img, 'check', '-f', 'qcow2', '--output=json', str(disk)],
                                 check=False, capture_output=True, text=True, timeout=30)
        report['image_check_returncode'] = checked.returncode
        report['image_check'] = checked.stdout
        report['backing_image_unchanged'] = hashlib.sha256(base.read_bytes()).hexdigest() == base_hash
    report['temporary_disk_removed'] = not any(path.exists() for path in (disk, base, root))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    parser.add_argument('--qemu-img', required=True)
    parser.add_argument('--kernel', type=Path, required=True)
    parser.add_argument('--root-image', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.kernel = args.kernel.resolve(strict=True)
    args.root_image = args.root_image.resolve(strict=True)
    report = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    passed = (report['guest_fstrim_verified'] and report['image_check_returncode'] == 0
              and report['backing_image_unchanged'] and report['qemu_stopped']
              and report['temporary_disk_removed'])
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
