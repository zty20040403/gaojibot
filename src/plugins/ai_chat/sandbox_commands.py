"""Root guest controller; only host-chosen command IDs reach systemd."""
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

if __package__:
    from .sandbox_command_process import STATE, OUTPUT, locked, validate, write_result
    from .sandbox_units import MARKER, directory, install, run
else:
    sys.path.insert(0,'/usr/local/share/gaoji')
    from sandbox_command_process import STATE, OUTPUT, locked, validate, write_result
    from sandbox_units import MARKER, directory, install, run

SCRIPT = '/usr/local/share/gaoji/sandbox_command_process.py'


class CommandError(RuntimeError):
    pass


def read_json(fd, name):
    try:
        file = os.open(name,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW,dir_fd=fd)
    except FileNotFoundError:
        return None
    with os.fdopen(file,'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise CommandError('invalid_command_record')
        raw = stream.read(32769)
    if len(raw)>32768:
        raise CommandError('invalid_command_record')
    return json.loads(raw)


class Commands:
    def __init__(self, *, state=STATE, output=OUTPUT, command=run, clock=time.time, boot=None,
                 runtime=Path('/run/systemd/system'), system=(Path('/etc/systemd/system'),Path('/usr/lib/systemd/system'),Path('/lib/systemd/system'))):
        self.state,self.output,self.command,self.clock = state,output,command,clock
        self.runtime,self.system = runtime,system
        self.boot = boot or (lambda:Path('/proc/sys/kernel/random/boot_id').read_text().strip())

    def resources(self):
        name = 'gaoji-commands.slice'
        if any(os.path.lexists(path/name) for path in self.system):
            raise CommandError('command_slice_conflict')
        if install(self.runtime/name,MARKER+b'[Slice]\nCPUWeight=20\nIOWeight=20\nMemoryMax=50%\n'):
            code,_ = self.command(['systemctl','daemon-reload'])
            if code:
                raise CommandError('command_slice_unavailable')

    def show(self, unit):
        code,text = self.command(['systemctl','show','--no-pager',
            '--property=LoadState,ActiveState,SubState,Result,ExecMainCode,ExecMainStatus,MainPID,ControlPID,TasksCurrent',unit])
        result = dict(line.split('=',1) for line in text.splitlines() if '=' in line)
        if result.get('LoadState') not in {'loaded','not-found'} or code and result.get('LoadState')!='not-found':
            raise CommandError('command_status_unavailable')
        return result

    def output_parts(self, identity):
        try:
            root = directory(self.output)
            try:
                file = os.open(identity+'.txt',os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW,dir_fd=root)
            finally:
                os.close(root)
        except FileNotFoundError:
            return dict(output_head='',output_tail='',output_bytes=0)
        with os.fdopen(file,'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
                raise CommandError('invalid_command_output')
            head = stream.read(2048)
            stream.seek(max(len(head),info.st_size-2048))
            tail = stream.read(2048)
        return dict(output_head=head.decode('utf-8',errors='replace'),output_tail=tail.decode('utf-8',errors='replace'),output_bytes=info.st_size)

    def report(self, fd, identity, unit):
        cached = read_json(fd,identity+'.final')
        if cached is not None:
            return cached
        stopped = read_json(fd,identity+'.stop') is not None
        result = read_json(fd,identity+'.result')
        observed = self.show(unit)
        active = observed.get('ActiveState')
        ended = result is not None or observed.get('SubState')=='exited' or active=='failed' or (active=='inactive' and observed.get('ExecMainCode') not in (None,'0'))
        if ended or stopped:
            if observed['LoadState']!='not-found':
                code,_ = self.command(['systemctl','stop',unit])
                if code:
                    raise CommandError('command_stop_unconfirmed')
                settled = self.show(unit)
                if settled['LoadState']!='not-found' and (settled.get('ActiveState') not in {'inactive','failed'}
                        or settled.get('MainPID')!='0' or settled.get('ControlPID')!='0'
                        or settled.get('TasksCurrent') not in {'0','18446744073709551615'}):
                    raise CommandError('command_stop_unconfirmed')
            confirmed = True
        else:
            confirmed = False
        if result is not None:
            state = 'cancelled' if result['error']=='command_cancelled' else 'completed' if result['exit_code']==0 and not result['error'] else 'failed'
            error = result['error']
        elif stopped:
            state,error = 'cancelled','command_cancelled'
        elif ended:
            failure = observed.get('Result')
            state = 'failed'
            error = 'command_'+failure if failure in {'timeout','oom-kill','signal','exit-code','resources'} else 'command_result_unavailable'
        elif active in {'active','activating'}:
            state,error = 'running',''
        else:
            state,error = 'ambiguous','command_start_unconfirmed'
        value = dict(state=state,stopped=confirmed,exit_code=result['exit_code'] if result else None,
            elapsed_seconds=result['elapsed_seconds'] if result else None,error=error)
        try:
            value.update(self.output_parts(identity))
        except (OSError,ValueError,CommandError):
            value.update(output_head='',output_tail='',output_bytes=0,output_error='command_output_unavailable')
        if confirmed:
            write_result(fd,identity+'.final',value)
        return value

    def handle(self, request):
        if (type(request) is not dict or request.get('op') not in {'start','status','stop'}
                or type(request.get('command_id')) is not str or not re.fullmatch(r'[0-9a-f]{32}',request['command_id'])
                or type(request.get('boot_id')) is not str):
            raise CommandError('invalid_command_request')
        operation,identity = request['op'],request['command_id']
        expected = {'op','command_id','boot_id'} | ({'program','argv','deadline_at'} if operation=='start' else set())
        if set(request)!=expected:
            raise CommandError('invalid_command_request')
        if request['boot_id']!=self.boot():
            return dict(state='cancelled',stopped=True,exit_code=None,elapsed_seconds=None,
                output_head='',output_tail='',output_bytes=0,error='vm_boot_replaced')
        payload = {key:request[key] for key in ('command_id','program','argv')} if operation=='start' else None
        if payload is not None:
            validate(payload)
            if type(request['deadline_at']) is not int or not 0<request['deadline_at']-self.clock()<=21600:
                raise CommandError('command_deadline_invalid')
        fd = directory(self.state,create=True)
        try:
            with locked(fd,identity,blocking=False):
                unit = 'gaoji-bg-'+identity+'.service'
                if operation=='stop':
                    if read_json(fd,identity+'.stop') is None:
                        write_result(fd,identity+'.stop',{'cancelled':True})
                if operation=='start':
                    existing = read_json(fd,identity+'.json')
                    if existing is not None and existing!=payload:
                        raise CommandError('command_intent_conflict')
                    if existing is None and read_json(fd,identity+'.stop') is None:
                        if self.show(unit)['LoadState']!='not-found':
                            raise CommandError('command_unit_conflict')
                        self.resources()
                        write_result(fd,identity+'.json',payload)
                        remaining = max(1,int(request['deadline_at']-self.clock()))
                        code,_ = self.command(['systemd-run','--no-block','--unit='+unit,
                            '--property=Type=exec','--property=KillMode=control-group',
                            '--property=RuntimeMaxSec='+str(remaining),'--property=TimeoutStopSec=5',
                            '--property=RemainAfterExit=yes','--property=Restart=no','--property=TasksAccounting=yes',
                            '--property=Slice=gaoji-commands.slice',
                            '--','/usr/bin/python3',SCRIPT,identity])
                        if code:
                            raise CommandError('command_start_unconfirmed')
                return self.report(fd,identity,unit)
        finally:
            os.close(fd)


def main():
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw)>65536:
            raise CommandError('command_request_too_large')
        result = dict(ok=True,**Commands().handle(json.loads(raw)))
    except Exception as error:
        result = dict(ok=False,error=str(error) if isinstance(error,CommandError) else 'command_controller_'+type(error).__name__)
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__=='__main__':
    raise SystemExit(main())
