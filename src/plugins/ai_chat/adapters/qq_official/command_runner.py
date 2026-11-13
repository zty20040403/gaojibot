"""Bounded command supervision; no model or human requester while waiting."""
import asyncio
import json
from pathlib import Path

from ... import sandbox_commands
from ...background_tools import workspace_owner
from ...platform_types import EndpointKey
from .background_commands import BackgroundCommands
from .command_execution import CommandExecution


class CommandRunner:
    def __init__(self, lifecycle):
        self.lifecycle,self.manager,self.calls,self.logger = lifecycle,lifecycle.manager,lifecycle.calls,lifecycle.logger
        self.commands = BackgroundCommands(lifecycle.store)
        self.execution = CommandExecution(self.commands,lifecycle.programs.wakes)
        self.script = Path(sandbox_commands.__file__).read_text()
        self.active = {}
        self.changed = asyncio.Event()
        self.closed,self.task = False,None

    async def visit(self, command_id):
        try:
            operation,row = await self.calls(self.execution.next,command_id)
            if operation=='notify':
                if await self.calls(self.execution.notify,command_id):
                    self.lifecycle.programs.changed.set()
                return
            async with asyncio.timeout(45):
                endpoint = EndpointKey.parse(row['endpoint_key'])
                # Do not acquire io_lock: an ordinary VM shell may be waiting
                # for its run-bg HTTP acknowledgment. Ownership + guest boot
                # checks prevent crossing replacement/stop lifecycles instead.
                await self.manager._metadata(workspace_owner(endpoint),row['sandbox_id'])
                if not await self.manager._running(row['sandbox_id']):
                    result = dict(ok=True,state='cancelled',stopped=True,exit_code=None,elapsed_seconds=None,
                        output_head='',output_tail='',output_bytes=0,error='vm_not_running')
                else:
                    request = dict(op=operation,command_id=command_id,boot_id=row['boot_id'])
                    if operation=='start':
                        await self.calls(self.execution.authorize_start,command_id)
                        request.update(program=row['program_name'],argv=json.loads(row['argv_json']),deadline_at=row['deadline_at'])
                    code,raw,_ = await self.manager._guest_exec(row['sandbox_id'],'/usr/bin/python3',['-c',self.script],
                        input_bytes=json.dumps(request).encode(),timeout=30)
                    if code or len(raw)>32768:
                        raise ValueError('command_guest_unavailable')
                    result = json.loads(raw)
                await self.calls(self.execution.record,command_id,result)
            if await self.calls(self.execution.notify,command_id):
                self.lifecycle.programs.changed.set()
        except asyncio.CancelledError:
            # guest-exec may already have started. Never free the slot or
            # pretend the VM process stopped when only host waiting stopped.
            raise
        except Exception as error:
            try:
                await self.calls(self.execution.uncertain,command_id)
            except Exception:
                pass  # Lease/DB loss leaves the existing unfinished row intact.
            self.logger.warning('Background command observation failed command=%s error=%s',command_id,type(error).__name__)

    async def tick(self):
        for command_id,task in tuple(self.active.items()):
            if task.done():
                self.active.pop(command_id)
                if not task.cancelled():
                    task.result()
        for command_id in await self.calls(self.execution.pending):
            if len(self.active)>=4:
                break
            if command_id not in self.active:
                self.active[command_id] = asyncio.create_task(self.visit(command_id))

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run())

    async def run(self):
        while not self.closed:
            self.changed.clear()
            try:
                await self.tick()
            except Exception as error:
                self.logger.warning('Background command reconciliation failed error=%s',type(error).__name__)
            try:
                await asyncio.wait_for(self.changed.wait(),2)
            except TimeoutError:
                pass

    async def close(self):
        self.closed = True
        tasks = list(self.active.values())+([self.task] if self.task is not None else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        self.active.clear()
