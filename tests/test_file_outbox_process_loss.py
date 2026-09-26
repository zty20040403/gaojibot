"""Real SIGKILL and PostgreSQL recovery; the QQ transport is explicitly simulated."""
from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import signal
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import psycopg
from psycopg import sql
from alembic import command
from alembic.config import Config
from nonebot.adapters.onebot.v11 import GroupMessageEvent

from tests import test_subagent_v2  # Initialize the isolated NoneBot test runtime.
from src.bot_storage.database import PostgresDatabase
from src.plugins.ai_chat.agent.background import SubAgentDispatcher
from src.plugins.ai_chat.agent.file_outbox import attempt_file
from src.plugins.ai_chat.onebot_codec import scope_from_event
from src.plugins.ai_chat.subagents import SubAgentStore


def _upload(root, content, filename):
    receipt = {"file_name": filename, "file_size": len(content), "uploader": "123",
               "upload_time": int(time.time()), "file_id": "simulated-qq-receipt"}
    with (Path(root) / "uploads.jsonl").open("a") as stream:
        stream.write(json.dumps(receipt) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return {"ok": True, "file_id": receipt["file_id"]}


def _child(dsn, schema, task_id, root, boundary, pipe):
    database = PostgresDatabase(dsn, schema=schema, min_size=1, max_size=2)
    store = SubAgentStore(database)
    def pause():
        pipe.send(boundary)
        while True:
            time.sleep(60)
    async def run():
        if boundary == "queued":
            pause()
        async def prepare(_artifact):
            data = (Path(root) / "artifact.txt").read_bytes()
            if boundary == "prepared":
                pause()
            return data
        async def send(data, filename):
            if boundary == "claimed":
                pause()
            result = _upload(root, data, filename)
            if boundary == "uploaded":
                pause()
            return result
        await attempt_file(store, task_id, store.deliveries(task_id)[0], prepare=prepare, send=send)
        if boundary == "acknowledged":
            pause()
    asyncio.run(run())


class FileOutboxTestDatabaseSafetyTests(unittest.TestCase):
    def test_rejects_production_database_before_migration(self):
        with patch.dict(os.environ, TEST_POSTGRES_DSN="test-dsn"), patch("psycopg.connect") as connect:
            connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (
                "qq_bot", False, 0, "")
            with self.assertRaisesRegex(RuntimeError, "dedicated gaoji_acceptance database"):
                FileOutboxProcessLossTests.setUpClass()

    def test_rejects_replicated_primary_before_migration(self):
        with patch.dict(os.environ, TEST_POSTGRES_DSN="test-dsn"), patch("psycopg.connect") as connect:
            connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (
                "gaoji_acceptance_test", False, 1, "ANY 1 (standby)")
            with self.assertRaisesRegex(RuntimeError, "must not run on a replicated"):
                FileOutboxProcessLossTests.setUpClass()


@unittest.skipUnless(os.getenv("TEST_POSTGRES_DSN"), "Requires an isolated PostgreSQL test database")
class FileOutboxProcessLossTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as connection:
            name, in_recovery, replicas, synchronous_standbys = connection.execute(
                "SELECT current_database(), pg_is_in_recovery(), "
                "(SELECT count(*) FROM pg_stat_replication), "
                "current_setting('synchronous_standby_names')"
            ).fetchone()
        if (name != "gaoji_acceptance" and not name.startswith("gaoji_acceptance_")):
            raise RuntimeError("Process-loss tests require a dedicated gaoji_acceptance database")
        if in_recovery or replicas or synchronous_standbys:
            raise RuntimeError("Process-loss tests must not run on a replicated PostgreSQL instance")

    async def test_postgres_offline_wait_keeps_manifest_and_retry_budget_after_reopen(self):
        dsn = os.environ["TEST_POSTGRES_DSN"]
        schema = "test_file_offline_" + uuid.uuid4().hex[:16]
        database = store = None
        try:
            with patch.dict(os.environ, AI_POSTGRES_DSN=dsn, AI_POSTGRES_SCHEMA=schema):
                command.upgrade(Config("alembic.ini"), "head")
            database = PostgresDatabase(dsn, schema=schema, min_size=1, max_size=2)
            store = SubAgentStore(database)
            task = store.create_task(scope_key="group:1", conversation_id="group:1:user:2",
                requester_user_id=2, trigger_message_id=None, objective="file", max_parallelism=1, max_steps=2)
            artifact = {"name": "result.txt", "size": 4, "snapshot": hashlib.sha256(b"test").hexdigest(),
                        "handle": "sandbox#s123abc/result.txt"}
            store.queue_file(task.task_id, artifact, "test.txt")
            prepare, send = AsyncMock(return_value=b"test"), AsyncMock(return_value={"ok": True})
            for _ in range(8):
                row = store.deliveries(task.task_id)[0]
                with patch("src.plugins.ai_chat.agent.file_outbox.time.time", return_value=row["payload"]["next_attempt_at"] + 1):
                    result = await attempt_file(store, task.task_id, row, prepare=prepare, send=send,
                                                readiness=AsyncMock(return_value="offline"))
                self.assertEqual((result["state"], result["attempts"]), ("queued", 0))
                self.assertEqual(result["artifact"], artifact)
                store.close()
                database.close()
                database = PostgresDatabase(dsn, schema=schema, min_size=1, max_size=2)
                store = SubAgentStore(database)
            prepare.assert_not_awaited()
            send.assert_not_awaited()
            with patch("src.plugins.ai_chat.agent.file_outbox.time.time", return_value=result["next_attempt_at"] + 1):
                result = await attempt_file(store, task.task_id, store.deliveries(task.task_id)[0],
                    prepare=prepare, send=send, readiness=AsyncMock(return_value=None))
            self.assertEqual((result["state"], result["attempts"]), ("acknowledged", 1))
            send.assert_awaited_once()
        finally:
            if store:
                store.close()
            if database:
                database.close()
            with psycopg.connect(dsn, autocommit=True) as connection:
                connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    async def test_sigkill_at_each_file_boundary_and_recover_without_duplicate_upload(self):
        dsn = os.environ["TEST_POSTGRES_DSN"]
        for boundary in ("queued", "prepared", "claimed", "uploaded", "acknowledged"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as root:
                schema = "test_file_crash_" + uuid.uuid4().hex[:16]
                database = store = None
                process = None
                parent = child = None
                try:
                    with patch.dict(os.environ, AI_POSTGRES_DSN=dsn, AI_POSTGRES_SCHEMA=schema):
                        command.upgrade(Config("alembic.ini"), "head")
                    database = PostgresDatabase(dsn, schema=schema, min_size=1, max_size=2)
                    store = SubAgentStore(database)
                    event = GroupMessageEvent(time=int(time.time()), self_id=123, post_type="message",
                        message_type="group", sub_type="normal", message_id=42, group_id=1, user_id=2,
                        message="file", raw_message="file", font=0, sender={"user_id": 2})
                    task = store.create_task(scope_key=scope_from_event(event).key, conversation_id="group:1:user:2",
                        requester_user_id=2, trigger_message_id=42, objective="file", max_parallelism=1, max_steps=2)
                    store.update_control(task.task_id, expected_version=0,
                        dispatch={"bot_id": "123", "event": event.model_dump(mode="json")})
                    data = b"artifact content"
                    (Path(root) / "artifact.txt").write_bytes(data)
                    artifact = {"name": "artifact.txt", "snapshot": hashlib.sha256(data).hexdigest(),
                                "size": len(data), "handle": "s123abc:/workspace/artifact.txt"}
                    store.queue_file(task.task_id, artifact, "task-revision-digest-artifact.txt")
                    context = multiprocessing.get_context("spawn")
                    parent, child = context.Pipe(duplex=False)
                    process = context.Process(target=_child, args=(dsn, schema, task.task_id, root, boundary, child))
                    process.start()
                    self.assertTrue(await asyncio.to_thread(parent.poll, 30), "Child did not reach the crash boundary")
                    self.assertEqual(parent.recv(), boundary)
                    os.kill(process.pid, signal.SIGKILL)
                    await asyncio.to_thread(process.join, 10)
                    self.assertEqual(process.exitcode, -signal.SIGKILL)
                    store.close()
                    database.close()
                    database = PostgresDatabase(dsn, schema=schema, min_size=1, max_size=2)
                    store = SubAgentStore(database)
                    row = store.deliveries(task.task_id)[0]
                    expected = {"queued": "queued", "prepared": "queued", "claimed": "sending",
                                "uploaded": "sending", "acknowledged": "acknowledged"}[boundary]
                    self.assertEqual(row["state"], expected)
                    self.assertEqual(row["payload"]["artifact"], artifact)
                    async def send(content, filename):
                        return _upload(root, content, filename)
                    result = await attempt_file(store, task.task_id, row,
                        prepare=AsyncMock(return_value=data), send=send)
                    receipt_path = Path(root) / "uploads.jsonl"
                    receipts = [json.loads(line) for line in receipt_path.read_text().splitlines()] if receipt_path.exists() else []
                    self.assertEqual(len(receipts), 0 if boundary == "claimed" else 1)
                    dispatcher = object.__new__(SubAgentDispatcher)
                    dispatcher.store = store
                    dispatcher.context = SimpleNamespace(state_dir=Path(root), sandbox_manager=None,
                        settings=SimpleNamespace(subagent_retention_seconds=3600))
                    dispatcher.coordinator = SimpleNamespace(_artifact_retention_state=lambda _: (False, set()))
                    bot = SimpleNamespace(self_id="123", call_api=AsyncMock(side_effect=
                        lambda action, **params: {"online": True} if action == "get_status" else {"files": receipts}))
                    with patch("src.plugins.ai_chat.agent.background.get_bot", return_value=bot):
                        await dispatcher.reconcile(task.task_id)
                    final = store.deliveries(task.task_id)[0]
                    self.assertEqual(final["state"], "unknown" if boundary == "claimed" else "acknowledged")
                    # After a send claim, absence of a QQ receipt cannot prove no upload happened.
                    # Keep it explicitly unknown, never silently resend or announce successful delivery.
                    result = await attempt_file(store, task.task_id, final, prepare=AsyncMock(return_value=data), send=send)
                    self.assertEqual(result["ok"], boundary != "claimed")
                    self.assertEqual(len(receipt_path.read_text().splitlines()) if receipt_path.exists() else 0,
                                     0 if boundary == "claimed" else 1)
                finally:
                    if process is not None and process.is_alive():
                        process.kill()
                        process.join(10)
                    if parent:
                        parent.close()
                    if child:
                        child.close()
                    if store:
                        store.close()
                    if database:
                        database.close()
                    with psycopg.connect(dsn, autocommit=True) as conn:
                        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
