"""Real shared OneBot/official lifecycles; only platform I/O is synthetic."""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import nonebot
nonebot.init()

from fastapi import FastAPI
from nonebot.adapters.onebot.v11 import Bot, Message, PrivateMessageEvent

from src.plugins.ai_chat.adapters.onebot_turn import OneBotTurnPlatform
from src.plugins.ai_chat.adapters.qq_official.runtime import OfficialRuntime, open_official_protocol
from src.plugins.ai_chat.application_diagnostics import configure_desktop_health
from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.onebot_runtime import OneBotRuntime
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey, SendRequest
from src.qq_official.testing.fake_platform import FakePlatform
from tests.postgres_fixture import isolated_postgres_for_test


def private(path, value):
    path.touch(mode=0o600)
    path.write_bytes(value)
    return str(path)


def chosen_settings(database, root):
    return replace(
        settings, postgres_dsn=database.dsn, postgres_schema=database.schema,
        admin_secret_file="",
        admin_bot_id=900001, admin_enabled=False, projects_enabled=False,
        sandbox_enabled=False, subagents_enabled=False, routines_enabled=False,
        group_wiki_enabled=False, ledger_enabled=True, context_lifecycle_enabled=True,
        turn_journal_enabled=True, outbox_enabled=True, quota_enabled=True,
        durable_jobs_enabled=True, semantic_enabled=False, historian_enabled=False,
        dream_enabled=False, matrix_enabled=False, imessage_enabled=False,
        mirror_routes_json="", browser_enabled=False, rich_render_enabled=False,
        media_enabled=False, mcp_enabled=False, observability_enabled=False,
        proactive_adaptive=False, alertmanager_url="", archive_enabled=False,
    )


def incoming():
    message = Message("TEST_LEGACY_INBOUND")
    return PrivateMessageEvent(
        time=int(time.time()), self_id=900001, post_type="message", user_id=100001,
        message_id=701, message=message, original_message=message,
        raw_message=str(message), font=0, message_type="private", sub_type="friend",
        sender={"user_id": 100001, "nickname": "TEST_LEGACY_USER"},
    )


class DualApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_dual_context_owns_one_worker_set_two_ingresses_and_ordered_shutdown(self):
        database = isolated_postgres_for_test(self)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            async with FakePlatform() as fake:
                env = {
                    "AI_ONEBOT_ENABLED": "true", "AI_QQ_OFFICIAL_ENABLED": "true",
                    "AI_QQ_OFFICIAL_APP_ID_FILE": private(root / "app", fake.app_id.encode()),
                    "AI_QQ_OFFICIAL_APP_SECRET_FILE": private(root / "secret", fake.app_secret.encode()),
                    "AI_QQ_OFFICIAL_BIND_PEPPER_FILE": private(root / "pepper", b"a" * 64),
                    "AI_QQ_OFFICIAL_OWNER_OPENIDS": "TEST_OWNER",
                    "AI_QQ_OFFICIAL_API_BASE": fake.base_url,
                }
                # M0's fake intentionally has no bot identity. Supply an explicit
                # fake authenticated READY observation, never derive it from AppID.
                @asynccontextmanager
                async def protocol(*args, **kwargs):
                    async with open_official_protocol(*args, **kwargs) as gateway:
                        real_run = gateway.run

                        async def run(callback):
                            async def forward(frame):
                                if frame.get("t") == "READY":
                                    frame = {**frame, "d": {**frame["d"], "user": {
                                        "id": "TEST_REAL_BOT", "bot": True}}}
                                await callback(frame)
                            await real_run(forward)

                        gateway.run = run
                        yield gateway

                official_instances = []

                def fake_transport_runtime(*args, **kwargs):
                    runtime = OfficialRuntime(*args, **kwargs, allow_test_server=True,
                        protocol_factory=protocol, probe_seconds=.05, retry_seconds=.05)
                    official_instances.append(runtime)
                    return runtime

                # Observe real worker execution, without replacing its body or builder.
                from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker
                worker_runs = Counter()
                real_worker_run = DurableJobWorker.run_forever

                async def worker_run(worker):
                    worker_runs[id(worker)] += 1
                    await real_worker_run(worker)

                connected_bots = {}
                host = OneBotRuntime(chosen_settings(database, root), state_dir=root / "state",
                    cache_dir=root / "cache", project_root=root,
                    logger=logging.getLogger("test-dual-startup"))
                app = FastAPI()
                configure_desktop_health(True)
                self.addCleanup(configure_desktop_health, False)
                with patch.dict(os.environ, env), patch("nonebot.get_bots", return_value=connected_bots), \
                    patch("src.plugins.ai_chat.adapters.qq_official.runtime.OfficialRuntime",
                          new=fake_transport_runtime), \
                    patch.object(DurableJobWorker, "run_forever", new=worker_run):
                    try:
                        await asyncio.gather(*(host.start(app) for _ in range(4)))
                        await asyncio.wait_for(fake.wait_ready(), 10)
                        context = host.context
                        official = context.official_runtime
                        self.assertEqual(official_instances, [official])
                        self.assertIsInstance(official, OfficialRuntime)
                        self.assertIs(official.application, context)
                        self.assertIs(official.database, context.database)
                        self.assertIsNot(context.database, database)
                        self.assertEqual(context.database.schema, database.schema)
                        self.assertIs(official.ledger, context.message_ledger)
                        self.assertIs(official.journal, context.turn_journal)
                        self.assertFalse(official._owns_ledger)
                        self.assertFalse(official._owns_journal)
                        self.assertIs(host.handlers.context, context)
                        self.assertIs(host.handlers.ingest_adapter.message_ledger, official.ledger)
                        async with asyncio.timeout(10):
                            while not (await context.official_observability.sample())["healthy"]:
                                await asyncio.sleep(.02)
                        writer = context.desktop_health_writer
                        writer.record()
                        health_path = context.state_dir / "desktop-health.json"
                        health = json.loads(health_path.read_text())
                        self.assertTrue(health["official"]["healthy"])
                        self.assertFalse(health["connected"])
                        # Official readiness does not depend on a OneBot connection.
                        self.assertEqual(connected_bots, {})

                        # A real OneBot event enters the shared canonical ingest.
                        event = incoming()
                        await host.handlers.ingest.handle_canonical_ingest(event)
                        legacy_scope = host.handlers.chat._conversation_scope(event)
                        legacy_id = await context.db_call(
                            context.message_ledger.canonical_id_for_native, legacy_scope, "701")
                        self.assertIsNotNone(legacy_id)
                        bot = Bot(Mock(), "900001")
                        bot.call_api = AsyncMock(return_value={"message_id": 900002})
                        connected_bots[bot.self_id] = bot
                        await host.connected(bot)
                        port = OneBotTurnPlatform(bot, event, canonical_scope=legacy_scope)
                        result = await port.send(SendRequest(port.endpoint, port.inbound.actor,
                            MessageBody((TextNode(0, "TEST_LEGACY_REPLY"),)), "TEST_DUAL_REPLY", 0))
                        self.assertEqual(result.status, "committed")
                        self.assertEqual(bot.call_api.await_count, 1)

                        # Real Gateway -> inbox -> turn command -> M3 -> receipt,
                        # with no fake model or substituted application builder.
                        await fake.emit("C2C_MESSAGE_CREATE", {
                            "id": "TEST_DUAL_HELP", "author": {"user_openid": "TEST_OWNER"},
                            "content": "/help", "timestamp": datetime.now(timezone.utc).isoformat(),
                        })
                        async with asyncio.timeout(10):
                            while len(fake.received) < 1:
                                await asyncio.sleep(.02)
                        self.assertEqual(len(fake.received), 1)
                        def applied_receipts():
                            with context.delivery_store.transaction() as cursor:
                                return cursor.execute(
                                    "SELECT count(*) FROM deliveries WHERE account_key=? AND status='committed'",
                                    (official._token.account_key,),
                                ).fetchone()[0]
                        async with asyncio.timeout(10):
                            while not await context.db_call(applied_receipts):
                                await asyncio.sleep(.02)
                        self.assertEqual(await context.db_call(applied_receipts), 1)
                        endpoint = EndpointKey(AccountKey("qq-official", fake.app_id, "test"),
                            "private", "TEST_OWNER")
                        official_id = await context.db_call(context.message_ledger.canonical_id_for_native,
                            endpoint.scope, "TEST_DUAL_HELP", endpoint=endpoint)
                        self.assertIsNotNone(official_id)
                        self.assertNotEqual(legacy_id, official_id)
                        self.assertEqual({scope.key for scope in await context.db_call(context.message_ledger.list_scopes)},
                            {legacy_scope.key, endpoint.scope.key})

                        # Repeated startup retains actual task identities; the
                        # account ingress/turn workers are separate from the one
                        # shared host worker and each physical worker runs once.
                        workers = dict(context.background_tasks._tasks)
                        self.assertIn("durable-jobs", workers)
                        self.assertIn("delivery", workers)
                        self.assertEqual(worker_runs[id(context.job_worker)], 1)
                        self.assertGreaterEqual(len(worker_runs), 3)
                        self.assertTrue(all(value == 1 for value in worker_runs.values()))
                        await asyncio.gather(host.start(app), host.start(app))
                        self.assertEqual(context.background_tasks._tasks, workers)
                        self.assertIs(context.desktop_health_writer, writer)
                        self.assertEqual(official_instances, [official])
                        self.assertEqual(context.background_tasks.failures(), {})
                        writer.record()
                        health = json.loads(health_path.read_text())
                        self.assertEqual(health["qq"], "900001")
                        self.assertTrue(health["connected"])
                        self.assertEqual(health["official"], {
                            "healthy": True, "lease_current": True, "gateway_ready": True})

                        actions = []
                        real_stop = official.stop
                        real_ledger_close = context.message_ledger.close
                        real_database_close = context.database.close

                        async def stop_official():
                            actions.append("official.start")
                            await real_stop()
                            self.assertFalse(context.database._closed)
                            context.message_ledger.list_scopes()
                            actions.append("official.drained")

                        def close_ledger():
                            actions.append("ledger.close")
                            return real_ledger_close()

                        def close_database():
                            actions.append("database.close")
                            return real_database_close()

                        with patch.object(official, "stop", new=stop_official), \
                            patch.object(context.message_ledger, "close", new=close_ledger), \
                            patch.object(context.database, "close", new=close_database):
                            await host.shutdown()
                            await host.shutdown()
                        self.assertEqual(actions, ["official.start", "official.drained",
                            "ledger.close", "database.close"])
                        self.assertTrue(context._closed)
                        self.assertTrue(context.database._closed)
                        self.assertFalse(database._closed)
                        self.assertTrue(all(task.done() for task in workers.values()))
                        self.assertEqual(context.background_tasks.running(), ())
                        self.assertEqual(official._supervisor.running(), ())
                        self.assertIsNone(official._token)
                        self.assertFalse(fake.connections)
                        health = json.loads(health_path.read_text())
                        self.assertFalse(health["connected"])
                        self.assertEqual(health["official"], {
                            "healthy": False, "lease_current": False, "gateway_ready": False})
                    finally:
                        await host.shutdown()

    async def test_real_legacy_only_context_never_opens_official_credentials(self):
        database = isolated_postgres_for_test(self)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            host = OneBotRuntime(chosen_settings(database, root), state_dir=root / "state",
                cache_dir=root / "cache", project_root=root,
                logger=logging.getLogger("test-disabled-official"))
            env = {"AI_ONEBOT_ENABLED": "true", "AI_QQ_OFFICIAL_ENABLED": "false",
                "AI_QQ_OFFICIAL_HOST_CONFIG_FILE": "/TEST_DO_NOT_OPEN/host.json",
                "AI_QQ_OFFICIAL_APP_SECRET_FILE": "/TEST_DO_NOT_OPEN/secret"}
            with patch.dict(os.environ, env), \
                patch("src.qq_official.credentials._read_secret",
                    side_effect=AssertionError("disabled official credentials read")) as reader:
                try:
                    await asyncio.gather(host.start(FastAPI()), host.start(FastAPI()))
                    context = host.context
                    self.assertTrue(host.ready)
                    self.assertIsNone(context.official_runtime)
                    self.assertIsNone(context.official_observability)
                    self.assertIsNone(context.desktop_health_writer)
                    await host.handlers.ingest.handle_canonical_ingest(incoming())
                    self.assertEqual(len(await context.db_call(context.message_ledger.list_scopes)), 1)
                    self.assertIn("durable-jobs", context.background_tasks.running())
                    reader.assert_not_called()
                finally:
                    await host.shutdown()
                self.assertTrue(context._closed)
                self.assertTrue(context.database._closed)
