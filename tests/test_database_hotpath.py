from __future__ import annotations

from tests.postgres_fixture import postgres_for_test

import asyncio
import gc
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import nonebot

nonebot.init()

from nonebot.adapters.onebot.v11 import Message
from nonebot.exception import FinishedException
from src.plugins.ai_chat.application import ChatTurnResult
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.memory import ConversationMemory
from src.plugins.ai_chat.media_library import MediaLibrary
from tests.handler_fixture import HandlerTestCaseMixin
from tests.test_turn_continuity import group_event


class DatabaseHotPathTests(HandlerTestCaseMixin, unittest.IsolatedAsyncioTestCase):
    async def test_media_worker_notification_from_thread_wakes_bound_waiter(self):
        library = object.__new__(MediaLibrary)
        library._wake = asyncio.Event()
        library._worker_loop = asyncio.get_running_loop()
        waiter = asyncio.create_task(library._wake.wait())
        await asyncio.sleep(0)
        try:
            await self.app_context.db_call(library._notify_worker)
            self.assertTrue(await asyncio.wait_for(waiter, 1))
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    async def test_media_worker_keeps_notification_received_during_empty_query(self):
        library = object.__new__(MediaLibrary)
        library._wake = asyncio.Event()
        library._worker_loop = None
        library._closed = False
        library.worker_concurrency = 1
        job = object()
        attempts = 0

        def claim_jobs():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                library._notify_worker()
                return []
            return [job]

        async def process_job(value):
            self.assertIs(value, job)
            library._closed = True

        with (
            patch.object(library, "claim_jobs", claim_jobs),
            patch.object(library, "process_job", process_job),
        ):
            await asyncio.wait_for(library.run_forever(), 1)
        self.assertEqual(attempts, 2)
        self.assertIsNone(library._worker_loop)

    async def test_ingest_keeps_per_scope_order_during_cancellation(self):
        adapter = self.handlers.ingest_adapter
        started, release = threading.Event(), threading.Event()
        order = []

        def ingest(event):
            if event.message_id == 1:
                started.set()
                if not release.wait(2):
                    raise TimeoutError("test did not release ingest")
            order.append(event.message_id)

        first = group_event(1, Message("first"))
        second = group_event(2, Message("second"))
        other = group_event(3, Message("other")).model_copy(update={"group_id": 200})
        with patch.object(adapter, "_ingest", ingest):
            task = asyncio.create_task(adapter.ingest(first))
            successor = None
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                task.cancel()
                successor = asyncio.create_task(adapter.ingest(second))
                await asyncio.wait_for(adapter.ingest(other), 1)
                self.assertEqual(order, [3])
                self.assertFalse(successor.done())
            finally:
                release.set()
                await asyncio.gather(
                    task, *([successor] if successor else []), return_exceptions=True
                )
        self.assertTrue(task.cancelled())
        self.assertEqual(order, [3, 1, 2])
        # A retained cancelled task keeps its exception frame (and lock) alive.
        del task, successor
        await asyncio.sleep(0)
        gc.collect()
        self.assertEqual(len(adapter._ingest_locks), 0)

    async def test_cancelled_turn_creation_still_finishes_the_created_record(self):
        event = group_event(24, Message("hello"))
        started, release = threading.Event(), threading.Event()
        journal, ledger = Mock(), Mock()
        ledger.canonical_id_for_native.return_value = 1

        def start_turn(*args, **kwargs):
            started.set()
            if not release.wait(2):
                raise TimeoutError("test did not release turn creation")
            return SimpleNamespace(turn_id=55)

        journal.start_turn.side_effect = start_turn
        with (
            patch.object(self.app_context, "turn_journal", journal),
            patch.object(self.app_context, "message_ledger", ledger),
        ):
            task = asyncio.create_task(
                self.handlers.chat._run_tracked_ai(AsyncMock(), event, "hello")
            )
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                task.cancel()
                await asyncio.sleep(0)
            finally:
                release.set()
                await task
        journal.finish_turn.assert_called_once()
        self.assertEqual(journal.finish_turn.call_args.args, (55,))
        self.assertEqual(journal.finish_turn.call_args.kwargs["status"], "aborted")
        self.assertEqual(self.app_context.running_tasks.list_all(), [])

    async def assert_off_loop(self, operation, callback_owner, callback_name):
        main_thread = threading.get_ident()
        observed = []
        original = getattr(callback_owner, callback_name)

        def slow(*args, **kwargs):
            observed.append(threading.get_ident())
            time.sleep(0.025)
            return original(*args, **kwargs)

        with patch.object(callback_owner, callback_name, slow):
            result = await operation()
        self.assertTrue(observed, callback_name)
        self.assertNotIn(main_thread, observed, callback_name)
        return result

    async def test_canonical_ingest_and_profile_writes_run_off_loop(self):
        event = group_event(20, Message("hello"))
        ledger = MessageLedger(postgres_for_test(self))
        self.addCleanup(ledger.close)
        self.handlers.ingest_adapter.message_ledger = ledger
        await self.assert_off_loop(
            lambda: self.handlers.ingest.handle_canonical_ingest(event),
            ledger,
            "record_message",
        )
        await self.assert_off_loop(
            lambda: self.handlers.ingest.handle_group_activity(event),
            self.app_context.user_profiles,
            "observe",
        )

    async def test_context_read_and_reply_history_write_run_off_loop(self):
        event = group_event(21, Message("hello"))
        with patch(
            "src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools",
            AsyncMock(return_value="local fake answer"),
        ):

            async def ask():
                return await self.handlers.tools._ask_ai(
                    AsyncMock(), event, "hello", available_image_sources=[]
                )

            await self.assert_off_loop(
                ask, self.handlers.chat, "_current_group_context"
            )
            await self.assert_off_loop(ask, self.app_context.memory, "append_turn")

    async def test_delivery_attempt_is_durable_before_send_and_receipt_is_off_loop(
        self,
    ):
        event = group_event(22, Message("hello"))
        journal = Mock()
        order = []
        main = threading.get_ident()

        def started(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), main)
            time.sleep(0.025)
            order.append("durable-attempt")

        def finished(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), main)
            time.sleep(0.025)
            order.append("durable-receipt")

        class Matcher:
            @staticmethod
            async def send(message):
                self.assertEqual(order, ["durable-attempt"])
                order.append("sent")
                return {"message_id": 101}

        journal.record_send_started.side_effect = started
        journal.record_send_finished.side_effect = finished
        with (
            patch.object(self.app_context, "turn_journal", journal),
            patch.object(
                self.handlers.chat,
                "_run_tracked_ai",
                AsyncMock(return_value=ChatTurnResult(reply="done", turn_id=1)),
            ),
        ):
            with self.assertRaises(FinishedException):
                await self.handlers.replies._finish_tracked_ai(
                    Matcher, AsyncMock(), event, "hello", label="test"
                )
        self.assertEqual(order, ["durable-attempt", "sent", "durable-receipt"])

    async def test_cancellation_during_preparation_removes_running_task(self):
        event = group_event(23, Message("hello"))
        started, release = threading.Event(), threading.Event()

        def get_preference(*args):
            started.set()
            release.wait(2)
            return None

        with patch.object(
            self.app_context.model_preferences, "get_explicit", get_preference
        ):
            task = asyncio.create_task(
                self.handlers.chat._run_tracked_ai(AsyncMock(), event, "hello")
            )
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                self.assertEqual(len(self.app_context.running_tasks.list_all()), 1)
                task.cancel()
                await asyncio.sleep(0)
            finally:
                release.set()
            self.assertIsNone(await task)
        self.assertEqual(self.app_context.running_tasks.list_all(), [])

    async def test_shutdown_waits_for_cancelled_turn_cleanup_before_closing_database(
        self,
    ):
        order = []
        ready = asyncio.Event()

        async def turn():
            info = self.app_context.running_tasks.register_current(
                conversation_id="test",
                user_id=1,
                group_id=1,
                message_id=1,
                summary="test shutdown",
            )
            ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                await self.app_context.db_call(lambda: order.append("saved"))
                self.app_context.running_tasks.finish(info.task_id)

        task = asyncio.create_task(turn())
        await ready.wait()
        database = Mock()
        database.close.side_effect = lambda: order.append("closed")
        self.app_context.database = database
        await self.app_context.shutdown()
        self.assertTrue(task.cancelled())
        self.assertEqual(order, ["saved", "closed"])

    async def test_memory_writes_keep_message_pairs_and_persist_in_order(self):
        memory = ConversationMemory(100)
        sizes = []

        def save(payload):
            time.sleep(0.001)
            sizes.append(len(payload["test"]))

        with patch.object(memory._state, "save", save):
            await asyncio.gather(
                *(
                    self.app_context.db_call(memory.append_turn, "test", str(n), str(n))
                    for n in range(20)
                )
            )
        self.assertEqual(sizes, list(range(2, 41, 2)))
        history = memory.get("test")
        for n in range(0, 40, 2):
            self.assertEqual(history[n]["role"], "user")
            self.assertEqual(history[n + 1]["role"], "assistant")
            self.assertEqual(history[n]["content"], history[n + 1]["content"])
