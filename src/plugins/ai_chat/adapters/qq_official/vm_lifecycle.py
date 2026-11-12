"""Bind the account runtime's authenticated endpoints to owned conversation VMs."""
import asyncio

from ...background_tools import workspace_owner
from ...platform_types import EndpointKey
from .lease import assert_epoch
from .vm_api import VMAPIAuthority
from .vm_credentials import VMCredentials
from .vm_sessions import VMSessions


class OfficialVMLifecycle:
    def __init__(self, *, transaction, lease, private_admission, manager, calls, gateway, logger, clock, runtime, app):
        self.transaction,self.lease,self.manager,self.calls = transaction,lease,manager,calls
        self.gateway,self.logger = gateway,logger
        self.store = VMSessions(transaction,lease,private_admission=private_admission,clock=clock)
        self.credentials = VMCredentials(self.store,manager,calls,port=gateway.port)
        from .program_actions import ProgramActions
        from .program_http import ProgramHTTP
        self.actions = ProgramActions(self.store)
        self.api = VMAPIAuthority(self.credentials,actions=self.actions)
        from .program_runner import ProgramRunner
        self.programs = ProgramRunner(self,runtime,app)
        from .program_reads import ProgramReads
        from .program_stream import ProgramStream
        from .program_files import ProgramFiles
        from .program_units import ProgramUnits
        self.units = ProgramUnits(self,logger)
        from .program_status import ProgramStatus
        self.status = ProgramStatus(transaction=self.transaction,manager=self.manager,calls=self.calls,
            clock=self.store.clock,route=self.store._route)
        self.files = ProgramFiles(self,runtime,logger)
        self.http = ProgramHTTP(self.actions,runtime,calls,logger,programs=self.programs,reads=ProgramReads(app),
            stream=ProgramStream(self.store,calls,logger),files=self.files,units=self.units)
        self.closed,self.restoring = False,None
        from .program_keeper import ProgramKeeper
        self.keeper = ProgramKeeper(self)
        from .program_admin import ProgramAdmin
        self.admin = ProgramAdmin(self,runtime.jobs)

    def route(self, owner):
        # VM metadata contains the existing hashed owner ref, not a caller-
        # supplied endpoint. Resolve it against this account's actual routes.
        with self.transaction() as cursor:
            assert_epoch(cursor,self.lease)
            rows = cursor.execute('SELECT endpoint_key,generation,route_epoch FROM qq_official_endpoints WHERE account_key=?',
                (self.lease.account_key,)).fetchall()
            for row in rows:
                endpoint = EndpointKey.parse(row['endpoint_key'])
                if workspace_owner(endpoint)==owner:
                    self.store._route(cursor,endpoint)
                    return endpoint,(row['generation'],row['route_epoch'])
        raise PermissionError('vm_endpoint_unavailable')

    async def _paused(self, owner, sandbox_id):
        state = (await self.manager._metadata(owner,sandbox_id))['programs']
        return bool(state.get('paused') or state.get('planned_stop'))

    async def ready(self, owner, sandbox_id, *, automatic=False):
        if self.closed:
            raise PermissionError('vm_service_closed')
        if automatic and await self._paused(owner,sandbox_id):
            return None
        endpoint,route = await self.calls(self.route,owner)
        current = await self.credentials.prepare(endpoint,sandbox_id,expected_route=route)
        if automatic and await self._paused(owner,sandbox_id):
            await self.calls(self.store.discard,current)
            return None
        self.api.rotated(current)
        try:
            report = await self.units.sync(current,automatic=automatic)
            if not report['ok']:
                self.logger.warning('VM program sync incomplete vm=%s',sandbox_id)
        except Exception as error:
            # A broken unit must not make the conversation VM unusable. The
            # resident reservation remains held on uncertain guest outcomes.
            self.logger.warning('VM program sync failed vm=%s error=%s',sandbox_id,type(error).__name__)
        return current

    async def starting(self, owner, sandbox_id, *, automatic=False):
        if self.closed:
            raise PermissionError('vm_service_closed')
        await self.calls(self.route,owner)
        await self.manager.program_state.starting(owner,sandbox_id,automatic=automatic)

    async def failed(self, owner, sandbox_id):
        await self.manager.program_state.failed(owner,sandbox_id)

    async def stopped(self, owner, sandbox_id):
        await self.manager.program_state.stopped(owner,sandbox_id)

    async def stopping(self, owner, sandbox_id, *, automatic=False):
        self.api.revoke(sandbox_id)
        self.units.cancel(sandbox_id)
        await self.calls(self.store.revoke_vm,sandbox_id)
        if (self.manager._directory(sandbox_id)/'metadata.json').is_file():
            await self.manager.program_state.stopping(owner,sandbox_id,paused=not automatic)

    async def start(self):
        if getattr(self.manager,'conversation_lifecycle',None) not in (None,self):
            raise RuntimeError('vm_lifecycle_already_attached')
        self.manager.conversation_lifecycle = self
        self.gateway.vm_authority = self.api
        self.gateway.vm_handler = self.http.handle
        await self.calls(self.files.recover)
        await self.programs.start()
        await self.gateway.start()
        self.restoring = asyncio.create_task(self.restore())

    async def restore(self):
        try:
            await self.keeper.scan()
        except Exception as error:
            self.logger.warning('VM credential scan failed error=%s',type(error).__name__)
        if not self.closed:
            self.keeper.start()

    async def close(self):
        self.closed = True
        if getattr(self.manager,'conversation_lifecycle',None) is self:
            self.manager.conversation_lifecycle = None
        if self.restoring is not None:
            self.restoring.cancel()
            await asyncio.gather(self.restoring,return_exceptions=True)
        await self.keeper.close()
        await self.programs.close()
        await self.api.close()
