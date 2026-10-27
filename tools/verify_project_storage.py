"""M0 experiments, not a product volume manager.

Uses one explicitly selected local Docker socket and existing Python image.
Only the ephemeral helper is privileged. No pre-existing host data, credentials,
network or existing volumes are mounted. Small owned disk images are removed
in finally. Docker Desktop results are NOT native Ubuntu/NixOS validation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import time


SETUP = r'''
import json,os,pathlib,platform,shutil,subprocess
root=pathlib.Path('/fixtures')
result={'kernel':platform.release(),'tools':{name:shutil.which(name) for name in
 ['losetup','mount','umount','mkfs.ext4','fstrim']},'volumes':{}}
assert all(result['tools'].values()),result
for name in ('a','b'):
 image=root/(name+'.ext4')
 with image.open('xb') as f:f.truncate(96*1024*1024)
 subprocess.run(['mkfs.ext4','-q','-F','-m','0',str(image)],check=True)
 device=subprocess.check_output(['losetup','--find','--show',str(image)],text=True).strip()
 target=pathlib.Path('/work')/name
 target.mkdir()
 subprocess.run(['mount','-t','ext4','-o','nosuid,nodev',device,str(target)],check=True)
 target.chmod(0o777)
 result['volumes'][name]={'device':device,'image_size':image.stat().st_size,
  'allocated':image.stat().st_blocks*512}
baseline=pathlib.Path('/baseline')
baseline.chmod(0o777)
result['mounts']=[line.strip() for line in open('/proc/mounts') if ' /work/' in line]
for name in ('a','b'):
 subprocess.run(['umount','/work/'+name],check=True)
print(json.dumps(result))
'''

WORKER = r'''
import errno,hashlib,json,os,pathlib,statistics,sys,time
root=pathlib.Path('/workspace')
mode=sys.argv[1]
block=os.urandom(1024*1024)
result={'uid':os.getuid(),'mode':mode}
def write(path,count):
 total=0
 try:
  with path.open('wb',buffering=0) as f:
   for i in range(count):
    view=memoryview(block)
    while view:
     n=f.write(view)
     total+=n
     view=view[n:]
   os.fsync(f.fileno())
 except OSError as e:
  return {'bytes':total,'errno':e.errno}
 return {'bytes':total,'errno':None}
if mode=='benchmark':
 rates=[]
 for i in range(3):
  path=root/'benchmark'
  start=time.monotonic()
  r=write(path,32)
  elapsed=time.monotonic()-start
  assert r['errno'] is None,r
  rates.append(32/elapsed)
  path.unlink()
 result['mib_per_second']=rates
 result['median_mib_per_second']=statistics.median(rates)
elif mode=='seed':
 (root/'sentinel').write_bytes(b'project-b-unchanged'*128)
 result['sentinel_sha256']=hashlib.sha256((root/'sentinel').read_bytes()).hexdigest()
elif mode=='fill':
 result.update(write(root/'fill',128))
 assert result['errno']==errno.ENOSPC,result
 assert result['bytes']<96*1024*1024,result
elif mode=='independent':
 assert (root/'sentinel').read_bytes()==b'project-b-unchanged'*128
 result.update(write(root/'after-a-full',16))
 assert result['errno'] is None,result
 result['sentinel_sha256']=hashlib.sha256((root/'sentinel').read_bytes()).hexdigest()
elif mode=='driver':
 result.update(write(root/'driver',1))
 assert result['errno'] is None,result
else:raise ValueError(mode)
print(json.dumps(result))
'''

TRIM = r'''
import json,pathlib,subprocess
image=pathlib.Path('/fixtures/a.ext4')
before=image.stat().st_blocks*512
device=subprocess.check_output(['losetup','--noheadings','--output','NAME','--associated',str(image)],text=True).strip()
assert device.startswith('/dev/loop') and '\n' not in device
subprocess.run(['mount','-t','ext4','-o','nosuid,nodev',device,'/work/a'],check=True)
(pathlib.Path('/work/a')/'fill').unlink()
subprocess.run(['sync'],check=True)
r=subprocess.run(['fstrim','-v','/work/a'],capture_output=True,text=True)
subprocess.run(['umount','/work/a'],check=True)
print(json.dumps({'allocated_before':before,'allocated_after':image.stat().st_blocks*512,
 'returncode':r.returncode,'output':r.stdout.strip(),'error':r.stderr.strip()}))
'''

TEARDOWN = r'''
import pathlib,subprocess
for name in ('a','b'):
 target='/work/'+name
 if any(line.split()[1]==target for line in open('/proc/mounts')):
  subprocess.run(['umount',target],check=True)
 image='/fixtures/'+name+'.ext4'
 result=subprocess.run(['losetup','--json','--list','--associated',image],
  check=True,capture_output=True,text=True)
 import json
 for item in json.loads(result.stdout).get('loopdevices',[]):
  assert item['back-file']==image,item
  subprocess.run(['losetup','--detach',item['name']],check=True)
'''


class Probe:
    def __init__(self, docker: str, socket: Path, image: str):
        if not socket.is_socket():
            raise ValueError('An explicit local Unix Docker socket is required')
        self.prefix = [docker, '--host', f'unix://{socket}']
        self.owner = secrets.token_hex(8)
        self.label = f'io.gaoji.m0={self.owner}'
        self.helper = f'gaoji-m0-{self.owner}-helper'
        self.volume = f'gaoji-m0-{self.owner}-images'
        self.baseline = f'gaoji-m0-{self.owner}-baseline'
        self.driver_volume = f'gaoji-m0-{self.owner}-driver'
        self.containers: list[str] = []
        self.volumes: list[str] = []
        self.image = self.run('image', 'inspect', '--format', '{{.Id}}', image).stdout.strip()

    def run(self, *args: str, source: str | None = None, check=True, timeout=45):
        return subprocess.run([*self.prefix, *args], input=source, text=True,
                              capture_output=True, check=check, timeout=timeout)

    def execute(self, code: str):
        return self.run('exec', '-i', self.helper, 'python', '-', source=code)

    def worker(self, source: str, mode: str):
        name = f'gaoji-m0-{self.owner}-{mode}-{secrets.token_hex(3)}'
        self.containers.append(name)
        mount = f'type=volume,src={source},dst=/workspace'
        return self.run('run', '--rm', '--pull=never', '--name', name, '--label', self.label,
            '--network=none', '--read-only', '--cap-drop=ALL', '--user=65534:65534',
            '--security-opt=no-new-privileges', '--pids-limit=32', '--memory=128m', '--cpus=1',
            '--mount', mount, '--entrypoint=python', '-i', self.image, '-', mode,
            source=WORKER, check=False)

    def cleanup(self):
        errors = []
        for name in reversed(self.containers):
            inspected = self.run('inspect', name, check=False)
            if inspected.returncode:
                continue
            if json.loads(inspected.stdout)[0]['Config']['Labels'].get('io.gaoji.m0') != self.owner:
                errors.append(f'ownership mismatch: {name}')
                continue
            result = self.run('rm', '--force', name, check=False)
            if result.returncode:
                errors.append(result.stderr.strip())
        # Local-driver mounts must disappear before their helper-owned loop devices.
        for name in reversed(self.volumes):
            if name in {self.volume, self.baseline}:
                continue
            inspected = self.run('volume', 'inspect', name, check=False)
            if inspected.returncode:
                continue
            if json.loads(inspected.stdout)[0]['Labels'].get('io.gaoji.m0') != self.owner:
                raise RuntimeError('Driver volume ownership changed; refusing cleanup')
            self.run('volume', 'rm', name)
        inspected = self.run('inspect', self.helper, check=False)
        if inspected.returncode == 0:
            if json.loads(inspected.stdout)[0]['Config']['Labels'].get('io.gaoji.m0') != self.owner:
                raise RuntimeError('Helper ownership changed; refusing cleanup')
            result = self.run('exec', '-i', self.helper, 'python', '-', source=TEARDOWN, check=False)
            if result.returncode:
                errors.append(result.stderr.strip())
            self.run('rm', '--force', self.helper)
        for name in reversed(self.volumes):
            inspected = self.run('volume', 'inspect', name, check=False)
            if inspected.returncode:
                continue
            if json.loads(inspected.stdout)[0]['Labels'].get('io.gaoji.m0') != self.owner:
                errors.append(f'volume ownership mismatch: {name}')
                continue
            result = self.run('volume', 'rm', name, check=False)
            if result.returncode:
                errors.append(result.stderr.strip())
        remaining = self.run('ps', '--all', '--filter', f'label={self.label}', '--format', '{{.Names}}').stdout.strip()
        volumes = self.run('volume', 'ls', '--filter', f'label={self.label}', '--format', '{{.Name}}').stdout.strip()
        if errors or remaining or volumes:
            raise RuntimeError(json.dumps({'errors': errors, 'containers': remaining, 'volumes': volumes}))

    def measure(self):
        report = {'environment': json.loads(self.run('info', '--format', '{{json .}}').stdout),
                  'image': self.image, 'owner': self.owner, 'started_at': int(time.time())}
        # Keep the report public-safe; Docker info may include local registry settings.
        info = report['environment']
        report['environment'] = {key: info.get(key) for key in
            ('OperatingSystem', 'OSType', 'Architecture', 'KernelVersion', 'Driver', 'ServerVersion')}
        try:
            for name in (self.volume, self.baseline):
                self.volumes.append(name)
                self.run('volume', 'create', '--label', self.label, name)
            self.run('run', '--detach', '--pull=never', '--name', self.helper, '--label', self.label,
                '--privileged', '--user=0:0', '--network=none', '--read-only', '--pids-limit=64', '--memory=512m',
                '--cpus=1', '--mount', f'type=volume,src={self.volume},dst=/fixtures',
                '--mount', f'type=volume,src={self.baseline},dst=/baseline',
                '--tmpfs', '/work:rw,nosuid,nodev,size=32m', '--tmpfs', '/tmp:rw,nosuid,nodev,size=32m',
                '--entrypoint=python', self.image, '-c', 'import time; time.sleep(600)')
            setup = self.execute(SETUP)
            report['helper'] = json.loads(setup.stdout)
            projects = {}
            for project, entry in report['helper']['volumes'].items():
                name = f'gaoji-m0-{self.owner}-{project}'
                self.volumes.append(name)
                self.run('volume', 'create', '--label', self.label, '--driver', 'local',
                    '--opt', 'type=ext4', '--opt', f'device={entry["device"]}',
                    '--opt', 'o=nosuid,nodev', name)
                projects[project] = name
            results = {}
            for key, path, mode in (
                ('baseline', self.baseline, 'benchmark'),
                ('quota', projects['a'], 'benchmark'), ('b_seed', projects['b'], 'seed'),
                ('a_full', projects['a'], 'fill'), ('b_after', projects['b'], 'independent'),
            ):
                result = self.worker(path, mode)
                if result.returncode:
                    raise RuntimeError(f'{key}: {result.stderr.strip()} {result.stdout.strip()}')
                results[key] = json.loads(result.stdout)
            results['median_throughput_ratio'] = (results['quota']['median_mib_per_second'] /
                                                   results['baseline']['median_mib_per_second'])
            report['candidate_a'] = results
            report['candidate_a']['mount_strategy'] = 'helper-owned loop device, Docker local ext4 mount'
            for name in projects.values():
                self.run('volume', 'rm', name)
            report['ext4_trim'] = json.loads(self.execute(TRIM).stdout)
            # Never mount the same ext4 image twice through independent devices.
            self.execute(TEARDOWN.replace("('a','b')", "('b',)"))
            self.volumes.append(self.driver_volume)
            mountpoint = self.run('volume', 'inspect', '--format', '{{.Mountpoint}}', self.volume).stdout.strip()
            self.run('volume', 'create', '--label', self.label, '--driver', 'local',
                '--opt', 'type=ext4', '--opt', f'device={mountpoint}/b.ext4',
                '--opt', 'o=loop,nosuid,nodev', self.driver_volume)
            result = self.worker(self.driver_volume, 'driver')
            report['candidate_b'] = {'returncode': result.returncode, 'stdout': result.stdout.strip(),
                                     'stderr': result.stderr.strip()}
        except (RuntimeError, subprocess.SubprocessError) as exc:
            report['experiment_error'] = str(exc)
            if isinstance(exc, subprocess.CalledProcessError):
                report['experiment_stderr'] = exc.stderr
        finally:
            self.cleanup()
            report['cleanup_verified'] = True
        return report


def qcow_probe(qemu_img: str, qemu_io: str):
    """Measure QCOW2 discard only; explicitly NOT guest fstrim acceptance."""
    def run(*command):
        return subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)

    with tempfile.TemporaryDirectory(prefix='gaoji-m0-qcow-') as directory:
        path = Path(directory) / 'probe.qcow2'
        run(qemu_img, 'create', '-f', 'qcow2', str(path), '128M')
        empty = path.stat().st_blocks * 512
        run(qemu_io, '-f', 'qcow2', '-d', 'unmap', '-c', 'write -P 0x61 0 64M', str(path))
        written = path.stat().st_blocks * 512
        run(qemu_io, '-f', 'qcow2', '-d', 'unmap', '-c', 'discard 0 64M', str(path))
        discarded = path.stat().st_blocks * 512
        result = run(qemu_img, 'check', '-f', 'qcow2', '--output=json', str(path))
        return {'qemu_version': run(qemu_img, '--version').stdout.splitlines()[0],
                'empty_allocated': empty, 'written_allocated': written,
                'discarded_allocated': discarded, 'check': json.loads(result.stdout),
                'guest_fstrim_verified': False, 'temporary_disk_removed_on_return': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--docker', required=True)
    parser.add_argument('--socket', type=Path, required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--qemu-img')
    parser.add_argument('--qemu-io')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if bool(args.qemu_img) != bool(args.qemu_io):
        parser.error('Supply both QEMU binaries or neither')
    report = Probe(args.docker, args.socket, args.image).measure()
    if args.qemu_img:
        report['qcow2'] = qcow_probe(args.qemu_img, args.qemu_io)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 1 if 'experiment_error' in report else 0


if __name__ == '__main__':
    raise SystemExit(main())
