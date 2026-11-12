"""Host-owned guest-agent provisioning, independent of any human/job identity."""
import asyncio
import json
from pathlib import Path
import re
from uuid import UUID

from ... import sandbox_identity
from ...background_tools import workspace_owner


class VMCredentials:
    def __init__(self, store, manager, calls, *, port):
        if type(port) is not int or not 1<=port<65535:
            raise ValueError('invalid_vm_gateway_port')
        self.store,self.manager,self.calls = store,manager,calls
        self.base_url = f'http://10.0.2.2:{port}'
        self.script = Path(sandbox_identity.__file__).read_text()

    async def _guest(self, sandbox_id, payload):
        try:
            code,out,_ = await self.manager._guest_exec(sandbox_id,'/usr/bin/python3',['-c',self.script],
                input_bytes=json.dumps(payload).encode(),timeout=30)
        except Exception:
            raise PermissionError('vm_identity_unavailable') from None
        try:
            if code or len(out)>4096:
                raise ValueError()
            value = json.loads(out)
            if (type(value) is not dict or set(value)!={'boot_id','token_hash','base_url'}
                    or str(UUID(value['boot_id']))!=value['boot_id']
                    or value['token_hash'] is not None and re.fullmatch('[0-9a-f]{64}',value['token_hash']) is None):
                raise ValueError()
            if value['base_url'] is not None:
                sandbox_identity._url(value['base_url'])
            return value
        except (ValueError,TypeError,AttributeError):
            raise PermissionError('vm_identity_unavailable') from None

    async def _inspect(self, endpoint, sandbox_id):
        await self.manager._metadata(workspace_owner(endpoint),sandbox_id)
        if not await self.manager._running(sandbox_id):
            raise PermissionError('vm_not_running')
        return await self._guest(sandbox_id,{'op':'inspect'})

    async def prepare(self, endpoint, sandbox_id, *, expected_route):
        # The manager's file lock also serializes independent host processes.
        # No database transaction is held across any guest-agent operation.
        async with self.manager.io_lock(sandbox_id):
            observed = await self._inspect(endpoint,sandbox_id)
            current = None
            if observed['base_url']==self.base_url:
                current = await self.calls(self.store.resume,endpoint,sandbox_id,observed,expected_route=expected_route)
            if current is not None:
                return current
            credential = await self.calls(self.store.issue,endpoint,sandbox_id,observed['boot_id'],expected_route=expected_route)
            try:
                installed = await self._guest(sandbox_id,dict(op='install',token=credential.token,
                    boot_id=credential.source.boot_id,base_url=self.base_url))
                if installed!=dict(boot_id=credential.source.boot_id,token_hash=credential.source.token_hash,base_url=self.base_url):
                    raise PermissionError('vm_identity_unavailable')
                await self.check(credential.source)
                return credential.source
            except (Exception,asyncio.CancelledError):
                await self.calls(self.store.discard,credential.source)
                raise

    async def authenticate(self, token):
        current = await self.calls(self.store.authenticate,token)
        # A new HTTP request proves the VM still has this boot and token. A
        # caller cannot reuse an exfiltrated token after the VM has rebooted.
        observed = await self._inspect(current.endpoint,current.sandbox_id)
        if (observed['boot_id'],observed['token_hash'])!=(current.boot_id,current.token_hash):
            await self.calls(self.store.discard,current)
            raise PermissionError('vm_boot_replaced')
        await self.check(current)
        return current

    async def check(self, current):
        # Streaming/action boundaries use this cheap persistent check; boot
        # rotation and lifecycle revocation invalidate already admitted calls.
        def run():
            with self.store.transaction() as cursor:
                return self.store.check(cursor,current)
        return await self.calls(run)
