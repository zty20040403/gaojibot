"""Revocable VM API scopes; no model relay, Codex tunnel or human authority."""
import asyncio

from ...codex_gateway import RequestScope


class VMAPIAuthority:
    def __init__(self, credentials):
        self.credentials = credentials
        self.scopes = {}
        self.authenticating = set()
        self.retired = set()
        self.closed = False
        self.monitor = None

    async def authorize(self, token):
        if self.closed:
            raise PermissionError('vm_service_closed')
        task = asyncio.current_task()
        self.authenticating.add(task)
        try:
            current = await self.credentials.authenticate(token)
            if self.closed:
                raise PermissionError('vm_service_closed')
            # Only the digest-bearing source is retained, never the raw token.
            self.rotated(current)
            scope = self.scopes.get(current)
            if scope is None or scope.revoked:
                if scope is not None:
                    self._retire(scope)
                async def check():
                    await self.credentials.check(current)
                scope = self.scopes[current] = RequestScope(source={'endpoint_key':current.endpoint.key},
                    current=check,caller_ref=current.caller_ref)
            if self.monitor is None:
                self.monitor = asyncio.create_task(self._monitor())
            return scope
        finally:
            self.authenticating.discard(task)

    def _retire(self, scope):
        for task in scope.requests:
            self.retired.add(task)
            task.add_done_callback(self.retired.discard)
        scope.close()

    def rotated(self, current):
        for previous in tuple(self.scopes):
            if previous.endpoint==current.endpoint and previous!=current:
                self._retire(self.scopes.pop(previous))

    def revoke(self, sandbox_id):
        for current in tuple(self.scopes):
            if current.sandbox_id==sandbox_id:
                self._retire(self.scopes.pop(current))

    async def _monitor(self):
        while True:
            # Revocation also interrupts an upstream still waiting for headers.
            for current,scope in tuple(self.scopes.items()):
                if not scope.requests or scope.revoked:
                    continue
                try:
                    await self.credentials.check(current)
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
