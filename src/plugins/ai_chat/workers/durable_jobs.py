from __future__ import annotations

import asyncio
import inspect
import os
import socket
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, TypeAlias

from ..storage.jobs import DurableJob, DurableJobStore


DurableJobHandler: TypeAlias = Callable[
    [DurableJob],
    Awaitable[Mapping[str, Any] | None] | Mapping[str, Any] | None,
]
DurableJobCompensator: TypeAlias = Callable[
    [DurableJob, str],
    Awaitable[None] | None,
]


class JobDeferred(Exception):
    def __init__(self, reason: str, delay_seconds: int = 15):
        super().__init__(reason)
        self.delay_seconds = delay_seconds


class WorkerLogger(Protocol):
    def error(self, message: object, *args: object, **kwargs: object) -> object: ...

    def warning(self, message: object, *args: object, **kwargs: object) -> object: ...

    def info(self, message: object, *args: object, **kwargs: object) -> object: ...


class DurableJobWorker:
    def __init__(
        self,
        store: DurableJobStore,
        *,
        logger: WorkerLogger,
        poll_seconds: float = 2.0,
        concurrency: int = 2,
        worker_id: str | None = None,
        per_scope_limit: int | None = None,
    ) -> None:
        self.store = store
        self.logger = logger
        self.poll_seconds = max(float(poll_seconds), 0.1)
        self.concurrency = min(max(int(concurrency), 1), 32)
        self.per_scope_limit = per_scope_limit
        self.worker_id = worker_id or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )
        self._handlers: dict[str, DurableJobHandler] = {}
        self._timeouts: dict[str, float] = {}
        self._compensators: dict[str, DurableJobCompensator] = {}
        self._running: dict[int, asyncio.Task[Mapping[str, Any] | None]] = {}

    def register(
        self,
        kind: str,
        handler: DurableJobHandler,
        *,
        timeout_seconds: float | None = None,
        compensator: DurableJobCompensator | None = None,
    ) -> None:
        clean_kind = " ".join(str(kind).split())
        if not clean_kind:
            raise ValueError("job handler kind must not be empty")
        if clean_kind in self._handlers:
            raise ValueError(f"job handler already registered: {clean_kind}")
        self._handlers[clean_kind] = handler
        if timeout_seconds is not None:
            self._timeouts[clean_kind] = max(float(timeout_seconds), 0.1)
        if compensator is not None:
            self._compensators[clean_kind] = compensator

    @property
    def registered_kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    async def run_once(self) -> int:
        jobs = await asyncio.to_thread(
            self.store.claim_due,
            self.worker_id,
            limit=self.concurrency,
            kinds=self.registered_kinds,
            per_scope_limit=self.per_scope_limit,
        )
        if not jobs:
            return 0
        await asyncio.gather(*(self._execute(job) for job in jobs))
        return len(jobs)

    async def run_forever(self) -> None:
        pending: set[asyncio.Task[None]] = set()
        loop = asyncio.get_running_loop()
        next_claim = 0.0
        retry_delay = self.poll_seconds
        try:
            while True:
                if len(pending) < self.concurrency and loop.time() >= next_claim:
                    try:
                        jobs = await asyncio.to_thread(
                            self.store.claim_due,
                            self.worker_id,
                            limit=self.concurrency - len(pending),
                            kinds=self.registered_kinds,
                            per_scope_limit=self.per_scope_limit,
                        )
                    except Exception as exc:
                        self.logger.error(
                            f"Durable queue claim failed: {type(exc).__name__}; "
                            f"retrying in {retry_delay:g}s."
                        )
                        next_claim = loop.time() + retry_delay
                        retry_delay = min(retry_delay * 2, 60.0)
                    else:
                        retry_delay = self.poll_seconds
                        next_claim = loop.time() + (0 if jobs else self.poll_seconds)
                        pending.update(
                            asyncio.create_task(self._execute(job), name=f"durable-job:{job.job_id}")
                            for job in jobs
                        )
                if pending:
                    done, pending = await asyncio.wait(
                        pending, timeout=self.poll_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in done:
                        if task.cancelled():
                            continue
                        try:
                            task.result()
                        except Exception as exc:
                            # The durable lease/checkpoint, not a blind replay, owns recovery.
                            self.logger.error(
                                f"{task.get_name()} settlement failed: {type(exc).__name__}; "
                                "retained for lease recovery."
                            )
                else:
                    await asyncio.sleep(max(next_claim - loop.time(), 0.1))
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def cancel(self, job_id: int) -> bool:
        changed = self.store.cancel(job_id)
        task = self._running.get(int(job_id))
        if task is not None and not task.done():
            task.cancel()
        return changed

    async def _execute(self, job: DurableJob) -> None:
        handler = self._handlers.get(job.kind)
        if handler is None:
            await asyncio.to_thread(
                self.store.mark_failed,
                job.job_id,
                self.worker_id,
                f"no handler registered for {job.kind}",
                retryable=False,
            )
            self.logger.error(f"Durable job {job.handle} has no handler: {job.kind}")
            return
        handler_task = asyncio.create_task(
            self._invoke(handler, job),
            name=f"durable-job-handler:{job.job_id}",
        )
        self._running[job.job_id] = handler_task
        heartbeat = asyncio.create_task(
            self._heartbeat(job, handler_task),
            name=f"durable-job-heartbeat:{job.job_id}",
        )
        try:
            timeout = self._timeouts.get(job.kind)
            result = (
                await asyncio.wait_for(handler_task, timeout=timeout)
                if timeout is not None
                else await handler_task
            )
            safe_result = dict(result or {})
        except JobDeferred as exc:
            await asyncio.to_thread(self.store.defer, job, delay_seconds=exc.delay_seconds, reason=str(exc))
        except TimeoutError:
            await self._compensate(job, "timeout")
            changed = await asyncio.to_thread(
                self.store.mark_failed,
                job.job_id,
                self.worker_id,
                f"job exceeded {self._timeouts[job.kind]:g} seconds",
                retryable=False,
            )
            if changed:
                self.logger.warning(f"Durable job {job.handle} timed out.")
        except asyncio.CancelledError:
            try:
                current = await asyncio.to_thread(self.store.get, job.job_id)
                if current is not None and current.status == "cancelled":
                    await self._compensate(job, "cancelled")
                    self.logger.info(f"Durable job {job.handle} was cancelled.")
                    return
                await asyncio.to_thread(
                    self.store.defer, job, delay_seconds=0,
                    reason="worker stopped or could not confirm its lease",
                )
            except Exception as exc:
                self.logger.warning(
                    f"Durable job {job.handle} could not release its lease: "
                    f"{type(exc).__name__}; waiting for lease expiry."
                )
            raise
        except Exception as exc:
            retryable = job.attempts < job.max_attempts
            if not retryable:
                await self._compensate(job, "failed")
            changed = await asyncio.to_thread(
                self.store.mark_failed,
                job.job_id,
                self.worker_id,
                str(exc),
                retryable=retryable,
                retry_delay_seconds=min(10 * (2 ** max(job.attempts - 1, 0)), 300),
            )
            if changed:
                self.logger.warning(
                    f"Durable job {job.handle} failed on attempt {job.attempts}: {exc}"
                )
        else:
            # A lost success acknowledgement is not proof that the handler failed.
            changed = await asyncio.to_thread(
                self.store.mark_succeeded, job.job_id, self.worker_id,
                result=safe_result,
            )
            if not changed:
                self.logger.warning(
                    f"Durable job {job.handle} finished after losing its lease."
                )
        finally:
            self._running.pop(job.job_id, None)
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    async def _invoke(
        self,
        handler: DurableJobHandler,
        job: DurableJob,
    ) -> Mapping[str, Any] | None:
        result = handler(job)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def _compensate(self, job: DurableJob, reason: str) -> None:
        compensator = self._compensators.get(job.kind)
        if compensator is None:
            return
        try:
            result = compensator(job, reason)
            if inspect.isawaitable(result):
                await asyncio.shield(result)
        except Exception as exc:
            self.logger.error(
                f"Durable job {job.handle} compensation failed: {exc}"
            )

    async def _heartbeat(
        self,
        job: DurableJob,
        handler_task: asyncio.Task[Mapping[str, Any] | None],
    ) -> None:
        interval = min(max(self.store.lease_seconds / 3, 1.0), 2.0)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await asyncio.to_thread(
                    self.store.renew_lease, job.job_id, self.worker_id,
                )
            except Exception as exc:
                self.logger.warning(
                    f"Durable job {job.handle} lease renewal failed: "
                    f"{type(exc).__name__}; stopping the handler."
                )
                renewed = False
            if not renewed:
                if not handler_task.done():
                    handler_task.cancel()
                return
