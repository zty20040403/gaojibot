from __future__ import annotations

import asyncio
from contextvars import ContextVar
import threading
import time
import unittest

from src.bot_storage import DatabaseCalls


class DatabaseCallsTests(unittest.IsolatedAsyncioTestCase):
    async def test_result_errors_and_context_are_preserved_off_loop(self):
        calls = DatabaseCalls()
        self.addAsyncCleanup(calls.close)
        trace = ContextVar("test_db_trace", default="")
        token = trace.set("local-trace")
        self.addCleanup(trace.reset, token)
        main = threading.get_ident()

        def query(number, *, extra):
            return number + extra, trace.get(), threading.get_ident()

        result, seen_trace, thread = await calls(query, 2, extra=3)
        self.assertEqual((result, seen_trace), (5, "local-trace"))
        self.assertNotEqual(thread, main)
        error = ValueError("store failure")

        def fail():
            raise error

        with self.assertRaises(ValueError) as caught:
            await calls(fail)
        self.assertIs(caught.exception, error)
        self.assertEqual(await calls(lambda: 7), 7)

    async def test_concurrency_is_bounded_without_blocking_other_coroutines(self):
        calls = DatabaseCalls(2)
        self.addAsyncCleanup(calls.close)
        active = peak = ticks = 0
        lock = threading.Lock()

        def query(value):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.025)
            with lock:
                active -= 1
            return value

        batch = asyncio.gather(*(calls(query, n) for n in range(12)))
        while not batch.done():
            await asyncio.sleep(0.005)
            ticks += 1
        self.assertEqual(await batch, list(range(12)))
        self.assertEqual(peak, 2)
        self.assertGreater(ticks, 5)

    async def test_cancelled_write_keeps_slot_until_completion(self):
        calls = DatabaseCalls(1)
        self.addAsyncCleanup(calls.close)
        started, release = threading.Event(), threading.Event()
        order = []

        def write():
            started.set()
            if not release.wait(2):
                raise TimeoutError("test did not release write")
            order.append("written")

        task = asyncio.create_task(calls(write))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            successor = asyncio.create_task(calls(lambda: order.append("next")))
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            self.assertFalse(successor.done())
            self.assertEqual(order, [])
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await successor
        self.assertEqual(order, ["written", "next"])

    async def test_cancelled_waiter_never_runs_and_close_drains_existing_work(self):
        calls = DatabaseCalls(1)
        started, release = threading.Event(), threading.Event()
        order = []

        def write():
            started.set()
            if not release.wait(2):
                raise TimeoutError("test did not release write")
            order.append("written")

        writer = asyncio.create_task(calls(write))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            cancelled = asyncio.create_task(calls(lambda: order.append("cancelled")))
            waiting = asyncio.create_task(calls(lambda: order.append("late")))
            await asyncio.sleep(0)
            cancelled.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled
            close = asyncio.create_task(calls.close())
            await asyncio.sleep(0.01)
            self.assertFalse(close.done())
            with self.assertRaisesRegex(RuntimeError, "closed"):
                await calls(lambda: None)
        finally:
            release.set()
        await writer
        await close
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await waiting
        await calls.close()
        self.assertEqual(order, ["written"])

    async def test_cancellation_wins_over_late_store_failure_without_leaking_slot(self):
        calls = DatabaseCalls(1)
        started, release = threading.Event(), threading.Event()

        def fail():
            started.set()
            release.wait(2)
            raise ValueError("late database error")

        task = asyncio.create_task(calls(fail))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            task.cancel()
            await asyncio.sleep(0)
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(await calls(lambda: "ready"), "ready")
        await calls.close()

    def test_invalid_concurrency_is_rejected(self):
        with self.assertRaises(ValueError):
            DatabaseCalls(0)
