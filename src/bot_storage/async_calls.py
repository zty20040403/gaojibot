"""Bound synchronous store work without cancelling an in-flight transaction."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


class DatabaseCalls:
    """One application's async boundary; each callback owns a whole store call.

    Stores must provide their own thread safety. Never pass a live cursor or an
    async callback: a transaction must open and finish in the same invocation.
    """

    def __init__(self, concurrency: int = 4) -> None:
        if concurrency < 1:
            raise ValueError("database concurrency must be positive")
        self._slots = asyncio.Semaphore(concurrency)
        self._pending: set[asyncio.Task[Any]] = set()
        self._closed = False

    async def __call__(
        self, function: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
    ) -> T:
        if self._closed:
            raise RuntimeError("Database calls are closed")
        async with self._slots:
            if self._closed:
                raise RuntimeError("Database calls are closed")
            work = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
            self._pending.add(work)
            try:
                return await asyncio.shield(work)
            except asyncio.CancelledError:
                # Cancelling to_thread cannot stop a database write. Hold the
                # slot until it finishes, including repeated cancellation.
                while not work.done():
                    try:
                        await asyncio.shield(work)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not work.cancelled():
                    work.exception()
                raise
            finally:
                self._pending.discard(work)

    async def close(self) -> None:
        """Reject new work and drain writes before the owner closes its stores."""
        self._closed = True
        if self._pending:
            await asyncio.gather(
                *(asyncio.shield(work) for work in tuple(self._pending)),
                return_exceptions=True,
            )
