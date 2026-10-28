"""Application-owned official ingress inside the existing OneBot host.

These tests do not claim a standalone SDK-free deployment entry point (M8).
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.adapters.qq_official.config import OfficialConfig  # noqa: E402
from src.plugins.ai_chat.config import settings  # noqa: E402
from src.plugins.ai_chat.onebot_runtime import OneBotRuntime  # noqa: E402
from src.plugins.ai_chat.runtime import AppContext, build_app_context  # noqa: E402
from tests.postgres_fixture import isolated_postgres_for_test  # noqa: E402


ENABLED = {'AI_QQ_OFFICIAL_ENABLED': 'true'}
RUNTIME_FACTORY = 'src.plugins.ai_chat.adapters.qq_official.runtime.OfficialRuntime'


class OfficialAppStartTests(unittest.IsolatedAsyncioTestCase):
    def context(self, **changes):
        return SimpleNamespace(_closed=False, _closing=False, official_runtime=None, database=object(),
                               message_ledger=object(), logger=Mock(), **changes)

    async def test_disabled_hook_does_not_construct_runtime_read_credentials_or_touch_database(self):
        class DisabledContext:
            _closed = False
            _closing = False
            official_runtime = None

            @property
            def database(self):
                raise AssertionError('Disabled hook must not inspect storage')

            @property
            def message_ledger(self):
                raise AssertionError('Disabled hook must not inspect ledger')

        with (patch(RUNTIME_FACTORY) as factory,
              patch.object(OfficialConfig, 'load') as load,
              patch('src.qq_official.credentials.Credentials.from_files') as credentials,
              patch('src.plugins.ai_chat.runtime.PostgresDatabase') as database):
            self.assertFalse(await AppContext.start_official_ingress(DisabledContext(), {
                'AI_QQ_OFFICIAL_ENABLED': 'false',
                'AI_QQ_OFFICIAL_APP_ID_FILE': '/must/not/read',
                'AI_QQ_OFFICIAL_APP_SECRET_FILE': '/must/not/read',
                'AI_QQ_OFFICIAL_BIND_PEPPER_FILE': '/must/not/read',
            }))
            self.assertFalse(await AppContext.start_official_ingress(DisabledContext(), {}))
        factory.assert_not_called()
        load.assert_not_called()
        credentials.assert_not_called()
        database.assert_not_called()

    async def test_enabled_hook_shares_database_and_ledger_without_bot_and_starts_once(self):
        context = self.context()
        entered, release = asyncio.Event(), asyncio.Event()

        async def start():
            entered.set()
            await release.wait()
            return True

        official = SimpleNamespace(start=AsyncMock(side_effect=start), stop=AsyncMock())
        with (patch(RUNTIME_FACTORY, return_value=official) as factory,
              patch('nonebot.get_bots', side_effect=AssertionError('No OneBot connection is required')) as bots,
              patch('src.plugins.ai_chat.onebot_runtime.Bot', side_effect=AssertionError('No Bot instance is required')) as bot):
            first = asyncio.create_task(AppContext.start_official_ingress(context, ENABLED))
            await asyncio.wait_for(entered.wait(), 1)
            later = await asyncio.gather(*(AppContext.start_official_ingress(context, ENABLED) for _ in range(5)))
            self.assertEqual(later, [False] * 5)
            release.set()
            self.assertTrue(await first)
            self.assertFalse(await AppContext.start_official_ingress(context, ENABLED))
        factory.assert_called_once()
        self.assertTrue(factory.call_args.args[0].enabled)
        self.assertIs(factory.call_args.args[1], context.database)
        self.assertIs(factory.call_args.kwargs['ledger'], context.message_ledger)
        self.assertIs(factory.call_args.kwargs['logger'], context.logger)
        self.assertIs(context.official_runtime, official)
        official.start.assert_awaited_once()
        bots.assert_not_called()
        bot.assert_not_called()

    async def test_start_failure_stops_partial_official_resources_and_clears_handle(self):
        context = self.context()
        failure = RuntimeError('expected startup failure')
        official = SimpleNamespace(start=AsyncMock(side_effect=failure), stop=AsyncMock())
        with patch(RUNTIME_FACTORY, return_value=official):
            with self.assertRaises(RuntimeError) as error:
                await AppContext.start_official_ingress(context, ENABLED)
        self.assertIs(error.exception, failure)
        official.stop.assert_awaited_once()
        self.assertIsNone(context.official_runtime)

    async def test_cancelled_start_also_closes_official_resources(self):
        context = self.context()
        entered = asyncio.Event()

        async def start():
            entered.set()
            await asyncio.Event().wait()

        official = SimpleNamespace(start=AsyncMock(side_effect=start), stop=AsyncMock())
        with patch(RUNTIME_FACTORY, return_value=official):
            task = asyncio.create_task(AppContext.start_official_ingress(context, ENABLED))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        official.stop.assert_awaited_once()
        self.assertIsNone(context.official_runtime)

    async def test_closed_application_and_missing_durable_storage_cannot_start_runtime(self):
        context = self.context()
        context._closed = True
        with patch(RUNTIME_FACTORY) as factory:
            with self.assertRaisesRegex(RuntimeError, 'closed'):
                await AppContext.start_official_ingress(context, ENABLED)
            context._closed = False
            context.database = None
            with self.assertRaisesRegex(RuntimeError, 'durable storage'):
                await AppContext.start_official_ingress(context, ENABLED)
        factory.assert_not_called()


class OfficialAppShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_in_progress_cannot_start_a_new_official_runtime(self):
        database = isolated_postgres_for_test(self)
        configuration = replace(settings, ledger_enabled=True, semantic_enabled=False,
            historian_enabled=False, dream_enabled=False, matrix_enabled=False, imessage_enabled=False,
            mirror_routes_json='', browser_enabled=False, rich_render_enabled=False, media_enabled=False,
            archive_enabled=False)
        with TemporaryDirectory() as directory, patch('src.plugins.ai_chat.runtime.PostgresDatabase', return_value=database):
            root = Path(directory)
            (root / 'skills').mkdir()
            context = build_app_context(configuration, state_dir=root / 'state', project_root=root,
                logger=Mock(), historian_generator=AsyncMock(), dream_generator=AsyncMock(), evidence_provider=Mock())
            self.addAsyncCleanup(context.shutdown)
            official = SimpleNamespace(stop=AsyncMock())
            context.official_runtime = official
            entered, release = asyncio.Event(), asyncio.Event()

            async def stop_background():
                entered.set()
                await release.wait()
                return 0

            replacement = SimpleNamespace(start=AsyncMock(return_value=True), stop=AsyncMock())
            with (patch.object(context.background_tasks, 'stop_all', side_effect=stop_background),
                  patch(RUNTIME_FACTORY, return_value=replacement) as factory):
                shutdown = asyncio.create_task(context.shutdown())
                await asyncio.wait_for(entered.wait(), 2)
                self.assertIsNone(context.official_runtime)
                self.assertFalse(context._closed)
                try:
                    with self.assertRaisesRegex(RuntimeError, 'clos'):
                        await context.start_official_ingress(ENABLED)
                    factory.assert_not_called()
                finally:
                    release.set()
                    await asyncio.wait_for(shutdown, 5)
            official.stop.assert_awaited_once()

    async def test_real_app_stops_and_drains_official_before_shared_ledger_and_database_close(self):
        database = isolated_postgres_for_test(self)
        configuration = replace(settings, ledger_enabled=True, semantic_enabled=False,
            historian_enabled=False, dream_enabled=False, matrix_enabled=False, imessage_enabled=False,
            mirror_routes_json='', browser_enabled=False, rich_render_enabled=False, media_enabled=False,
            archive_enabled=False)
        with TemporaryDirectory() as directory, patch('src.plugins.ai_chat.runtime.PostgresDatabase', return_value=database):
            root = Path(directory)
            (root / 'skills').mkdir()
            context = build_app_context(configuration, state_dir=root / 'state', project_root=root,
                logger=Mock(), historian_generator=AsyncMock(), dream_generator=AsyncMock(), evidence_provider=Mock())
            self.addAsyncCleanup(context.shutdown)
            self.assertIs(context.database, database)
            self.assertIsNotNone(context.message_ledger)
            actions = []
            entered, release = asyncio.Event(), asyncio.Event()

            async def stop_official():
                actions.append('official.enter')
                context.message_ledger.list_scopes()
                entered.set()
                await release.wait()
                actions.append('official.drained')

            official = SimpleNamespace(start=AsyncMock(return_value=True), stop=AsyncMock(side_effect=stop_official))
            with patch(RUNTIME_FACTORY, return_value=official):
                self.assertTrue(await context.start_official_ingress(ENABLED))
            ledger_close, database_close = context.message_ledger.close, database.close

            def close_ledger():
                actions.append('ledger.close')
                ledger_close()

            def close_database():
                actions.append('database.close')
                database_close()

            with (patch.object(context.message_ledger, 'close', side_effect=close_ledger) as ledger_closed,
                  patch.object(database, 'close', side_effect=close_database) as database_closed):
                shutdown = asyncio.create_task(context.shutdown())
                await asyncio.wait_for(entered.wait(), 2)
                ledger_closed.assert_not_called()
                database_closed.assert_not_called()
                release.set()
                await asyncio.wait_for(shutdown, 5)
                await context.shutdown()
                ledger_closed.assert_called_once()
                database_closed.assert_called_once()
            official.stop.assert_awaited_once()
            self.assertLess(actions.index('official.drained'), actions.index('ledger.close'))
            self.assertLess(actions.index('ledger.close'), actions.index('database.close'))
            self.assertIsNone(context.official_runtime)
            self.assertTrue(context._closed)


class OfficialOneBotStartupHookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = self.enterContext(TemporaryDirectory())
        self.root = Path(temporary)
        self.order = []

        async def start_official():
            self.order.append('official.start')
            return True

        async def start_workers(*args):
            self.order.append('onebot.workers')

        self.context = SimpleNamespace(state_dir=self.root, alert_store=None, alert_preferences=Mock(),
            model_catalog=object(), llm_gateway=object(), start_official_ingress=AsyncMock(side_effect=start_official),
            shutdown=AsyncMock())
        self.runtime = OneBotRuntime(settings, state_dir=self.root, cache_dir=self.root,
                                     project_root=self.root, logger=Mock())
        self.builder = self.enterContext(patch('src.plugins.ai_chat.onebot_runtime.build_app_context', return_value=self.context))
        self.enterContext(patch('src.plugins.ai_chat.onebot_runtime.assemble_handlers', return_value=SimpleNamespace()))
        self.enterContext(patch('src.plugins.ai_chat.onebot_runtime.AlertNotificationService'))
        self.enterContext(patch('src.plugins.ai_chat.onebot_runtime.configure_llm_runtime'))
        self.enterContext(patch('src.plugins.ai_chat.onebot_runtime.release_llm_runtime'))
        self.routes = self.enterContext(patch('src.plugins.ai_chat.onebot_runtime.register_http_surfaces'))
        self.workers = self.enterContext(patch('src.plugins.ai_chat.onebot_runtime.start_background_tasks', new=AsyncMock(side_effect=start_workers)))
        self.bots = self.enterContext(patch('nonebot.get_bots', side_effect=AssertionError('No Bot is connected')))
        self.addAsyncCleanup(self.runtime.shutdown)

    async def test_application_start_hook_runs_without_connection_event_and_only_once(self):
        application = object()
        await asyncio.gather(*(self.runtime.start(application) for _ in range(5)))
        self.builder.assert_called_once()
        self.context.start_official_ingress.assert_awaited_once_with()
        self.workers.assert_awaited_once()
        self.assertEqual(self.order, ['official.start', 'onebot.workers'])
        self.bots.assert_not_called()
        self.assertTrue(self.runtime.ready)
        self.assertIs(self.routes.call_args.args[0], application)

    async def test_failed_official_start_closes_context_and_prevents_ready_or_onebot_workers(self):
        self.context.start_official_ingress.side_effect = RuntimeError('official startup unavailable')
        with self.assertRaisesRegex(RuntimeError, 'official startup'):
            await self.runtime.start(object())
        self.context.shutdown.assert_awaited_once()
        self.workers.assert_not_awaited()
        self.assertFalse(self.runtime.ready)
        self.bots.assert_not_called()
