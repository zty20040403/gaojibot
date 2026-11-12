"""Bind the account runtime's authenticated endpoints to owned conversation VMs."""
import asyncio

from ...background_tools import workspace_owner
from ...platform_types import EndpointKey
from .lease import assert_epoch
from .vm_api import VMAPIAuthority
from .vm_credentials import VMCredentials
from .vm_sessions import VMSessions


class OfficialVMLifecycle:
    def __init__(self, *, transaction, lease, private_admission, manager, calls, gateway, logger, clock):
        self.transaction,self.lease,self.manager,self.calls = transaction,lease,manager,calls
        self.gateway,self.logger = gateway,logger
        self.store = VMSessions(transaction,lease,private_admission=private_admission,clock=clock)
        self.credentials = VMCredentials(self.store,manager,calls,port=gateway.port)
        self.api = VMAPIAuthority(self.credentials)
        self.closed,self.restoring = False,None

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
                    return endpoint,(row['generation'],row['route_epoch'])
        raise PermissionError('vm_endpoint_unavailable')

    async def ready(self, owner, sandbox_id):
        if self.closed:
            raise PermissionError('vm_service_closed')
        endpoint,route = await self.calls(self.route,owner)
        current = await self.credentials.prepare(endpoint,sandbox_id,expected_route=route)
        self.api.rotated(current)
        return current

    async def stopping(self, owner, sandbox_id):
        self.api.revoke(sandbox_id)
        await self.calls(self.store.revoke_vm,sandbox_id)

    async def start(self):
        if getattr(self.manager,'conversation_lifecycle',None) not in (None,self):
            raise RuntimeError('vm_lifecycle_already_attached')
        self.manager.conversation_lifecycle = self
        self.gateway.vm_authority = self.api
        await self.gateway.start()
        self.restoring = asyncio.create_task(self.restore())

    async def restore(self):
        # Restore running VMs without blocking QQ startup on one broken guest.
        # Booting retained units is added with the resident-program keeper.
        try:
            items = await self.manager._all_metadata()
            for item in items:
                owner,sid = str(item.get('owner_ref','')),str(item['id'])
                if not owner.startswith('qqo:conversation:'):
                    continue
                try:
                    if await self.manager._running(sid):
                        await self.ready(owner,sid)
                except Exception as error:
                    self.logger.warning('VM credential restore failed vm=%s error=%s',sid,type(error).__name__)
        except Exception as error:
            self.logger.warning('VM credential scan failed error=%s',type(error).__name__)

    async def close(self):
        self.closed = True
        if getattr(self.manager,'conversation_lifecycle',None) is self:
            self.manager.conversation_lifecycle = None
        if self.restoring is not None:
            self.restoring.cancel()
            await asyncio.gather(self.restoring,return_exceptions=True)
        await self.api.close()
