"""Host-owned resident inventory and crash circuit; no guest data grants identity."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
import stat
import time


class VMProgramState:
    def __init__(self, manager, *, limit=4, clock=time.time):
        self.manager,self.limit,self.clock = manager,max(0,min(int(limit),32)),clock
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def _guard(self):
        from src.sandbox_runtime.project_vm_io import host_lock
        async with self.lock, host_lock(self.manager.root/'.resident-programs.lock'):
            yield

    async def _load(self, owner, sid):
        metadata = await self.manager._metadata(owner,sid)
        return metadata,dict(metadata.get('programs',{}))

    @staticmethod
    def read(directory):
        try:
            fd = os.open(directory/'programs.json',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            with os.fdopen(fd,'rb') as file:
                if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                    raise ValueError('invalid_vm_program_state')
                data = file.read(524289)
        except FileNotFoundError:
            return {}
        if len(data)>524288:
            raise ValueError('vm_program_state_too_large')
        state = json.loads(data)
        if type(state) is not dict:
            raise ValueError('invalid_vm_program_state')
        return state

    def _save(self, sid, metadata, state):
        # Keep operational state separate: last_used/last_started updates often
        # span VM I/O and must not overwrite a newer resident reservation.
        self.manager._save_metadata(sid,state,filename='programs.json')

    def _failure(self, state, reason):
        now = int(self.clock())
        failures = [stamp for stamp in state.get('failures',[]) if now-1800<=stamp<=now]
        failures.append(now)
        state.update(failures=failures[-2:],running=False,starting=False,retained=False,error=reason)
        if len(failures)>=2:
            state.update(suspended=True,alert='VM 在 30 分钟内两次启动失败或非计划重启；常驻程序自动加载已暂停，需要管理员检查后恢复。')

    async def starting(self, owner, sid):
        async with self._guard():
            metadata,state = await self._load(owner,sid)
            state.update(starting=True,running=False,paused=False)
            self._save(sid,metadata,state)

    async def failed(self, owner, sid, *, reason='vm_start_failed'):
        async with self._guard():
            metadata,state = await self._load(owner,sid)
            if state.get('starting') or state.get('running'):
                self._failure(state,reason)
                self._save(sid,metadata,state)

    async def stopping(self, owner, sid):
        async with self._guard():
            metadata,state = await self._load(owner,sid)
            state.update(running=False,starting=False,retained=False,paused=True)
            self._save(sid,metadata,state)

    async def admit(self, owner, sid, boot, *, enabled):
        """Reserve before loading any units; unknown outcomes retain the slot."""
        async with self._guard():
            metadata,state = await self._load(owner,sid)
            if state.get('boot_id') not in (None,boot) and state.get('running'):
                self._failure(state,'vm_unexpected_reboot')
            retained = sorted(str(item['id']) for item in await self.manager._all_metadata()
                if item.get('programs',{}).get('retained'))
            room = retained.index(sid)<self.limit if sid in retained else len(retained)<self.limit
            reason = ('vm_programs_suspended' if state.get('suspended') else
                'programs_disabled' if not enabled else 'resident_vm_limit' if not room else '')
            state.update(boot_id=boot,running=True,starting=False,paused=False,last_seen=int(self.clock()))
            if not reason:
                state['retained'] = True
            state['hold_reason'] = reason
            self._save(sid,metadata,state)
            return not reason,reason

    async def report(self, owner, sid, boot, report, *, allowed):
        async with self._guard():
            metadata,state = await self._load(owner,sid)
            if state.get('boot_id')!=boot:
                raise PermissionError('vm_program_boot_replaced')
            known = '_sync' not in report['errors']
            if known:
                state['wanted'] = bool(report['units'] or report['errors'])
            if report['ok'] and (not state.get('wanted') or not allowed):
                state['retained'] = False
            state.update(snapshot=report,updated_at=int(self.clock()))
            self._save(sid,metadata,state)
            return dict(state)

    async def reset(self, owner, sid):
        """Only an authenticated admin action may call this, not VM HTTP."""
        async with self._guard():
            metadata,state = await self._load(owner,sid)
            state.update(failures=[],suspended=False,paused=False,error='',alert='',hold_reason='')
            self._save(sid,metadata,state)

    @staticmethod
    def keeps_awake(metadata):
        return bool(metadata.get('programs',{}).get('retained'))

    @staticmethod
    def keeps_disk(metadata):
        return bool(metadata.get('programs',{}).get('wanted') or VMProgramState.keeps_awake(metadata))
