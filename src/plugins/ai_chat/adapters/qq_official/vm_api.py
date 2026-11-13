"""Revocable VM API scopes; no model relay, Codex tunnel or human authority."""
import asyncio
from dataclasses import dataclass
from starlette.exceptions import HTTPException

from ...codex_gateway import RequestScope
from .program_actions import ProgramDenied
from .vm_sessions import VMSource


@dataclass(frozen=True)
class VMRequestIdentity:
    source: VMSource
    generation: int | tuple[int,int] | None = None
    operation: str = 'read'


class VMAPIAuthority:
    def __init__(self, credentials, *, actions=None, commands=None):
        self.credentials,self.actions,self.commands = credentials,actions,commands
        self.scopes = {}
        self.authenticating = set()
        self.retired = set()
        self.closed = False
        self.monitor = None

    async def authorize(self, token, *, write=False):
        if self.closed:
            raise PermissionError('vm_service_closed')
        task = asyncio.current_task()
        self.authenticating.add(task)
        try:
            operation = 'command' if write=='command' else 'write' if write else 'read'
            actions = self.commands if operation=='command' else self.actions
            if write:
                if actions is None:
                    raise PermissionError('vm_actions_unavailable')
                current,generation = await self.credentials.authenticate_action(token,actions)
            else:
                current,generation = await self.credentials.authenticate(token),None
            if self.closed:
                raise PermissionError('vm_service_closed')
            # Only the digest-bearing source is retained, never the raw token.
            self.rotated(current,generation=generation,operation=operation)
            identity = VMRequestIdentity(current,generation,operation)
            scope = self.scopes.get(identity)
            if scope is None or scope.revoked:
                if scope is not None:
                    self._retire(scope)
                async def check():
                    if generation is None:
                        await self.credentials.check(current)
                    else:
                        await self.credentials.calls(actions.require_generation,current,generation)
                scope = self.scopes[identity] = RequestScope(source={'endpoint_key':current.endpoint.key},
                    current=check,caller_ref=current.caller_ref,identity=current,generation=generation)
            if self.monitor is None:
                self.monitor = asyncio.create_task(self._monitor())
            return scope
        except ProgramDenied as error:
            raise HTTPException(error.status,str(error)) from None
        finally:
            self.authenticating.discard(task)

    def _retire(self, scope):
        for task in scope.requests:
            self.retired.add(task)
            task.add_done_callback(self.retired.discard)
        scope.close()

    def rotated(self, current, *, generation=None, operation='read'):
        for previous in tuple(self.scopes):
            if previous.source.endpoint==current.endpoint and (previous.source!=current or (
                    previous.operation==operation and generation is not None and previous.generation!=generation)):
                self._retire(self.scopes.pop(previous))

    def revoke(self, sandbox_id):
        for current in tuple(self.scopes):
            if current.source.sandbox_id==sandbox_id:
                self._retire(self.scopes.pop(current))

    async def _monitor(self):
        while True:
            # Revocation also interrupts an upstream still waiting for headers.
            for scope in tuple(self.scopes.values()):
                if not scope.requests or scope.revoked:
                    continue
                try:
                    await scope.current()
                except ProgramDenied as error:
                    scope.close(status=error.status,reason=str(error))
                except Exception:
                    scope.close()
            await asyncio.sleep(0.25)

    async def close(self):
        self.closed = True
        tasks = self.authenticating | self.retired
        for scope in self.scopes.values():
            tasks.update(scope.requests)
            scope.close()
        self.scopes.clear()
        if self.monitor is not None:
            tasks.add(self.monitor)
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
