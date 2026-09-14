from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import nonebot

nonebot.init()

from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.lifecycle import BackgroundTaskSupervisor
from src.plugins.ai_chat.runtime import build_app_context


class RecordingLogger:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def error(self, message: object, *args: object, **kwargs: object) -> None:
        self.errors.append(str(message))

    def warning(self, message: object, *args: object, **kwargs: object) -> None:
        self.warnings.append(str(message))

    def info(self, message: object, *args: object, **kwargs: object) -> None:
        self.infos.append(str(message))


class BackgroundTaskSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_is_idempotent_and_shutdown_cancels_task(self) -> None:
        logger = RecordingLogger()
        supervisor = BackgroundTaskSupervisor(logger)
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def worker() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        self.assertTrue(supervisor.start("worker", worker))
        self.assertFalse(supervisor.start("worker", worker))
        await started.wait()
        self.assertEqual(supervisor.running(), ("worker",))
        self.assertEqual(await supervisor.stop_all(), 1)
        self.assertTrue(stopped.is_set())
        self.assertEqual(supervisor.running(), ())

    async def test_unexpected_failure_is_observable(self) -> None:
        logger = RecordingLogger()
        supervisor = BackgroundTaskSupervisor(logger)

        async def worker() -> None:
            raise RuntimeError("boom")

        self.assertTrue(supervisor.start("worker", worker))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(supervisor.running(), ())
        self.assertEqual(supervisor.failures(), {"worker": "boom"})
        self.assertTrue(any("boom" in message for message in logger.errors))
        await supervisor.stop_all()

    async def test_transient_failure_restarts_without_duplicate_worker(self) -> None:
        supervisor = BackgroundTaskSupervisor(RecordingLogger(), restart_delay_seconds=0.01)
        started = asyncio.Event()
        stopped = asyncio.Event()
        attempts = 0

        async def worker():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionError("database unavailable")
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        supervisor.start("worker", worker)
        await asyncio.sleep(0)
        self.assertEqual(supervisor.running(), ())
        self.assertFalse(supervisor.start("worker", worker))
        await asyncio.wait_for(started.wait(), 1)
        self.assertEqual(attempts, 2)
        self.assertEqual(supervisor.running(), ("worker",))
        self.assertIn("database unavailable", supervisor.failures()["worker"])
        await supervisor.stop_all()
        self.assertTrue(stopped.is_set())

    async def test_normal_return_is_also_restarted(self) -> None:
        supervisor = BackgroundTaskSupervisor(RecordingLogger(), restart_delay_seconds=0.01)
        restarted = asyncio.Event()
        attempts = 0

        async def worker():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return
            restarted.set()
            await asyncio.Event().wait()

        supervisor.start("worker", worker)
        await asyncio.wait_for(restarted.wait(), 1)
        self.assertEqual(attempts, 2)
        await supervisor.stop_all()

    async def test_shutdown_during_backoff_cannot_resurrect_worker(self) -> None:
        supervisor = BackgroundTaskSupervisor(RecordingLogger(), restart_delay_seconds=0.01)
        attempts = 0

        async def worker():
            nonlocal attempts
            attempts += 1
            raise ConnectionError("offline")

        supervisor.start("worker", worker)
        await asyncio.sleep(0)
        self.assertEqual(await supervisor.stop_all(), 1)
        self.assertFalse(supervisor.start("worker", worker))
        await asyncio.sleep(0.02)
        self.assertEqual(attempts, 1)

    async def test_repeated_failure_backs_off_with_a_cap(self) -> None:
        logger = RecordingLogger()
        supervisor = BackgroundTaskSupervisor(
            logger, restart_delay_seconds=0.01, max_restart_delay_seconds=0.02,
        )
        fourth = asyncio.Event()
        attempts = 0

        async def worker():
            nonlocal attempts
            attempts += 1
            if attempts == 4:
                fourth.set()
            raise ConnectionError("offline")

        supervisor.start("worker", worker)
        await asyncio.wait_for(fourth.wait(), 1)
        self.assertIn("restarting in 0.01s", logger.errors[0])
        self.assertTrue(all("restarting in 0.02s" in item for item in logger.errors[1:]))
        await supervisor.stop_all()


class AppContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_builder_owns_core_resources_and_shutdown_is_idempotent(
        self,
    ) -> None:
        logger = RecordingLogger()
        test_settings = replace(
            settings,
            ledger_enabled=True,
            context_lifecycle_enabled=True,
            turn_journal_enabled=True,
            reminders_enabled=True,
            outbox_enabled=True,
            quota_enabled=True,
            semantic_enabled=False,
            historian_enabled=False,
            dream_enabled=False,
            matrix_enabled=False,
            imessage_enabled=False,
            mirror_routes_json="",
            browser_enabled=False,
            rich_render_enabled=False,
            media_enabled=False,
        )

        async def historian(_candidate):
            raise AssertionError("disabled historian should not run")

        async def dream(_scope, _entries, _evidence):
            raise AssertionError("disabled dream should not run")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_root = root / "project"
            (project_root / "skills").mkdir(parents=True)
            context = build_app_context(
                test_settings,
                state_dir=root / "state",
                project_root=project_root,
                logger=logger,
                historian_generator=historian,
                dream_generator=dream,
                evidence_provider=lambda _entry: "",
                started_at=123,
            )

            self.assertEqual(context.started_at, 123)
            self.assertIsNotNone(context.message_ledger)
            self.assertIsNotNone(context.context_store)
            self.assertIsNotNone(context.turn_journal)
            self.assertIsNotNone(context.reminder_store)
            self.assertIsNotNone(context.delivery_store)
            self.assertIsNotNone(context.usage_store)
            self.assertIsNone(context.historian_service)
            self.assertIsNone(context.dream_service)

            await context.shutdown()
            await context.shutdown()

        self.assertFalse(logger.errors)


if __name__ == "__main__":
    unittest.main()
