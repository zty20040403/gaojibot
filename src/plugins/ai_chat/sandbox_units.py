"""Guest-only systemd reconciliation; user files never replace system units."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile

MARKER = b'# gaoji managed program v1\n'
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,71}\.(?:service|timer)')
SLICE = 'gaoji-programs.slice'
DROPIN = 'zz-gaoji-program.conf'


class UnitError(RuntimeError):
    pass


def directory(path, *, create=False):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise UnitError('invalid_directory')
    fd = os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part,0o755,dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd); fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_at(fd, name):
    file = os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
    try:
        size = os.fstat(file)
        if not stat.S_ISREG(size.st_mode) or not 0<size.st_size<=65536:
            raise UnitError('invalid_unit_file')
        with os.fdopen(os.dup(file),'rb') as stream:
            data = stream.read(65537)
        after = os.fstat(file)
        if (len(data)!=size.st_size or (size.st_mtime_ns,size.st_ctime_ns,size.st_size)!=(after.st_mtime_ns,after.st_ctime_ns,after.st_size)):
            raise UnitError('unit_file_changed')
        if b'\x00' in data:
            raise UnitError('invalid_unit_file')
        data.decode('utf-8')
        return data
    finally:
        os.close(file)


def install(path, content):
    fd = directory(path.parent,create=True)
    temporary = '.gaoji-'+secrets.token_hex(12)
    try:
        try:
            previous = read_at(fd,path.name)
        except FileNotFoundError:
            previous = None
        if previous is not None and not previous.startswith(MARKER):
            raise UnitError('unmanaged_file_conflict')
        if previous==content:
            return False
        out = os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644,dir_fd=fd)
        with os.fdopen(out,'wb') as stream:
            stream.write(content)
        os.replace(temporary,path.name,src_dir_fd=fd,dst_dir_fd=fd)
        return True
    finally:
        try:
            os.unlink(temporary,dir_fd=fd)
        except FileNotFoundError:
            pass
        os.close(fd)


def remove(path):
    fd = directory(path.parent)
    try:
        if not read_at(fd,path.name).startswith(MARKER):
            raise UnitError('unmanaged_file_conflict')
        os.unlink(path.name,dir_fd=fd)
    finally:
        os.close(fd)


def run(args):
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(args,stdout=output,stderr=subprocess.STDOUT,timeout=10)
        output.seek(0)
        text = output.read(16000).decode(errors='replace')
    return result.returncode,re.sub(r'gvm_[A-Za-z0-9_-]{43}','[VM_TOKEN]',text)


class Units:
    def __init__(self, registry=Path('/workspace/.gaoji/systemd'), runtime=Path('/run/systemd/system'), *,
                 system=(Path('/etc/systemd/system'),Path('/usr/lib/systemd/system'),Path('/lib/systemd/system')), command=run):
        self.registry,self.runtime,self.system,self.command = registry,runtime,system,command

    def control(self, *args):
        code,text = self.command(['systemctl',*args])
        if code:
            raise UnitError('systemctl_failed: '+text[:1000])
        return text

    def status(self, name):
        text = self.control('show','--no-pager','--property=LoadState,ActiveState,SubState,Result,Slice',name)
        result = dict(line.split('=',1) for line in text.splitlines() if '=' in line)
        if result.get('ActiveState')=='failed':
            code,logs = self.command(['journalctl','--no-pager','-o','cat','-n','8','-u',name])
            result['logs'] = logs[-4000:] if not code else 'journal_unavailable'
        return dict(name=name,**result)

    def conflict(self, name):
        return name in {'gaoji-units.service','gaoji-units-boot.service'} or any(os.path.lexists(path/name) for path in self.system)

    def sync(self, *, enabled=True, disabled=()):
        if (type(enabled) is not bool or not isinstance(disabled,(list,tuple)) or len(disabled)>256
                or any(not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,79}',name) for name in disabled)):
            raise UnitError('invalid_unit_policy')
        fd = directory(self.runtime,create=True)
        lock = os.open('.gaoji-units.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600,dir_fd=fd)
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            return self._sync(fd,enabled,set(disabled))
        finally:
            os.close(lock); os.close(fd)

    def _sync(self, runtime_fd, enabled, disabled):
        desired,errors,owned = {},{},set()
        try:
            root = directory(self.registry)
        except FileNotFoundError:
            root = None
        if root is not None:
            try:
                names = [name for name in os.listdir(root) if name.endswith(('.service','.timer'))]
                if len(names)>32:
                    raise UnitError('unit_limit_exceeded')
                for name in names:
                    try:
                        if not NAME.fullmatch(name) or self.conflict(name):
                            raise UnitError('system_unit_or_invalid_name')
                        try:
                            if not read_at(runtime_fd,name).startswith(MARKER):
                                raise UnitError('unmanaged_file_conflict')
                        except FileNotFoundError:
                            pass
                        desired[name] = read_at(root,name)
                    except (OSError,ValueError,UnitError) as error:
                        errors[name] = str(error) if isinstance(error,UnitError) else type(error).__name__
            finally:
                os.close(root)
        for name in os.listdir(runtime_fd):
            if NAME.fullmatch(name):
                try:
                    if read_at(runtime_fd,name).startswith(MARKER):
                        owned.add(name)
                except (OSError,ValueError,UnitError):
                    pass # Not an owned file; never stop or remove it.
        if self.conflict(SLICE):
            raise UnitError('system_slice_conflict')
        install(self.runtime/SLICE,MARKER+b'[Slice]\nCPUWeight=10\nIOWeight=10\nMemoryMax=50%\n')
        changed,blocked = [],set()
        for name in sorted(owned-desired.keys()-errors.keys()):
            try:
                if self.conflict(name):
                    raise UnitError('system_unit_conflict')
                self.control('stop',name)
                remove(self.runtime/name)
                try:
                    remove(self.runtime/(name+'.d')/DROPIN)
                except FileNotFoundError:
                    pass
                try:
                    remove(self.runtime/('.gaoji-paused-'+name))
                except FileNotFoundError:
                    pass
            except (OSError,UnitError,subprocess.TimeoutExpired) as error:
                errors[name] = str(error)[:1000]
        for name,content in desired.items():
            try:
                pause = self.runtime/('.gaoji-paused-'+name)
                was_paused = os.path.lexists(pause)
                if not enabled or name.rsplit('.',1)[0] in disabled:
                    blocked.add(name)
                    install(pause,MARKER+b'disabled\n')
                elif was_paused:
                    remove(pause)
                override = MARKER+('[Unit]\nConditionPathExists=!'+str(pause)+'\n').encode()
                override += (('[Service]\nSlice='+SLICE+'\nEnvironment="GAOJI_PROGRAM='+name.rsplit('.',1)[0]+'"\n').encode()
                    if name.endswith('.service') else b'[Timer]\n')
                updated = install(self.runtime/(name+'.d')/DROPIN,override)
                if install(self.runtime/name,MARKER+content) or updated or was_paused:
                    changed.append(name)
            except (OSError,ValueError,UnitError) as error:
                errors[name] = str(error)[:1000]
        self.control('daemon-reload')
        for name in sorted(set(changed)|blocked):
            try:
                self.control('stop' if name in blocked else 'restart' if name in owned else 'start',name)
            except (UnitError,subprocess.TimeoutExpired) as error:
                errors[name] = str(error)[:1000]
        rows = []
        for name in sorted(desired.keys()-errors.keys() | set(changed)):
            try:
                row = self.status(name)
                row['enabled'] = name not in blocked
                rows.append(row)
                if row.get('ActiveState')=='failed':
                    errors.setdefault(name,'unit_failed')
            except (UnitError,subprocess.TimeoutExpired) as error:
                errors[name] = str(error)[:1000]
        return dict(ok=not errors,units=rows,errors=errors)


def main():
    parser = argparse.ArgumentParser(description='Root-only guest worker; use gaoji-units from the sandbox user.')
    parser.add_argument('--policy-stdin',action='store_true')
    args = parser.parse_args()
    try:
        import sys
        policy = json.loads(sys.stdin.read(32769)) if args.policy_stdin else {}
        if type(policy) is not dict or policy.keys()-{'enabled','disabled'}:
            raise UnitError('invalid_unit_policy')
        result = Units().sync(**policy)
    except (OSError,ValueError,UnitError,subprocess.TimeoutExpired) as error:
        result = dict(ok=False,error=str(error)[:1000])
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__=='__main__':
    raise SystemExit(main())
