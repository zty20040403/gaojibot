"""Recover retained conversation VMs without holding QQ startup or DB locks."""
import asyncio


class ProgramKeeper:
    def __init__(self, lifecycle):
        self.lifecycle,self.manager = lifecycle,lifecycle.manager
        self.lock = asyncio.Lock()
        self.task,self.loop = None,None
        self.changed = asyncio.Event()

    def enabled(self, endpoint):
        with self.lifecycle.transaction() as cursor:
            self.lifecycle.store._route(cursor,endpoint)
            row = cursor.execute('SELECT programs_enabled FROM conversation_settings WHERE scope_key=?',
                (endpoint.scope.key,)).fetchone()
            return bool(row['programs_enabled']) if row else True

    async def visit(self, owner, sid):
        try:
            async with asyncio.timeout(420):
                endpoint,_ = await self.lifecycle.calls(self.lifecycle.route,owner)
                metadata = await self.manager._metadata(owner,sid)
                state = metadata['programs']
                if await self.manager._running(sid):
                    if not state.get('paused') and not state.get('planned_stop'):
                        await self.lifecycle.ready(owner,sid,automatic=True)
                    return
                if state.get('planned_stop'):
                    await self.manager.program_state.stopped(owner,sid)
                    return
                if state.get('running') or state.get('starting'):
                    self.lifecycle.api.revoke(sid)
                    self.lifecycle.units.cancel(sid)
                    await self.lifecycle.calls(self.lifecycle.store.revoke_vm,sid)
                    await self.manager.program_state.failed(owner,sid,reason='vm_unexpected_stop')
                if (await self.lifecycle.calls(self.enabled,endpoint)
                        and await self.manager.program_state.reserve_start(owner,sid)):
                    try:
                        await self.manager.start_owned(owner,sid,automatic=True)
                    except BaseException:
                        # Capacity/definition failures before an actual boot do
                        # not count as crashes, but release the pending slot.
                        await self.manager.program_state.failed(owner,sid)
                        raise
        except Exception as error:
            self.lifecycle.logger.warning('Resident VM recovery failed vm=%s error=%s',sid,type(error).__name__)

    async def scan(self):
        async with self.lock:
            items = iter(sorted(await self.manager._all_metadata(),key=lambda item:str(item['id'])))
            async def worker():
                for item in items:
                    owner,sid = str(item.get('owner_ref','')),str(item['id'])
                    if not owner.startswith('qqo:conversation:') or self.lifecycle.closed:
                        continue
                    task = asyncio.create_task(self.visit(owner,sid))
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        # Stopping one VM cancels its sync, not this keeper.
                        if asyncio.current_task().cancelling():
                            task.cancel()
                            await asyncio.gather(task,return_exceptions=True)
                            raise
            tasks = [asyncio.create_task(worker()) for _ in range(2)]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)

    def wake(self):
        if self.loop is not None and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self.changed.set)
            except RuntimeError:
                self.lifecycle.logger.warning('Program policy committed; runtime closing, sync remains pending')

    def start(self):
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.create_task(self.run())

    async def run(self):
        while not self.lifecycle.closed:
            try:
                await asyncio.wait_for(self.changed.wait(),30)
            except TimeoutError:
                pass
            self.changed.clear()
            try:
                await self.scan()
            except Exception as error:
                self.lifecycle.logger.warning('Resident VM scan failed error=%s',type(error).__name__)

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
