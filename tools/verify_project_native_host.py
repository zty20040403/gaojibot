"""M0 quota probe, run as root ONLY inside a marked disposable native Linux VM.

No daemon configuration, pre-existing volumes or host files are changed. Uses an
already loaded Python image and the guest's local Docker socket. The marker is
an accident guard, not a security boundary. Never run this on a production host.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import subprocess
import tempfile

from verify_project_storage import WORKER


def command(*args, check=True, source=None, timeout=120):
    return subprocess.run(args, check=check, input=source, text=True,
                          capture_output=True, timeout=timeout)


class NativeProbe:
    def __init__(self, root: Path, image: str):
        self.root = root
        self.owner = secrets.token_hex(8)
        self.label = f'io.gaoji.m0={self.owner}'
        self.image = self.docker('image', 'inspect', '--format', '{{.Id}}', image).stdout.strip()
        self.devices: dict[str, str] = {}
        self.mounts: list[Path] = []
        self.containers: list[str] = []
        self.volumes: list[str] = []

    def docker(self, *args, **kwargs):
        return command('docker', '--host=unix:///var/run/docker.sock', *args, **kwargs)

    def setup_volume(self, name):
        image = self.root / f'{name}.ext4'
        with image.open('xb') as stream:
            stream.truncate(96 * 1024 * 1024)
        command('mkfs.ext4', '-q', '-F', '-m', '0', str(image))
        device = command('losetup', '--find', '--show', str(image)).stdout.strip()
        self.devices[name] = device
        mount = self.root / name
        mount.mkdir()
        command('mount', '-t', 'ext4', '-o', 'nosuid,nodev', device, str(mount))
        self.mounts.append(mount)
        mount.chmod(0o777)
        return mount

    def worker(self, mount, mode, *, volume=False):
        name = f'gaoji-m0-{self.owner}-{secrets.token_hex(4)}'
        self.containers.append(name)
        return self.docker('run', '--rm', '--pull=never', '--name', name,
            '--label', self.label, '--network=none', '--read-only', '--cap-drop=ALL',
            '--user=65534:65534', '--security-opt=no-new-privileges',
            '--memory=128m', '--cpus=1', '--pids-limit=32', '--mount',
            f'type={"volume" if volume else "bind"},src={mount},dst=/workspace',
            '--entrypoint=python', '-i', self.image, '-', mode, source=WORKER, check=False)

    def checked_worker(self, mount, mode):
        result = self.worker(mount, mode)
        if result.returncode:
            raise RuntimeError(result.stderr[-4000:] + result.stdout[-4000:])
        return json.loads(result.stdout)

    def measure(self):
        a, b = self.setup_volume('a'), self.setup_volume('b')
        baseline = self.root / 'baseline'
        baseline.mkdir(mode=0o777)
        baseline.chmod(0o777)
        report = {'seed_b': self.checked_worker(b, 'seed'),
                  'baseline': self.checked_worker(baseline, 'benchmark'),
                  'quota': self.checked_worker(a, 'benchmark'),
                  'mounts': command('findmnt', '-J', '--target', str(a)).stdout}
        report['fill_a'] = self.checked_worker(a, 'fill')
        report['independent_b'] = self.checked_worker(b, 'independent')
        report['quota_to_baseline_ratio'] = (report['quota']['median_mib_per_second'] /
                                             report['baseline']['median_mib_per_second'])
        command('sync')
        image_a = self.root / 'a.ext4'
        report['allocated_full'] = image_a.stat().st_blocks * 512
        (a / 'fill').unlink()
        command('sync')
        trimmed = command('fstrim', '-v', str(a), check=False)
        report['trim'] = {'returncode': trimmed.returncode, 'output': trimmed.stdout,
                          'error': trimmed.stderr}
        command('sync')
        report['allocated_trimmed'] = image_a.stat().st_blocks * 512
        command('umount', str(b))
        self.mounts.remove(b)
        command('mount', '-t', 'ext4', '-o', 'nosuid,nodev', self.devices['b'], str(b))
        self.mounts.append(b)
        report['remount_b'] = self.checked_worker(b, 'independent')

        # Candidate B gets a different, unmounted image. It cannot alias A or B.
        direct = self.root / 'direct.ext4'
        with direct.open('xb') as stream:
            stream.truncate(96 * 1024 * 1024)
        command('mkfs.ext4', '-q', '-F', '-m', '0', str(direct))
        volume = f'gaoji-m0-{self.owner}-direct'
        self.volumes.append(volume)
        self.docker('volume', 'create', '--label', self.label, '--driver=local',
                    '--opt=type=ext4', f'--opt=device={direct}', '--opt=o=loop,nosuid,nodev', volume)
        result = self.worker(volume, 'driver', volume=True)
        report['direct_image_driver'] = {'returncode': result.returncode,
            'stdout': result.stdout[-4000:], 'stderr': result.stderr[-4000:]}
        report['hard_quota_verified'] = (report['fill_a']['errno'] == 28
            and report['independent_b']['errno'] is None
            and report['remount_b']['sentinel_sha256'] == report['seed_b']['sentinel_sha256'])
        return report

    def cleanup(self):
        # Only owned, labelled containers can be forcibly removed. A failed
        # unmount/detach aborts removal of the backing directory for diagnosis.
        for name in reversed(self.containers):
            value = self.docker('inspect', name, check=False)
            if value.returncode:
                continue
            assert json.loads(value.stdout)[0]['Config']['Labels']['io.gaoji.m0'] == self.owner
            self.docker('rm', '--force', name)
        for name in reversed(self.volumes):
            value = self.docker('volume', 'inspect', name, check=False)
            if value.returncode:
                continue
            assert json.loads(value.stdout)[0]['Labels']['io.gaoji.m0'] == self.owner
            self.docker('volume', 'rm', name)
        for mount in reversed(self.mounts):
            command('umount', str(mount))
        for name, device in self.devices.items():
            loops = json.loads(command('losetup', '--json', '--list', '--associated',
                                        str(self.root / f'{name}.ext4')).stdout)
            matches = [item for item in loops.get('loopdevices', []) if item['name'] == device]
            assert len(matches) == 1 and matches[0]['back-file'] == str(self.root / f'{name}.ext4')
            command('losetup', '--detach', device)
        assert not self.docker('ps', '-aq', '--filter', f'label={self.label}').stdout.strip()
        assert not self.docker('volume', 'ls', '-q', '--filter', f'label={self.label}').stdout.strip()
        for name in ('a', 'b', 'direct'):
            assert not json.loads(command('losetup', '--json', '--list', '--associated',
                str(self.root / f'{name}.ext4')).stdout).get('loopdevices')
        shutil.rmtree(self.root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='python:3.12-slim')
    args = parser.parse_args()
    assert os.geteuid() == 0, 'Guest root required'
    assert Path('/etc/gaoji-m0-disposable').read_text().strip() == 'gaoji-m0'
    assert not Path('/run/systemd/container').exists(), 'Must boot a real guest kernel'
    virtualisation = command('systemd-detect-virt', '--vm').stdout.strip()
    assert virtualisation in {'qemu', 'kvm', 'apple'}, virtualisation
    assert Path('/var/run/docker.sock').is_socket()
    root = Path(tempfile.mkdtemp(prefix='gaoji-m0-', dir='/var/tmp'))
    root.chmod(0o755)
    report = {'os_release': platform.freedesktop_os_release(), 'kernel': platform.release(),
              'virtualisation': virtualisation, 'docker_version': command('docker',
              '--host=unix:///var/run/docker.sock', 'version', '--format', '{{.Server.Version}}').stdout.strip()}
    probe = None
    try:
        probe = NativeProbe(root, args.image)
        report['image'] = probe.image
        report.update(probe.measure())
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        try:
            if probe:
                probe.cleanup()
            else:
                root.rmdir()
            report['owned_resources_removed'] = not root.exists()
        except Exception as exc:
            report['cleanup_error'] = f'{type(exc).__name__}: {exc}'
    print(json.dumps(report, sort_keys=True), flush=True)
    return 0 if report.get('hard_quota_verified') and report.get('owned_resources_removed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
