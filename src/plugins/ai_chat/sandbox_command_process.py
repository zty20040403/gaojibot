"""Guest systemd payload: stream a sandbox-user process to a bounded work file."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import subprocess
import time

if __package__:
    from .sandbox_units import directory
else:
    from sandbox_units import directory

STATE = Path('/run/gaoji/commands')
OUTPUT = Path('/workspace/.gaoji/out')
MAX_OUTPUT = 64*1024*1024


class CommandCancelled(PermissionError):
    pass


@contextmanager
def locked(fd, identity, *, blocking=True):
    file = os.open(identity+'.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=fd)
    try:
        fcntl.flock(file,fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(file)


def validate(data):
    if (type(data) is not dict or set(data)!={'command_id','program','argv'}
            or type(data['command_id']) is not str or not re.fullmatch(r'[0-9a-f]{32}',data['command_id'])
            or type(data['program']) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,79}',data['program'])
            or type(data['argv']) is not list or not 1<=len(data['argv'])<=128
            or any(type(arg) is not str or '\0' in arg for arg in data['argv']) or not data['argv'][0]
            or len(json.dumps(data,ensure_ascii=False).encode())>32768):
        raise ValueError('invalid_command_payload')


def write_result(fd, name, value):
    temporary = '.'+name+'.'+secrets.token_hex(8)
    out = os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
    try:
        with os.fdopen(out,'w') as stream:
            json.dump(value,stream); stream.flush(); os.fsync(stream.fileno())
        # Preserve an earlier completion even if a duplicate runner is invoked.
        os.link(temporary,name,src_dir_fd=fd,dst_dir_fd=fd,follow_symlinks=False)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary,dir_fd=fd)
        except FileNotFoundError:
            pass


def execute(data, *, state=STATE, output=OUTPUT, cwd='/work', identity=None, limit=MAX_OUTPUT):
    validate(data)
    if identity is None:
        user = pwd.getpwnam('sandbox')
        identity = user.pw_uid,user.pw_gid
    privileges = dict(user=identity[0],group=identity[1],extra_groups=[]) if os.geteuid()==0 else {}
    if not privileges and identity!=(os.getuid(),os.getgid()):
        raise PermissionError('cannot_change_command_user')
    state_fd = directory(state)
    process = None
    started,total,error,code = time.monotonic(),0,'',None
    try:
        # These are workspace directories, not the root-only command manifest.
        parent = directory(output.parent,create=True)
        try:
            os.fchown(parent,*identity)
        finally:
            os.close(parent)
        root = directory(output,create=True)
        try:
            os.fchown(root,*identity)
            out = os.open(data['command_id']+'.txt',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644,dir_fd=root)
        finally:
            os.close(root)
        with os.fdopen(out,'wb',buffering=0) as stream:
            os.fchown(stream.fileno(),*identity)
            environment = dict(PATH='/usr/local/bin:/usr/bin:/bin',HOME=cwd,USER='sandbox',LOGNAME='sandbox',
                GAOJI_PROGRAM=data['program'],LANG='C.UTF-8')
            with locked(state_fd,data['command_id']):
                # The controller records stop even before systemd has accepted
                # start. A late unit may run this wrapper, never its command.
                try:
                    os.stat(data['command_id']+'.stop',dir_fd=state_fd,follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise CommandCancelled()
                process = subprocess.Popen(data['argv'],cwd=cwd,env=environment,stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,stderr=subprocess.STDOUT,**privileges)
            try:
                while True:
                    chunk = process.stdout.read1(16384)
                    if not chunk:
                        break
                    accepted = chunk[:max(0,limit-total)]
                    remaining = memoryview(accepted)
                    while remaining:
                        remaining = remaining[os.write(stream.fileno(),remaining):]
                    total += len(accepted)
                    if len(accepted)!=len(chunk):
                        error = 'command_output_limit'
                        process.kill()
                        break
                code = process.wait(timeout=5)
            finally:
                process.stdout.close()
            os.fsync(stream.fileno())
    except Exception as exception:
        error = 'command_cancelled' if isinstance(exception,CommandCancelled) else 'command_process_'+type(exception).__name__
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                error = 'command_process_stop_unconfirmed'
        try:
            result = dict(exit_code=code,elapsed_seconds=int(time.monotonic()-started),output_bytes=total,error=error)
            write_result(state_fd,data['command_id']+'.result',result)
        finally:
            os.close(state_fd)
    return result


def main():
    import sys
    if len(sys.argv)!=2 or not re.fullmatch(r'[0-9a-f]{32}',sys.argv[1]):
        raise ValueError('invalid_command_id')
    fd = directory(STATE)
    try:
        source = os.open(sys.argv[1]+'.json',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        with os.fdopen(source,'rb') as stream:
            raw = stream.read(32769)
        if len(raw)>32768:
            raise ValueError('command_payload_too_large')
        data = json.loads(raw)
        if data.get('command_id')!=sys.argv[1]:
            raise ValueError('command_manifest_mismatch')
    finally:
        os.close(fd)
    result = execute(data)
    return 1 if result['error'] else min(255,result['exit_code'] if result['exit_code']>=0 else 128-result['exit_code'])


if __name__=='__main__':
    raise SystemExit(main())
