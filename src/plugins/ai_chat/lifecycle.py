from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol


class LifecycleLogger(Protocol):
    def error(self, message: object, *args: object, **kwargs: object) -> object: ...


TaskFactory = Callable[[], Awaitable[None]]


class BackgroundTaskSupervisor:
    """Owns long-running plugin tasks and gives shutdown one drain point."""

    def __init__(
        self,
        logger: LifecycleLogger,
        *,
        restart_delay_seconds: float = 1.0,
        max_restart_delay_seconds: float = 60.0,
    ) -> None:
        self._logger = logger
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._failures: dict[str, str] = {}
        self._retrying: set[str] = set()
        self._stopping = False
        self._restart_delay = max(restart_delay_seconds, 0.01)
        self._max_restart_delay = max(max_restart_delay_seconds, self._restart_delay)

    def start(self, name: str, factory: TaskFactory) -> bool:
        if self._stopping:
            return False
        current = self._tasks.get(name)
        if current is not None and not current.done():
            return False

        self._failures.pop(name, None)
        task = asyncio.create_task(self._run(name, factory), name=f"ai-chat:{name}")
        self._tasks[name] = task
        task.add_done_callback(
            lambda completed, task_name=name: self._on_done(
                task_name,
                completed,
            )
        )
        return True

    def running(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, task in self._tasks.items()
            if not task.done() and name not in self._retrying
        )

    def failures(self) -> dict[str, str]:
        return dict(self._failures)

    async def stop_all(self) -> int:
        self._stopping = True
        active = [task for task in self._tasks.values() if not task.done()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        self._tasks.clear()
        self._retrying.clear()
        return len(active)

    async def _run(self, name: str, factory: TaskFactory) -> None:
        delay = self._restart_delay
        loop = asyncio.get_running_loop()
        try:
            while not self._stopping:
                started_at = loop.time()
                try:
                    await factory()
                    raise RuntimeError("long-running worker returned unexpectedly")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if loop.time() - started_at >= self._max_restart_delay:
                        delay = self._restart_delay
                    self._failures[name] = str(exc)
                    self._retrying.add(name)
                    self._logger.error(
                        f"Background task {name!r} stopped unexpectedly: {exc}; "
                        f"restarting in {delay:g}s."
                    )
                    await asyncio.sleep(delay)
                    self._retrying.discard(name)
                    delay = min(delay * 2, self._max_restart_delay)
        finally:
            self._retrying.discard(name)

    def _on_done(self, name: str, task: asyncio.Task[None]) -> None:
        owns_slot = self._tasks.get(name) is task
        if owns_slot:
            self._tasks.pop(name, None)
        if task.cancelled():
            return
        try:
            failure = task.exception()
        except asyncio.CancelledError:
            return
        if failure is not None and owns_slot:
            self._failures[name] = str(failure)
            self._logger.error(
                f"Background task {name!r} stopped unexpectedly: {failure}"
            )
