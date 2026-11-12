"""Dependency-free guest helper. It never prints a token, only its fingerprint."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from urllib.parse import urlsplit
from uuid import UUID

TOKEN = re.compile(r'gvm_[A-Za-z0-9_-]{43}')


def _read(fd, name, limit):
    try:
        child = os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
    except FileNotFoundError:
        return None
    with os.fdopen(child,'rb') as data:
        if not stat.S_ISREG(os.fstat(data.fileno()).st_mode):
            raise ValueError('invalid_token_file')
        value = data.read(limit+1)
        if len(value)>limit:
            raise ValueError('invalid_token_file')
        return value


def _write(fd, name, content):
    temporary = '.token-'+secrets.token_hex(12)
    child = os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
    try:
        with os.fdopen(child,'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary,name,src_dir_fd=fd,dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary,dir_fd=fd)
        except FileNotFoundError:
            pass


def _url(value):
    parsed = urlsplit(value)
    if (parsed.scheme!='http' or parsed.hostname!='10.0.2.2' or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment or not parsed.port
            or value!=f'http://10.0.2.2:{parsed.port}'):
        raise ValueError('invalid_host_url')
    return value


def run(payload, *, directory=Path('/run/gaoji'), boot_file=Path('/proc/sys/kernel/random/boot_id')):
    boot = boot_file.read_text().strip()
    if str(UUID(boot))!=boot:
        raise ValueError('invalid_boot_id')
    if payload.get('op')=='install':
        if (set(payload)!={'op','token','boot_id','base_url'} or payload['boot_id']!=boot
                or type(payload['token']) is not str or TOKEN.fullmatch(payload['token']) is None):
            raise ValueError('invalid_token_install')
        base_url = _url(payload['base_url'])
        directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    elif payload!={'op':'inspect'}:
        raise ValueError('invalid_token_operation')
    try:
        fd = os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    except FileNotFoundError:
        return dict(boot_id=boot,token_hash=None,base_url=None)
    try:
        if payload['op']=='install':
            os.fchmod(fd,0o700)
            _write(fd,'connection.json',json.dumps({'base_url':base_url}).encode())
            _write(fd,'token',payload['token'].encode())
        token = _read(fd,'token',128)
        connection = _read(fd,'connection.json',1024)
        if token is not None and TOKEN.fullmatch(token.decode('ascii')) is None:
            raise ValueError('invalid_token_file')
        result = dict(boot_id=boot,token_hash=hashlib.sha256(token).hexdigest() if token else None,
            base_url=_url(json.loads(connection)['base_url']) if connection else None)
        # The unprivileged sandbox user must read this conversation capability.
        # It is not a host secret. Repair old root-only installations on inspect
        # without rotating a valid boot token or allowing guest users to write it.
        for name,value in (('token',token),('connection.json',connection)):
            if value is not None:
                child = os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
                try:
                    if not stat.S_ISREG(os.fstat(child).st_mode):
                        raise ValueError('invalid_token_file')
                    os.fchmod(child,0o644)
                finally:
                    os.close(child)
        os.fchmod(fd,0o755)
        return result
    finally:
        os.close(fd)


if __name__=='__main__':
    try:
        print(json.dumps(run(json.loads(sys.stdin.read(2048)))))
    except Exception:
        print('{"error":"vm_token_io_failed"}')
        sys.exit(1)
