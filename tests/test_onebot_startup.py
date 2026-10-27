from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import nonebot
from fastapi import FastAPI
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    NoticeEvent,
)
from nonebot.exception import IgnoredException

nonebot.init()

from src.plugins.ai_chat import agent_loop, matcher_handlers
from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.onebot_runtime import OneBotRuntime
from src.plugins.ai_chat.onebot_workers import start_background_tasks
from tests.handler_fixture import HandlerTestCaseMixin


class OneBotStartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = OneBotRuntime(
            settings,
            state_dir=self.root / "state",
            cache_dir=self.root / "cache",
            project_root=self.root,
            logger=Mock(),
        )
        self.context = SimpleNamespace(
            model_catalog=object(),
            llm_gateway=Mock(),
            state_dir=self.root,
            alert_store=None,
            alert_preferences=Mock(),
            shutdown=AsyncMock(),
            db_call=DatabaseCalls(),
        )
        self.handlers = SimpleNamespace(
            group_enabled=Mock(return_value=True),
            triggers=SimpleNamespace(
                _generate_historian=AsyncMock(return_value="history"),
                _generate_dream=AsyncMock(return_value=[]),
                _dream_evidence=Mock(return_value="evidence"),
            ),
        )
        self.builder = self.enterContext(
            patch(
                "src.plugins.ai_chat.onebot_runtime.build_app_context",
                return_value=self.context,
            )
        )
        self.assembler = self.enterContext(
            patch(
                "src.plugins.ai_chat.onebot_runtime.assemble_handlers",
                return_value=self.handlers,
            )
        )
        self.routes = self.enterContext(
            patch("src.plugins.ai_chat.onebot_runtime.register_http_surfaces")
        )
        self.workers = self.enterContext(
            patch(
                "src.plugins.ai_chat.onebot_runtime.start_background_tasks",
                new_callable=AsyncMock,
            )
        )
        self.enterContext(patch.object(agent_loop, "_model_catalog", None))
        self.enterContext(patch.object(agent_loop, "_llm_gateway", None))
        self.addAsyncCleanup(self.runtime.shutdown)

    async def test_construction_is_lazy_and_concurrent_start_is_idempotent(self):
        self.builder.assert_not_called()
        self.assertFalse(self.runtime.ready)
        self.assertFalse((self.root / "state").exists())
        with self.assertRaises(RuntimeError):
            _ = self.runtime.handlers
        app = object()
        await asyncio.gather(*(self.runtime.start(app) for _ in range(5)))
        self.builder.assert_called_once()
        self.assembler.assert_called_once_with(self.context)
        self.routes.assert_called_once()
        self.workers.assert_awaited_once()
        self.assertTrue(self.runtime.ready)
        self.assertIs(self.runtime.context, self.context)
        self.assertEqual(
            agent_loop._runtime(),
            (self.context.model_catalog, self.context.llm_gateway),
        )
        callbacks = self.builder.call_args.kwargs
        self.assertEqual(await callbacks["historian_generator"]("candidate"), "history")
        self.assertEqual(
            await callbacks["dream_generator"]("group", (1, 2), "source"), []
        )
        self.assertEqual(callbacks["evidence_provider"]("entry"), "evidence")
        self.handlers.triggers._generate_dream.assert_awaited_once_with(
            "group", [1, 2], "source"
        )
        await self.runtime.shutdown()
        await self.runtime.shutdown()
        self.context.shutdown.assert_awaited_once()
        self.assertFalse(self.runtime.ready)
        self.assertIsNone(agent_loop._llm_gateway)
        with self.assertRaises(RuntimeError):
            _ = self.runtime.context

    async def test_failed_start_closes_resources_and_does_not_become_ready(self):
        self.workers.side_effect = RuntimeError("recovery failed")
        with self.assertRaisesRegex(RuntimeError, "recovery failed"):
            await self.runtime.start(object())
        self.context.shutdown.assert_awaited_once()
        self.assertFalse(self.runtime.ready)
        self.assertIsNone(agent_loop._llm_gateway)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await self.runtime.start(object())
        self.builder.assert_called_once()

    async def test_build_failure_does_not_install_routes_or_start_workers(self):
        self.builder.side_effect = RuntimeError("database unavailable")
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            await self.runtime.start(object())
        self.assembler.assert_not_called()
        self.routes.assert_not_called()
        self.workers.assert_not_awaited()
        self.assertFalse(self.runtime.ready)

    async def test_shutdown_does_not_clear_a_newer_model_runtime(self):
        await self.runtime.start(object())
        newer_catalog, newer_gateway = object(), Mock()
        agent_loop.configure_llm_runtime(newer_catalog, newer_gateway)
        await self.runtime.shutdown()
        self.assertEqual(agent_loop._runtime(), (newer_catalog, newer_gateway))

    async def test_preprocessor_requires_readiness_and_handles_approval_before_history(
        self,
    ):
        bot, event = Mock(), Mock(spec=GroupMessageEvent)
        event.group_id = 100
        with self.assertRaises(IgnoredException):
            await self.runtime.preprocess(bot, event)
        self.context.mobile_authorization = object()
        await self.runtime.start(object())
        with patch(
            "src.plugins.ai_chat.onebot_runtime.handle_approval_event",
            new=AsyncMock(return_value=True),
        ):
            with self.assertRaisesRegex(IgnoredException, "authorization"):
                await self.runtime.preprocess(bot, event)
            self.handlers.group_enabled.assert_not_called()
        with patch(
            "src.plugins.ai_chat.onebot_runtime.handle_approval_event",
            new=AsyncMock(return_value=False),
        ):
            self.handlers.group_enabled.return_value = False
            with self.assertRaisesRegex(IgnoredException, "disabled"):
                await self.runtime.preprocess(bot, event)
            self.handlers.group_enabled.return_value = True
            await self.runtime.preprocess(bot, event)


class OneBotImportTests(unittest.TestCase):
    def test_import_does_not_open_databases_or_create_state_or_bind_invalid_annotations(
        self,
    ):
        with TemporaryDirectory() as directory:
            state = Path(directory) / "never-created"
            script = """
import nonebot
nonebot.init()
from types import ModuleType
from unittest.mock import patch
with patch("sqlite3.connect", side_effect=AssertionError("import opened SQLite")), patch(
    "src.bot_storage.PostgresDatabase.__init__", side_effect=AssertionError("import opened PostgreSQL")
):
    import src.plugins.ai_chat as plugin
assert not plugin._runtime.ready
assert not hasattr(plugin, "app_context")
assert isinstance(plugin.memory, ModuleType)
assert not hasattr(plugin, "handlers")
assert not plugin._runtime.state_dir.exists()
assert not plugin._runtime.cache_dir.exists()
try:
    plugin._runtime.context
except RuntimeError:
    pass
else:
    raise AssertionError("import assembled a runtime")
print("lazy import verified")
"""
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=30,
                env={
                    **os.environ,
                    "PYTHON_DOTENV_DISABLED": "1",
                    "AI_STATE_DIR": str(state),
                    "AI_CACHE_DIR": str(state / "cache"),
                    "AI_POSTGRES_DSN": "",
                },
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("lazy import verified", result.stdout)
            self.assertNotIn("NameError", result.stdout + result.stderr)
            self.assertNotIn("ForwardRef", result.stdout + result.stderr)
            self.assertFalse(state.exists())


class OneBotHttpStartupTests(HandlerTestCaseMixin, unittest.IsolatedAsyncioTestCase):
    async def test_real_assembly_registers_control_api_only_on_start_and_closes_on_exit(
        self,
    ):
        configuration = replace(
            settings,
            admin_enabled=True,
            observability_enabled=False,
            alertmanager_url="",
            alert_notify_group_id=0,
        )
        self.app_context.settings = configuration
        runtime = OneBotRuntime(
            configuration,
            state_dir=self.app_context.state_dir,
            cache_dir=self.app_context.state_dir / "cache",
            project_root=self.app_context.project_root,
            logger=self.app_context.logger,
        )
        self.addAsyncCleanup(runtime.shutdown)
        app = FastAPI()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://test"
        ) as client:
            self.assertEqual(
                (
                    await client.get(configuration.admin_path + "/api/v1/overview")
                ).status_code,
                404,
            )
            with patch(
                "src.plugins.ai_chat.onebot_runtime.build_app_context",
                return_value=self.app_context,
            ):
                await runtime.start(app)
            self.assertTrue(runtime.ready)
            self.assertEqual(
                (await client.get(configuration.admin_path)).status_code, 200
            )
            self.assertEqual(
                (
                    await client.get(configuration.admin_path + "/api/v1/overview")
                ).status_code,
                503,
            )
            self.assertIsNotNone(runtime.handlers.ingest_adapter)
            self.assertEqual(self.app_context.background_tasks.running(), ())
            await runtime.shutdown()
            self.assertTrue(self.app_context._closed)


class OneBotWorkerStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_precedes_workers_and_keeps_all_enabled_callbacks(self):
        order = []
        workers = {}

        def start(name, callback):
            order.append(name)
            workers[name] = callback
            return True

        context = SimpleNamespace(
            turn_journal=SimpleNamespace(
                recover_interrupted=Mock(side_effect=lambda: order.append("recover")),
                recovered_unknown_effects=0,
                recovered_crashed_turns=0,
            ),
            background_tasks=SimpleNamespace(start=start),
            logger=Mock(),
            settings=replace(
                settings,
                observability_enabled=True,
                alertmanager_url="https://alerts.test",
                alert_notify_group_id=1,
            ),
            mobile_authorization=Mock(),
            proactive_learning=Mock(),
            subagent_coordinator=Mock(),
            local_model=Mock(),
            job_store=SimpleNamespace(enqueue=Mock(return_value=(None, True))),
            job_worker=SimpleNamespace(run_forever=Mock(), registered_kinds=("test",)),
            media_library=Mock(),
            vision_worker=Mock(),
            cold_archive=Mock(),
            reminder_store=Mock(),
            delivery_store=Mock(),
            semantic_recall=Mock(),
            historian_service=Mock(),
            dream_service=Mock(),
            bridge_manager=SimpleNamespace(
                matrix=Mock(), imessage=Mock(), imessage_poll_enabled=True
            ),
        )
        handlers, notifier = Mock(), Mock()
        await start_background_tasks(context, handlers, notifier)
        self.assertEqual(order[0], "recover")
        self.assertEqual(
            set(workers),
            {
                "event-loop-lag",
                "proactive-feedback",
                "mobile-authorization",
                "subagent-workflows",
                "agent-routines",
                "local-model-health",
                "alert-notifier",
                "durable-jobs",
                "media-library",
                "vision-worker",
                "cold-archive",
                "reminders",
                "delivery",
                "matrix-sync",
                "imessage-sync",
                "semantic-index",
                "historian",
                "memory-dream",
            },
        )
        self.assertIs(workers["delivery"], handlers.delivery._delivery_loop)
        self.assertIs(workers["agent-routines"], handlers.routines.run_forever)
        self.assertIs(workers["historian"], handlers.triggers._historian_loop)
        self.assertIs(workers["proactive-feedback"], handlers.triggers._proactive_feedback_loop)
        context.job_store.enqueue.assert_called_once()


EXPECTED_BINDINGS = (
    ("ai", "commands", "handle_ai"),
    ("web_search", "commands", "handle_web_search"),
    ("image_ocr", "commands", "handle_image_ocr"),
    ("voice_answer", "commands", "handle_voice_answer"),
    ("voice_transcription", "commands", "handle_voice_transcription"),
    ("model_command", "commands", "handle_model_command"),
    ("memory_command", "commands", "handle_memory_command"),
    ("control_command", "commands", "handle_control_command"),
    ("effort_command", "commands", "handle_effort_command"),
    ("shell_command", "commands", "handle_shell_command"),
    ("pin_command", "commands", "handle_pin_command"),
    ("unpin_command", "commands", "handle_unpin_command"),
    ("pins_command", "commands", "handle_pins_command"),
    ("task_status", "commands", "handle_task_status"),
    ("usage_command", "commands", "handle_usage_command"),
    ("routine_command", "commands", "handle_routine_command"),
    ("task_stop", "commands", "handle_task_stop"),
    ("mention_ai", "commands", "handle_mention_ai"),
    ("canonical_ingest_tracker", "ingest", "handle_canonical_ingest"),
    ("reaction_feedback_tracker", "ingest", "handle_reaction_notice"),
    ("proactive_feedback_tracker", "ingest", "handle_proactive_notice"),
    ("group_activity_tracker", "ingest", "handle_group_activity"),
    ("image_auto_description", "ingest", "handle_image_auto_description"),
    ("proactive_chat", "triggers", "handle_proactive_chat"),
    ("sticker", "commands", "handle_sticker"),
    ("qq_face", "commands", "handle_qq_face"),
    ("sticker_status", "commands", "handle_sticker_status"),
    ("group_context_recorder", "ingest", "handle_group_context_recorder"),
    ("ai_reset", "commands", "handle_ai_reset"),
    ("clear_data", "commands", "handle_clear_data"),
)


class OneBotMatcherStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_typed_adapters_delegate_to_the_owned_service(self):
        class CaptureMatcher:
            def __init__(self):
                self.endpoint = None

            def handle(self):
                def capture(endpoint):
                    self.endpoint = endpoint
                    return endpoint

                return capture

        registry = SimpleNamespace(
            **{name: CaptureMatcher() for name, _, _ in EXPECTED_BINDINGS}
        )
        services = SimpleNamespace(
            commands=SimpleNamespace(),
            ingest=SimpleNamespace(),
            triggers=SimpleNamespace(),
        )
        for _, service, name in EXPECTED_BINDINGS:
            setattr(getattr(services, service), name, AsyncMock())
        runtime = SimpleNamespace(handlers=services)
        with patch.object(matcher_handlers, "matchers", registry):
            matcher_handlers.register_matcher_handlers(runtime)
        values = {
            Bot: Mock(),
            MessageEvent: Mock(),
            NoticeEvent: Mock(),
            Message: Message("args"),
        }
        self.assertEqual(len(EXPECTED_BINDINGS), 30)
        for matcher, service, name in EXPECTED_BINDINGS:
            with self.subTest(matcher=matcher, handler=name):
                endpoint = getattr(registry, matcher).endpoint
                parameters = inspect.signature(endpoint, eval_str=True).parameters
                arguments = {
                    key: values[item.annotation] for key, item in parameters.items()
                }
                await endpoint(**arguments)
                getattr(getattr(services, service), name).assert_awaited_once_with(
                    *arguments.values()
                )
