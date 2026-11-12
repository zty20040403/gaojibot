"""VM-scoped requests run one fixed root guest helper, never a host shell."""
import asyncio
import json
from pathlib import Path
import re

from starlette.responses import JSONResponse

from ... import sandbox_units
from ...background_tools import workspace_owner
from .program_actions import ProgramDenied


class ProgramUnits:
    def __init__(self, lifecycle, logger):
        self.sessions,self.manager,self.calls = lifecycle.store,lifecycle.manager,lifecycle.calls
        self.logger,self.active,self.tasks = logger,set(),{}
        self.script = Path(sandbox_units.__file__).read_text()

    def cancel(self, sid):
        task = self.tasks.get(sid)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def policy(self, current):
        with self.sessions.transaction() as cursor:
            self.sessions.check(cursor,current)
            row = cursor.execute('SELECT programs_enabled FROM conversation_settings WHERE scope_key=?',
                (current.scope_key,)).fetchone()
            disabled = cursor.execute('SELECT program_name FROM program_controls WHERE scope_key=? AND NOT enabled LIMIT 257',
                (current.scope_key,)).fetchall()
            if len(disabled)>256:
                raise ProgramDenied('program_unit_policy_too_large',503)
            return dict(enabled=row['programs_enabled'] if row else True,disabled=[item['program_name'] for item in disabled])

    async def sync(self, current, *, automatic=False):
        sid = current.sandbox_id
        if sid in self.active or len(self.active)>=4:
            raise ProgramDenied('program_units_busy',429)
        self.active.add(sid)
        self.tasks[sid] = asyncio.current_task()
        admitted = False
        try:
            policy = await self.calls(self.policy,current)
            owner = workspace_owner(current.endpoint)
            await self.manager._metadata(owner,sid)
            if not await self.manager._running(sid):
                raise PermissionError('vm_not_running')
            allowed,reason = await self.manager.program_state.admit(owner,sid,current.boot_id,enabled=policy['enabled'],automatic=automatic)
            admitted = True
            effective = dict(policy,enabled=allowed)
            # Do not acquire manager.io_lock here: the sandbox user can be
            # waiting for this response inside an exec already holding it.
            # The guest reconciler has its own file lock and bounded commands.
            async with asyncio.timeout(110):
                code,out,_ = await self.manager._guest_exec(sid,'/usr/bin/timeout',[
                    '--signal=TERM','--kill-after=5s','90','/usr/bin/python3','-c',self.script,'--policy-stdin'],
                    input_bytes=json.dumps(effective).encode(),timeout=100)
            if await self.calls(self.policy,current)!=policy:
                raise ProgramDenied('program_unit_policy_changed',409)
            try:
                if code not in (0,1) or len(out)>256*1024:
                    raise ValueError()
                report = json.loads(out)
                if type(report) is not dict or type(report.get('ok')) is not bool or code!=int(not report['ok']):
                    raise ValueError()
                if 'error' in report and type(report['error']) is str:
                    report = dict(ok=False,units=[],errors={'_sync':report['error'][:4000]})
                if (set(report)!={'ok','units','errors'} or type(report['units']) is not list or len(report['units'])>32
                        or type(report['errors']) is not dict or len(report['errors'])>64):
                    raise ValueError()
            except (ValueError,TypeError):
                raise ProgramDenied('program_unit_result_unavailable',502) from None
            # Logs are untrusted VM content. Do not leak this VM capability if
            # a unit printed its environment; host credentials never go there.
            report = json.loads(re.sub(r'gvm_[A-Za-z0-9_-]{43}','[VM_TOKEN]',json.dumps(report)))
            state = await self.manager.program_state.report(owner,sid,current.boot_id,report,allowed=allowed)
            if reason not in ('','programs_disabled') and state.get('wanted'):
                report = dict(report,ok=False,errors={**report['errors'],'_host':reason})
            return report
        except Exception:
            if admitted:
                try:
                    await self.manager.program_state.report(owner,sid,current.boot_id,
                        dict(ok=False,units=[],errors={'_sync':'program_sync_unavailable'}),allowed=allowed)
                except Exception as error:
                    self.logger.warning('Program unit state failed error=%s',type(error).__name__)
            raise
        finally:
            self.active.discard(sid)
            self.tasks.pop(sid,None)

    async def handle(self, request):
        try:
            body = bytearray()
            async with asyncio.timeout(10):
                async for chunk in request.stream():
                    if len(body)+len(chunk)>256:
                        return JSONResponse({'error':'invalid_unit_request'},400)
                    body.extend(chunk)
            if request.query_params or json.loads(body)!= {}:
                raise ValueError()
            scope = request.state.channel
            await scope.current()
            result = await self.sync(scope.identity)
            return JSONResponse(result,200 if result['ok'] else 409)
        except ProgramDenied as error:
            return JSONResponse({'error':str(error)},error.status)
        except PermissionError:
            return JSONResponse({'error':'vm_source_revoked'},403)
        except (ValueError,TypeError):
            return JSONResponse({'error':'invalid_unit_request'},400)
        except Exception as error:
            self.logger.warning('Program unit sync failed error=%s',type(error).__name__)
            return JSONResponse({'error':'program_unit_sync_unavailable'},503)
