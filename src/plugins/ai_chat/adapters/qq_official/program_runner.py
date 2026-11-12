"""Bounded, revocable program turns sharing the foreground conversation lock."""
import asyncio
from contextlib import nullcontext

from .program_actions import ProgramDenied
from .program_execution import ProgramExecution
from .program_model import ProgramModel
from .program_wakes import ProgramWakes


class ProgramRunner:
    def __init__(self, lifecycle, runtime, app):
        self.runtime,self.calls,self.logger = runtime,lifecycle.calls,app.logger
        self.wakes = ProgramWakes(lifecycle.actions)
        self.execution = ProgramExecution(self.wakes)
        self.model = ProgramModel(app,self.execution,lifecycle.credentials,self.calls)
        self.active,self.runs,self.busy = {},{},set()
        self.changed,self.closed,self.task = asyncio.Event(),False,None

    async def start(self):
        if self.task is None:
            await self.calls(self.execution.recover)
            self.task = asyncio.create_task(self._loop())

    async def submit(self, current, **fields):
        if self.closed or self.task is None or self.task.done():
            raise ProgramDenied('program_runner_unavailable',503)
        row,_ = await self.calls(self.wakes.submit,current,**fields)
        self.changed.set()
        return await self.calls(self.wakes.actions.get,current,row['action_id'])

    async def _loop(self):
        while not self.closed:
            self.changed.clear()
            try:
                await self.tick()
            except Exception as error:
                self.logger.warning('Program wake reconciliation failed error=%s',type(error).__name__)
                await asyncio.sleep(1)
            try:
                await asyncio.wait_for(self.changed.wait(),0.25)
            except TimeoutError:
                pass

    async def tick(self):
        for wake_id,run in tuple(self.runs.items()):
            task = self.active.get(wake_id)
            if task is not None and not task.done() and not task.cancelling():
                try:
                    await self.calls(self.execution.current,run)
                except Exception:
                    task.cancel()
        for row in await self.calls(self.execution.pending):
            wake_id,endpoint = row['wake_id'],row['endpoint_key']
            if wake_id in self.active:
                continue
            if row['state']=='answered' and row['reply_action_id'] is not None:
                if not await self.calls(self.execution.settle_delivery,row):
                    try:
                        await self.calls(self.execution.current,row)
                        self._send()
                    except PermissionError as error:
                        await self.calls(self.execution.finish,row,'cancelled',self._reason(error))
                continue
            if row['state']=='queued' and row['due_at']+300<=int(self.execution.clock()):
                await self.calls(self.execution.claim,wake_id)  # Records expiry without starting a model.
                continue
            lock = self.runtime.turn_locks.setdefault(endpoint,asyncio.Lock())
            if len(self.active)>=3 or endpoint in self.busy or lock.locked():
                continue
            self.busy.add(endpoint)
            task = self.active[wake_id] = asyncio.create_task(self._run(row,lock))
            task.add_done_callback(lambda task,key=wake_id,endpoint=endpoint:self._done(key,endpoint,task))

    def _send(self):
        sender = getattr(self.runtime,'sender',None)
        if sender is not None:
            sender.wake()

    @staticmethod
    def _reason(error):
        if isinstance(error,ProgramDenied):
            return str(error)
        if isinstance(error,PermissionError):
            return 'program_source_revoked'
        return 'program_wake_'+type(error).__name__.lower()

    async def _run(self, row, lock):
        run = None
        tasks = getattr(self.runtime,'conversation_tasks',None)
        try:
            with tasks.track(row['endpoint_key'],row['conversation_generation'] or 0) if tasks else nullcontext():
                async with asyncio.timeout(180),lock:
                    if row['state']=='answered':
                        run = row
                    else:
                        def claim():
                            nonlocal run
                            # DatabaseCalls drains committed writes on cancellation;
                            # retain their result even when its await is interrupted.
                            run = self.execution.claim(row['wake_id'])
                        await self.calls(claim)
                    if run is None:
                        return
                    self.runs[row['wake_id']] = run
                    if run['state']=='running':
                        answer = await self.model.ask(run)
                        await self.calls(self.execution.answer,run,answer)
                    await self.calls(self.execution.publish,run)
                    self._send()
        except asyncio.CancelledError:
            if run is not None:
                reason = 'program_wake_interrupted'
                try:
                    await self.calls(self.execution.current,run)
                except PermissionError as error:
                    reason = self._reason(error)
                await self.calls(self.execution.finish,run,'cancelled',reason)
            raise
        except Exception as error:
            self.logger.warning('Program wake failed error=%s',type(error).__name__)
            if run is not None:
                await self.calls(self.execution.finish,run,'cancelled' if isinstance(error,PermissionError) else 'failed',self._reason(error))

    def _done(self, wake_id, endpoint, task):
        self.active.pop(wake_id,None); self.runs.pop(wake_id,None); self.busy.discard(endpoint)
        if not task.cancelled() and task.exception() is not None:
            self.logger.warning('Program wake task failed error=%s',type(task.exception()).__name__)
        self.changed.set()

    async def close(self):
        self.closed = True
        tasks = [*self.active.values(),*([self.task] if self.task is not None else [])]
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
