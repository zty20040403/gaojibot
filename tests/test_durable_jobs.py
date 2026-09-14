from __future__ import annotations

import asyncio
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker


class RecordingLogger:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, message: object, *args, **kwargs) -> None:
        self.errors.append(str(message))

    def warning(self, message: object, *args, **kwargs) -> None:
        self.warnings.append(str(message))

    def info(self, message: object, *args, **kwargs) -> None:
        return None


class DurableJobStoreTests(unittest.TestCase):
    def test_idempotent_enqueue_claim_and_restart_recovery(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.sqlite3"
            store = DurableJobStore(path, lease_seconds=10)
            started_at = int(time.time())
            first, created = store.enqueue(
                kind="test.echo",
                idempotency_key="same-operation",
                payload={"value": 7},
                now=started_at,
            )
            duplicate, duplicate_created = store.enqueue(
                kind="test.echo",
                idempotency_key="same-operation",
                payload={"value": 99},
                now=started_at + 1,
            )
            self.assertTrue(created)
            self.assertFalse(duplicate_created)
            self.assertEqual(duplicate.job_id, first.job_id)
            self.assertEqual(duplicate.payload, {"value": 7})

            claimed = store.claim_due("worker-a", now=started_at)
            self.assertEqual(len(claimed), 1)
            self.assertEqual(claimed[0].attempts, 1)
            store.close()

            restarted = DurableJobStore(path, lease_seconds=10)
            self.assertEqual(restarted.recovered_jobs, 0)
            self.assertEqual(
                restarted.recover_expired_leases(now=started_at + 10),
                1,
            )
            reclaimed = restarted.claim_due("worker-b", now=started_at + 10)
            self.assertEqual(len(reclaimed), 1)
            self.assertEqual(reclaimed[0].attempts, 2)
            self.assertTrue(
                restarted.mark_succeeded(
                    reclaimed[0].job_id,
                    "worker-b",
                    result={"ok": True},
                    now=started_at + 11,
                )
            )
            self.assertEqual(restarted.stats()["succeeded"], 1)
            restarted.close()

    def test_retry_limit_cancel_and_manual_requeue(self) -> None:
        store = DurableJobStore(":memory:", default_max_attempts=2)
        job, _ = store.enqueue(
            kind="test.fail",
            idempotency_key="failure",
            now=200,
        )
        first = store.claim_due("worker", now=200)[0]
        self.assertTrue(
            store.mark_failed(
                first.job_id,
                "worker",
                "temporary",
                retry_delay_seconds=0,
                now=200,
            )
        )
        second = store.claim_due("worker", now=200)[0]
        self.assertTrue(
            store.mark_failed(
                second.job_id,
                "worker",
                "permanent",
                now=201,
            )
        )
        self.assertEqual(store.stats()["failed"], 1)
        self.assertTrue(store.requeue(job.job_id, now=202))
        self.assertTrue(store.cancel(job.job_id, now=203))
        self.assertEqual(store.stats()["cancelled"], 1)
        store.close()


class DurableJobWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def wait_for_status(self, store, job_id, status):
        async def wait():
            while store.get(job_id).status != status:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), 2)

    async def test_queue_recovers_after_claim_errors(self) -> None:
        store = DurableJobStore(":memory:")
        worker = DurableJobWorker(store, logger=RecordingLogger(), poll_seconds=0.1)
        seen = []
        worker.register("test", lambda job: seen.append(job.job_id))
        job, _ = store.enqueue(kind="test", idempotency_key="queued")
        claim = store.claim_due
        attempts = 0

        def flaky_claim(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts <= 2:
                raise ConnectionError("temporary database outage")
            return claim(*args, **kwargs)

        with patch.object(store, "claim_due", side_effect=flaky_claim):
            running = asyncio.create_task(worker.run_forever())
            try:
                await self.wait_for_status(store, job.job_id, "succeeded")
                self.assertEqual(seen, [job.job_id])
                self.assertFalse(running.done())
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
        store.close()

    async def test_one_settlement_error_does_not_stop_other_jobs(self) -> None:
        store = DurableJobStore(":memory:")
        worker = DurableJobWorker(store, logger=RecordingLogger(), concurrency=2, poll_seconds=0.1)
        first, _ = store.enqueue(kind="test", idempotency_key="broken")
        second, _ = store.enqueue(kind="test", idempotency_key="healthy")

        async def handler(job):
            if job.job_id == first.job_id:
                raise ValueError("bad job")
            await asyncio.sleep(0.05)
            return {"ok": True}

        worker.register("test", handler)
        with patch.object(store, "mark_failed", side_effect=ConnectionError("settlement unavailable")):
            running = asyncio.create_task(worker.run_forever())
            try:
                await self.wait_for_status(store, second.job_id, "succeeded")
                self.assertEqual(store.get(first.job_id).status, "running")
                self.assertFalse(running.done())
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
        store.close()

    async def test_claim_outage_does_not_cancel_a_running_job(self) -> None:
        store = DurableJobStore(":memory:")
        worker = DurableJobWorker(store, logger=RecordingLogger(), concurrency=2, poll_seconds=0.1)
        first, _ = store.enqueue(kind="test", idempotency_key="already-running")
        started, claim_failed, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        claim = store.claim_due
        loop = asyncio.get_running_loop()
        calls = 0

        def flaky_claim(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                loop.call_soon_threadsafe(claim_failed.set)
                raise ConnectionError("cannot claim more work")
            return claim(*args, **kwargs)

        async def handler(_job):
            started.set()
            await release.wait()
            return {"ok": True}

        worker.register("test", handler)
        with patch.object(store, "claim_due", side_effect=flaky_claim):
            running = asyncio.create_task(worker.run_forever())
            try:
                await asyncio.wait_for(started.wait(), 1)
                await asyncio.wait_for(claim_failed.wait(), 1)
                release.set()
                await self.wait_for_status(store, first.job_id, "succeeded")
                self.assertEqual(store.get(first.job_id).attempts, 1)
                self.assertFalse(running.done())
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
        store.close()

    async def test_lost_success_ack_is_not_marked_as_handler_failure(self) -> None:
        store = DurableJobStore(":memory:")
        worker = DurableJobWorker(store, logger=RecordingLogger())
        seen = []
        worker.register("test", lambda job: seen.append(job.job_id))
        job, _ = store.enqueue(kind="test", idempotency_key="lost-ack", resume_on_lease_loss=True)
        with patch.object(store, "mark_succeeded", side_effect=ConnectionError("lost ack")), \
                patch.object(store, "mark_failed", wraps=store.mark_failed) as mark_failed:
            with self.assertRaises(ConnectionError):
                await worker.run_once()
            mark_failed.assert_not_called()
        self.assertEqual(seen, [job.job_id])
        self.assertEqual(store.get(job.job_id).status, "running")
        store.close()

    async def test_failed_heartbeat_stops_handler_and_preserves_continuation(self) -> None:
        store = DurableJobStore(":memory:")
        worker = DurableJobWorker(store, logger=RecordingLogger())
        started, stopped = asyncio.Event(), asyncio.Event()

        async def handler(_job):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        worker.register("test", handler)
        job, _ = store.enqueue(kind="test", idempotency_key="lease", resume_on_lease_loss=True)
        claimed = store.claim_due(worker.worker_id)[0]
        with patch.object(store, "renew_lease", side_effect=ConnectionError("offline")):
            running = asyncio.create_task(worker._execute(claimed))
            await asyncio.wait_for(started.wait(), 1)
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(running, 3)
        self.assertTrue(stopped.is_set())
        self.assertEqual(store.get(job.job_id).status, "pending")
        self.assertEqual(store.get(job.job_id).attempts, 0)
        self.assertEqual(worker._running, {})
        store.close()

    async def test_registered_handler_completes_job(self) -> None:
        store = DurableJobStore(":memory:")
        logger = RecordingLogger()
        worker = DurableJobWorker(
            store,
            logger=logger,
            concurrency=1,
            worker_id="test-worker",
        )
        seen: list[int] = []

        async def handle(job):
            await asyncio.sleep(0)
            seen.append(int(job.payload["value"]))
            return {"processed": True}

        worker.register("test.echo", handle)
        store.enqueue(
            kind="test.echo",
            idempotency_key="worker-operation",
            payload={"value": 42},
        )

        self.assertEqual(await worker.run_once(), 1)
        self.assertEqual(seen, [42])
        self.assertEqual(store.stats()["succeeded"], 1)
        self.assertFalse(logger.errors)
        store.close()

    async def test_running_job_can_be_cancelled_and_compensated(self) -> None:
        store = DurableJobStore(":memory:", lease_seconds=10)
        logger = RecordingLogger()
        worker = DurableJobWorker(
            store,
            logger=logger,
            concurrency=1,
            worker_id="test-worker",
        )
        started = asyncio.Event()
        compensated: list[str] = []

        async def handle(_job):
            started.set()
            await asyncio.Event().wait()

        async def compensate(_job, reason):
            compensated.append(reason)

        worker.register("test.long", handle, compensator=compensate)
        job, _ = store.enqueue(kind="test.long", idempotency_key="long")
        running = asyncio.create_task(worker.run_once())
        await started.wait()
        self.assertTrue(worker.cancel(job.job_id))
        await running

        self.assertEqual(store.get(job.job_id).status, "cancelled")
        self.assertEqual(compensated, ["cancelled"])
        store.close()

    async def test_job_timeout_is_terminal_and_compensated(self) -> None:
        store = DurableJobStore(":memory:")
        logger = RecordingLogger()
        worker = DurableJobWorker(store, logger=logger, worker_id="test-worker")
        compensated: list[str] = []

        async def handle(_job):
            await asyncio.sleep(1)

        worker.register(
            "test.timeout",
            handle,
            timeout_seconds=0.01,
            compensator=lambda _job, reason: compensated.append(reason),
        )
        job, _ = store.enqueue(kind="test.timeout", idempotency_key="timeout")
        await worker.run_once()

        self.assertEqual(store.get(job.job_id).status, "failed")
        self.assertEqual(compensated, ["timeout"])
        store.close()
