"""Real PostgreSQL and private RPC; platform membership and kernel effects are fixtures."""

import asyncio
from dataclasses import replace
import hashlib
import json
import os
import time
import unittest
from unittest.mock import AsyncMock, patch

import nonebot
from alembic import command
from alembic.config import Config
from nonebot.adapters.onebot.v11 import Message

nonebot.init()

from src.plugins.ai_chat.agent.execution import active_agent_step
from src.plugins.ai_chat.project_blobs import ProjectFile
from src.plugins.ai_chat.project_commands import command_result, handle_project_command
from src.bot_storage import DatabaseError
from src.bot_storage.schema import HEAD_REVISION
from src.plugins.ai_chat.project_runtime import ProjectRuntime
from src.plugins.ai_chat.projects import ProjectConflict, ProjectStore
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker
from src.plugins.ai_chat.tools import domain_registry
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from tests import test_project_runtime as runtime_fixture
from tests.test_routine_tools import event


class ProjectToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await runtime_fixture.ProjectRuntimeTests.asyncSetUp(self)
        await self.runtime.start()
        self.app.project_runtime = self.runtime
        self.context = ToolContext(self.handlers, event(), frozenset({"projects"}))

    async def call(self, name, args=None, context=None):
        return json.loads(
            await domain_registry.execute(
                TurnState(context or self.context),
                name,
                args or {},
            )
        )

    async def create(self, title="Website", context=None):
        result = await self.call("project_create", {"title": title}, context)
        self.assertTrue(result["ok"], result)
        return result

    async def test_save_command_reports_queued_then_verified_version_and_label(self):
        created = await self.create()
        await self.worker.run_once()
        pid = int(created["project"]["handle"][4:])
        root = self.volumes.binding(self.scope.key, pid)
        (root / "notes.txt").write_bytes(b"saved via command")
        output = await command_result(self.handlers, self.context.event, "保存 Release one")
        self.assertIn("排队", output)
        self.assertNotIn("保存已完成", output)
        await self.worker.run_once()
        status = await self.call("project_status")
        self.assertTrue(status["snapshot"]["saved"], status)
        self.assertEqual(status["snapshot"]["version"], "v1")
        self.assertEqual(status["versions"][0]["label"], "Release one")
        output = await command_result(self.handlers, self.context.event, "状态")
        self.assertIn("最近保存已完成：v1", output)
        self.assertEqual(self.runtime.store.read_file(self.access, pid, 1, "notes.txt"), b"saved via command")
        second = event()
        second.message_id += 1
        output = await command_result(self.handlers, second, f"保存 prj#{pid}\t第二版标签")
        self.assertIn("排队", output)
        await self.worker.run_once()
        versions = self.runtime.store.versions(self.access, pid)
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["label"], "第二版标签")

    async def test_snapshot_replay_keeps_original_project_after_focus_changes(self):
        first = await self.create()
        await self.worker.run_once()
        handle = first["project"]["handle"]
        result = await self.call("project_snapshot", {"label": "Milestone"})
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["snapshot"]["saved"])
        self.runtime.store.open(self.access, self.pid)
        replay = await self.call("project_snapshot", {"label": "Milestone"})
        self.assertEqual(replay["project"]["handle"], handle)
        self.assertEqual(len(self.runtime.lifecycle.journal(self.access, int(handle[4:]))), 2)
        self.assertEqual(self.runtime.lifecycle.journal(self.access, self.pid), [])
        changed = await self.call("project_snapshot", {"label": "Different"})
        self.assertEqual(changed["code"], "project_conflict")
        await self.worker.run_once()
        replay = await self.call("project_snapshot", {"label": "Milestone"})
        self.assertTrue(replay["snapshot"]["saved"], replay)
        self.assertEqual(self.runtime.store.current(self.access)["project_id"], self.pid)

    async def test_snapshot_permission_account_binding_and_arguments(self):
        created = await self.create()
        await self.worker.run_once()
        handle = created["project"]["handle"]
        self.bot.get_group_member_list.return_value.append({"user_id": 202})
        for changed in (event(actor=202), event(group=102), event(bot=302), event(group=None)):
            result = await self.call("project_snapshot", {"project_handle": handle}, replace(self.context, event=changed))
            self.assertFalse(result["ok"], result)
        result = await self.call("project_snapshot", {"label": "x" * 121})
        self.assertEqual(result["code"], "project_invalid_arguments")
        result = await self.call("project_snapshot", {"source_turn_id": 1})
        self.assertEqual(result["code"], "project_invalid_arguments")
        self.app.settings = replace(self.app.settings, sandbox_allowed_users=set())
        self.assertEqual((await self.call("project_snapshot"))["code"], "project_forbidden")

    async def test_snapshot_cannot_wake_unmaterialized_project_or_replay_after_revocation(self):
        self.assertEqual((await self.call("project_snapshot"))["code"], "project_conflict")
        created = await self.create()
        await self.worker.run_once()
        pid = int(created["project"]["handle"][4:])
        principal = self.app.message_ledger.ensure_principal_identity("onebot-v11", "202", "Editor")
        project = self.runtime.store.get(self.access, pid)
        self.runtime.store.set_member(
            self.access, pid, principal, "editor", expected_version=project["version"],
        )
        self.bot.get_group_member_list.return_value.append({"user_id": 202})
        self.app.settings = replace(self.app.settings, sandbox_allowed_users={201, 202})
        context = replace(self.context, event=event(actor=202))
        args = {"project_handle": created["project"]["handle"]}
        result = await self.call("project_snapshot", args, context)
        self.assertTrue(result["ok"], result)
        await self.worker.run_once()
        project = self.runtime.store.get(self.access, pid)
        self.runtime.store.set_member(
            self.access, pid, principal, "viewer", expected_version=project["version"],
        )
        self.assertEqual((await self.call("project_snapshot", args, context))["code"], "project_forbidden")

    async def test_snapshot_failure_does_not_claim_save_completed(self):
        await self.create()
        await self.worker.run_once()
        await self.call("project_snapshot")
        with patch.object(self.runtime.backends["oci"], "execute", side_effect=RuntimeError("fixture failure")):
            await self.worker.run_once()
        output = await command_result(self.handlers, self.context.event, "状态")
        self.assertIn("版本保存未完成", output)
        self.assertNotIn("最近保存已完成", output)
        self.assertNotIn("fixture failure", output)

    async def test_snapshot_empty_migration_reversible_and_retained_journal_protected(self):
        with patch.dict(os.environ, {
            "AI_POSTGRES_DSN": self.app.database.dsn,
            "AI_POSTGRES_SCHEMA": self.app.database.schema,
        }):
            command.downgrade(Config("alembic.ini"), "0050_project_files")
            command.upgrade(Config("alembic.ini"), HEAD_REVISION)
            await self.create()
            await self.worker.run_once()
            result = await self.call("project_snapshot")
            self.assertTrue(result["ok"], result)
            with self.assertRaisesRegex(RuntimeError, "snapshot journals"):
                command.downgrade(Config("alembic.ini"), "0050_project_files")
        self.app.database.require_revision(HEAD_REVISION)

    async def test_create_duplicate_delivery_wake_status_and_versions(self):
        created = await self.create()
        handle = created["project"]["handle"]
        self.assertTrue(created["project"]["preparing"])
        self.assertEqual(created["project"]["status"], "stopped")
        self.assertEqual(self.docker.serial, 0)
        results = await asyncio.gather(*(self.create() for _ in range(4)))
        self.assertTrue(all(r["project"]["handle"] == handle for r in results))
        self.assertEqual(len(self.runtime.store.list(self.access)), 2)
        pid = int(handle[4:])
        self.assertEqual(len(self.runtime.lifecycle.journal(self.access, pid)), 1)
        self.assertEqual(created["operation"]["status"], "pending")
        await self.worker.run_once()
        status = await self.call("project_status")
        self.assertEqual(status["project"]["status"], "active")
        self.assertFalse(status["project"]["preparing"])
        self.assertIsNone(status["usage"]["used_bytes"])
        self.assertEqual(self.docker.serial, 1)
        self.assertEqual((await self.create())["project"]["handle"], handle)
        self.assertEqual(self.docker.serial, 1)
        encoded = json.dumps(status)
        for private in (
            "sandbox_id",
            "reservation",
            "scope_key",
            "bot_native_user_id",
            "creation_key",
        ):
            self.assertNotIn(private, encoded)
        self.assertEqual((await self.call("project_versions"))["versions"], [])

    async def test_replay_survives_store_reopen_rename_and_newer_focus(self):
        created = await self.create()
        await self.worker.run_once()
        pid = int(created["project"]["handle"][4:])
        project = self.runtime.store.get(self.access, pid)
        self.runtime.store.update(
            self.access, pid, expected_version=project["version"], title="Renamed"
        )
        self.runtime.store.open(self.access, self.pid)
        await self.runtime.close()
        self.app.job_worker = DurableJobWorker(
            self.jobs, logger=self.logger, worker_id="restarted"
        )
        self.runtime = ProjectRuntime(self.handlers)
        self.app.project_runtime = self.runtime
        self.addAsyncCleanup(self.runtime.close)
        await self.runtime.start()
        replay = await self.create()
        self.assertEqual(replay["project"]["handle"], created["project"]["handle"])
        self.assertEqual(replay["project"]["title"], "Renamed")
        self.assertFalse(replay["current"])
        key = self.runtime.store.get(self.access, pid)["creation_key"]
        with self.assertRaises(ProjectConflict):
            self.runtime.store.create(self.access, "Different", creation_key=key)
        new_event = replace(self.context, event=event())
        new_event.event.message_id += 1
        new = await self.create(context=new_event)
        self.assertNotEqual(new["project"]["handle"], replay["project"]["handle"])

    async def test_other_group_bot_and_private_cannot_read_project(self):
        handle = (await self.create())["project"]["handle"]
        for changed in (
            event(group=102),
            event(bot=302),
            event(group=None),
            event(actor=202, group=None),
        ):
            context = replace(self.context, event=changed)
            result = await self.call(
                "project_status", {"project_handle": handle}, context
            )
            self.assertFalse(result["ok"])
            self.assertNotIn("Website", json.dumps(result))
        # A different current group can list its own projects without seeing this one.
        listed = await self.call(
            "project_list", context=replace(self.context, event=event(group=102))
        )
        self.assertEqual(listed["projects"], [])

    async def test_revocation_offline_and_sandbox_admission_block_mutations(self):
        self.bots.clear()
        self.assertEqual(
            (await self.call("project_create", {"title": "No"}))["code"],
            "project_identity_unavailable",
        )
        self.bots["301"] = self.bot
        self.bot.get_group_member_list.return_value = [{"user_id": 301}]
        self.assertEqual((await self.call("project_list"))["code"], "project_forbidden")
        self.bot.get_group_member_list.return_value = [
            {"user_id": 201},
            {"user_id": 301},
        ]
        self.app.settings = replace(self.app.settings, sandbox_allowed_users=set())
        self.assertFalse((await self.call("project_create", {"title": "No"}))["ok"])
        self.assertTrue((await self.call("project_list"))["ok"])
        self.assertEqual(len(self.runtime.store.list(self.access)), 1)

    async def test_failed_preparation_is_not_reported_as_running(self):
        await self.create()
        with patch.object(
            self.runtime.backends["oci"],
            "execute",
            side_effect=RuntimeError("private failure"),
        ):
            await self.worker.run_once()
        status = await self.call("project_status")
        self.assertEqual(status["operation"]["status"], "needs_attention")
        self.assertEqual(status["project"]["status"], "stopped")
        self.assertNotIn("private failure", json.dumps(status))
        output = await command_result(self.handlers, self.context.event, "状态")
        self.assertIn("需要处理", output)
        self.assertNotIn("运行中", output)

    async def test_disabled_and_background_callers_never_advertise_or_execute(self):
        for context in (
            replace(self.context, capabilities=frozenset()),
            replace(self.context, routine_run_id="synthetic"),
        ):
            self.assertFalse(
                any(
                    d["function"]["name"].startswith("project_")
                    for d in domain_registry.definitions(context)
                )
            )
            self.assertEqual(
                (await self.call("project_list", context=context))["code"],
                "tool_unavailable",
            )
        token = active_agent_step.set(object())
        try:
            self.assertEqual(
                (await self.call("project_list"))["code"], "tool_unavailable"
            )
        finally:
            active_agent_step.reset(token)
        self.app.settings = replace(self.app.settings, projects_enabled=False)
        self.assertEqual((await self.call("project_list"))["code"], "tool_unavailable")

    async def test_member_view_does_not_grant_edit_or_wake(self):
        self.bot.get_group_member_list.return_value.append({"user_id": 202})
        context = replace(self.context, event=event(actor=202))
        handle = f"prj#{self.pid}"
        opened = await self.call("project_open", {"project_handle": handle}, context)
        self.assertTrue(opened["ok"])
        self.assertTrue(opened["readonly"])
        self.assertFalse(opened["project"]["preparing"])
        self.assertIsNone(
            self.runtime.store.get(self.access, self.pid)["lifecycle_job_id"]
        )
        denied = await self.call(
            "project_set_rebuild",
            {
                "project_handle": handle,
                "expected_revision": 1,
                "command": "npm ci",
            },
            context,
        )
        self.assertEqual(denied["code"], "project_forbidden")

    async def test_owner_open_wakes_and_recorded_command_does_not_execute(self):
        result = await self.call(
            "project_set_rebuild",
            {
                "project_handle": f"prj#{self.pid}",
                "expected_revision": 1,
                "command": "npm ci",
            },
        )
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["executed"])
        self.assertEqual(self.docker.serial, 0)
        stale = await self.call(
            "project_set_rebuild",
            {
                "project_handle": f"prj#{self.pid}",
                "expected_revision": 1,
                "command": "npm ci",
            },
        )
        self.assertEqual(stale["code"], "project_conflict")
        opened = await self.call("project_open", {"project_handle": f"prj#{self.pid}"})
        self.assertTrue(opened["ok"], opened)
        self.assertFalse(opened["readonly"])
        self.assertTrue(opened["project"]["preparing"])

    async def test_versions_show_version_number_not_configuration_revision(self):
        project = self.runtime.store.get(self.access, self.pid)
        self.runtime.store.snapshot(
            self.access,
            self.pid,
            [ProjectFile("index.html", b"page")],
            expected_version=project["version"],
            label="First",
        )
        result = await self.call("project_versions")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["versions"][0]["handle"], "v1")
        self.assertEqual(result["versions"][0]["label"], "First")
        self.assertGreater(result["project"]["revision"], 1)
        self.assertNotIn("manifest_sha256", json.dumps(result))

    async def test_soft_quota_and_unknown_usage_are_honest_in_command(self):
        self.runtime.backends["oci"].quota_mode = "soft"
        output = await command_result(self.handlers, self.context.event, "状态")
        self.assertIn("软配额", output)
        self.assertIn("未知", output)
        self.assertIn("不能按零计算", output)
        self.assertNotIn("0.0 MiB /", output)
        with patch(
            "src.plugins.ai_chat.projects.time.time",
            return_value=int(time.time()) + 24 * 3600 + 1,
        ):
            self.assertEqual(
                (await self.call("project_status"))["code"], "project_not_selected"
            )

    async def test_model_cannot_supply_identity_admin_paths_or_create_key(self):
        for injected in (
            {"admin": True},
            {"scope_key": "onebot-v11:group:666"},
            {"creation_key": "f" * 64},
            {"path": "/etc"},
            {"quota_bytes": 100},
        ):
            result = await self.call("project_create", {"title": "No", **injected})
            self.assertEqual(result["code"], "project_invalid_arguments")
        self.assertEqual(len(self.runtime.store.list(self.access)), 1)
        self.assertEqual(
            (await self.call("project_status", {"project_handle": "prj#-1"}))["code"],
            "project_invalid_arguments",
        )

    async def test_slash_commands_share_registry_and_cannot_bypass_disabled_tool(self):
        self.assertIn(
            f"prj#{self.pid}",
            await command_result(self.handlers, self.context.event, ""),
        )
        output = await command_result(
            self.handlers, self.context.event, "新建 Class website"
        )
        self.assertIn("项目已登记", output)
        self.assertIn("准备中", output)
        self.assertIn(
            "还没有保存版本",
            await command_result(self.handlers, self.context.event, "版本"),
        )
        with patch.dict(
            "src.plugins.ai_chat.tool_policy._TOOL_ENABLED_OVERRIDES",
            {"project_list": False},
        ):
            self.assertIn(
                "不能使用",
                await command_result(self.handlers, self.context.event, "列表"),
            )
        self.assertIn(
            "/项目", await command_result(self.handlers, self.context.event, "新建")
        )

    async def test_duplicate_key_is_database_constrained_and_payload_mismatch_rejected(
        self,
    ):
        key = hashlib.sha256(b"stable-request").hexdigest()
        args = (self.access, "Stored")
        first = self.runtime.store.create(*args, creation_key=key)
        second = self.runtime.store.create(*args, creation_key=key)
        self.assertEqual(first["project_id"], second["project_id"])
        with self.assertRaises(ProjectConflict):
            self.runtime.store.create(self.access, "Other", creation_key=key)
        other = self.runtime.store.create(self.access, "Unkeyed")
        with self.assertRaises(DatabaseError):
            with self.runtime.store._transaction() as cursor:
                cursor.execute(
                    "UPDATE projects SET creation_key=? WHERE project_id=?",
                    (key, other["project_id"]),
                )

    async def test_unbound_legacy_project_is_not_exposed_to_arbitrary_bot(self):
        with self.runtime.store._transaction() as cursor:
            cursor.execute(
                "UPDATE projects SET bot_native_user_id='' WHERE project_id=?",
                (self.pid,),
            )
        self.assertEqual((await self.call("project_list"))["projects"], [])
        self.assertEqual(
            (await self.call("project_status"))["code"], "project_forbidden"
        )

    async def test_independent_connections_publish_one_creation_and_block_destructive_downgrade(
        self,
    ):
        store = ProjectStore(
            self.app.database, self.app.state_dir / "projects/versions"
        )
        self.addCleanup(store.close)
        key = hashlib.sha256(b"concurrent-request").hexdigest()
        rows = await asyncio.gather(
            *(
                asyncio.to_thread(
                    s.create, self.access, "Shared request", creation_key=key
                )
                for s in (store, self.runtime.store)
            )
        )
        self.assertEqual(rows[0]["project_id"], rows[1]["project_id"])
        with patch.dict(
            os.environ,
            {
                "AI_POSTGRES_DSN": self.app.database.dsn,
                "AI_POSTGRES_SCHEMA": self.app.database.schema,
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "creation identities"):
                command.downgrade(Config("alembic.ini"), "0047_project_runtime_owner")
        self.app.database.require_revision(HEAD_REVISION)
        self.assertEqual(
            store.get(self.access, rows[0]["project_id"])["creation_key"], key
        )

    async def test_user_titles_are_sent_as_text_never_cq_actions(self):
        title = "[CQ:at,qq=all]"
        self.runtime.store.create(self.access, title)
        with patch(
            "src.plugins.ai_chat.matchers.project_command.finish",
            new_callable=AsyncMock,
        ) as finish:
            await handle_project_command(
                self.handlers, self.context.event, Message("列表")
            )
        segment = finish.call_args.args[0]
        self.assertEqual(segment.type, "text")
        self.assertIn(title, segment.data["text"])
