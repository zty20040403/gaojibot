"""Guest browser mutations: write-ahead receipts, no replay after uncertainty."""
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import time
from uuid import UUID

JOURNAL = Path('/var/lib/gaoji/browser-actions')
PROFILE = Path('/work/.gaoji/chromium')


def save(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value,stream,separators=(',',':'))
        stream.flush(); os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


async def execute(request, perform, *, journal=JOURNAL, boot=None, clock=time.time):
    if (type(request) is not dict or set(request)!={'id','boot','expires','action','domain'}
            or type(request['id']) is not str or re.fullmatch('[a-f0-9]{64}',request['id']) is None
            or request['action'] not in {'logout','clear'} or type(request['domain']) is not str or len(request['domain'])>253
            or (request['action']=='clear' and request['domain']!='')
            or (request['action']=='logout' and not request['domain'])
            or type(request['expires']) is not int or type(request['boot']) is not str):
        return {'error':'browser_request_invalid'}
    try:
        if str(UUID(request['boot']))!=request['boot']:
            raise ValueError()
    except ValueError:
        return {'error':'browser_request_invalid'}
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip() if boot is None else boot
    if request['boot']!=boot:
        return {'error':'browser_boot_changed'}
    journal.mkdir(parents=True,exist_ok=True,mode=0o700)
    fingerprint = hashlib.sha256(json.dumps(request,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    with (journal/'lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return {'error':'browser_busy'}
        path = journal/(request['id']+'.json')
        if path.exists():
            receipt = json.loads(path.read_text())
            if receipt['fingerprint']!=fingerprint:
                return {'error':'browser_request_conflict'}
            return receipt['result'] if receipt['state']=='done' else {'error':'browser_action_unknown'}
        if not clock()<request['expires']<=clock()+120:
            return {'error':'browser_request_expired'}
        save(path,{'fingerprint':fingerprint,'state':'started'})
        try:
            async with asyncio.timeout(30):
                result = await perform(request)
        except Exception:
            result = {'error':'browser_action_unknown'}
        # Cancellation/process death leaves started; a later call never repeats it.
        save(path,{'fingerprint':fingerprint,'state':'done','result':result})
        return result


def uses_profile(pid, profile):
    try:
        args = (Path('/proc')/str(pid)/'cmdline').read_bytes().split(b'\0')
    except (FileNotFoundError,ProcessLookupError):
        return False
    return ('--user-data-dir='+str(profile)).encode() in args and not any(arg.startswith(b'--type=') for arg in args)


def processes(profile):
    return [int(item.name) for item in Path('/proc').iterdir() if item.name.isdigit() and uses_profile(int(item.name),profile)]


async def stop_browser(profile):
    for pid in processes(profile):
        try:
            descriptor = os.pidfd_open(pid)
        except ProcessLookupError:
            continue
        try:
            if uses_profile(pid,profile):
                try:
                    signal.pidfd_send_signal(descriptor,signal.SIGTERM)
                except ProcessLookupError:
                    pass
        finally:
            os.close(descriptor)
    async with asyncio.timeout(5):
        while processes(profile):
            await asyncio.sleep(.1)


async def clear_profile(request, *, profile=PROFILE):
    if profile.resolve()!=profile.absolute():
        raise ValueError('browser_profile_symlink')
    await stop_browser(profile)
    if profile.exists():
        trash = profile.parent/('.browser-cleared-'+request['id'])
        if trash.exists():
            raise ValueError('browser_cleanup_collision')
        # Detach the old profile first. Removing it must not delete a new profile
        # another user-approved job creates after this operation's rename.
        profile.rename(trash)
        shutil.rmtree(trash)
    return {'cleared':True}
