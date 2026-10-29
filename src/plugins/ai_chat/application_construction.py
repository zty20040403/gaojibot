"""Resource ownership until a shared AppContext has been fully constructed."""
from __future__ import annotations

import asyncio
import inspect
from typing import Any


class ConstructionResources:
    """An allocation ledger, never a runtime or an authority grant.

    Production hosts await failure cleanup on the same event loop. Successful
    construction transfers ownership to the existing AppContext.shutdown.
    """

    def __init__(self, logger: Any) -> None:
        self.logger = logger
        self._resources: list[Any] = []
        self._retired: list[Any] = []

    def own(self, resource):
        if callable(getattr(resource, "close", None)) and not any(
            item is resource for item in self._resources
        ):
            self._resources.append(resource)
        return resource

    def forget(self, *resources) -> None:
        self._resources = [
            item for item in self._resources
            if not any(item is resource for resource in resources)
        ]
        self._retired = [
            item for item in self._retired
            if not any(item is resource for resource in resources)
        ]

    def retire(self, *resources) -> None:
        """Optional service failed; these allocations have no final owner."""
        for resource in resources:
            if (any(item is resource for item in self._resources)
                    and not any(item is resource for item in self._retired)):
                self._retired.append(resource)

    async def close_retired(self) -> None:
        while self._retired:
            resource = self._retired[0]
            try:
                result = resource.close()
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                # Keep ownership so complete construction rollback can retry
                # cleanup under its shield on the same event loop.
                raise
            except Exception:
                self._failed()
            self.forget(resource)

    def transfer(self) -> None:
        self._resources.clear()
        self._retired.clear()

    def _failed(self) -> None:
        try:
            self.logger.warning("Application construction cleanup failed.")
        except Exception:
            pass

    def close_one(self, resource) -> None:
        # Existing synchronous soft-failure cleanup, retaining its exception
        # behavior while preventing a later construction rollback double-close.
        self.forget(resource)
        resource.close()

    def close_sync(self) -> None:
        """For direct synchronous callers: close only synchronous allocations.

        No task is started and no coroutine is moved to another event loop.
        Async clients allocated before a failure require build_owned_context.
        """
        pending = []
        for resource in reversed(self._resources):
            if inspect.iscoroutinefunction(resource.close):
                pending.append(resource)
                continue
            try:
                resource.close()
            except BaseException:
                self._failed()
        self._resources = list(reversed(pending))

    async def aclose(self) -> None:
        while self._resources:
            resource = self._resources.pop()
            try:
                result = resource.close()
                if inspect.isawaitable(result):
                    await result
            except BaseException:
                # We are already unwinding a construction failure. Cleanup of
                # one resource cannot replace the original control exception or
                # skip the remaining resources, especially the shared pool.
                self._failed()


async def build_owned_context(builder, *args, **kwargs):
    """Await complete rollback before propagating a construction failure."""
    resources = ConstructionResources(kwargs["logger"])
    try:
        context = builder(*args, **kwargs, _construction=resources)
        await resources.close_retired()
    except BaseException:
        cleanup = asyncio.create_task(resources.aclose())
        cancelled = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as error:
                # A second cancellation must not abandon pool/client cleanup.
                cancelled = error
        cleanup.result()
        if cancelled is not None:
            raise cancelled
        raise
    resources.transfer()
    return context
