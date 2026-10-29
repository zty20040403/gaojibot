"""Application composition over real PostgreSQL/Unix RPC, mocked QQ and Docker."""

import asyncio
from dataclasses import replace
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import nonebot
from nonebot.adapters.onebot.v11 import Bot

nonebot.init()

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.config import Settings, settings
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.turn_journal import TurnJournal
from src.plugins.ai_chat.project_lifecycle import KIND
from src.plugins.ai_chat.project_runtime import (
    ProjectRuntime,
    install_projects,
    validate_project_settings,
)
from src.plugins.ai_chat.projects import ProjectAccess
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker, JobDeferred
from tests.postgres_fixture import postgres_for_test
from tests import test_project_executor as executor_fixture


class ProjectRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await executor_fixture.ProjectExecutorTests.asyncSetUp(self)
        self.ledger = MessageLedger(postgres_for_test(self))
        self.addCleanup(self.ledger.close)
        self.journal = TurnJournal(postgres_for_test(self))
        self.addCleanup(self.journal.close)
        self.app = SimpleNamespace(
            settings=replace(
                settings,
                projects_enabled=True,
                sandbox_enabled=True,
                sandbox_backend="oci",
                ledger_enabled=True,
                turn_journal_enabled=True,
                outbox_enabled=True,
                durable_jobs_enabled=True,
                sandbox_runner_socket="/fixture/runner.sock",
                sandbox_runner_token="fixture-only",
                project_instance_id="",
                sandbox_allow_all=False,
                sandbox_allowed_users={201},
                admin_user_ids={201},
            ),
            database=postgres_for_test(self),
            state_dir=self.root / "application",
            message_ledger=self.ledger,
            turn_journal=self.journal,
            job_store=self.jobs,
            job_worker=self.worker,
            delivery_store=Mock(),
            sandbox_manager=self.client,
            background_tasks=Mock(),
            db_call=DatabaseCalls(),
        )
        self.addAsyncCleanup(self.app.db_call.close)
        self.handlers = SimpleNamespace(
            context=self.app, group_enabled=Mock(return_value=True),
            triggers=Mock(spec=(
                "_proactive_feedback_loop", "_semantic_index_loop",
                "_historian_loop", "_dream_loop",
            )),
        )
        self.bot = Mock(spec=Bot)
        self.bot.self_id = "301"
        self.bot.get_group_member_list = AsyncMock(
            return_value=[{"user_id": 201}, {"user_id": 301}]
        )
        self.bots = {"301": self.bot}
        self.enterContext(
            patch(
                "nonebot.get_bots", return_value=self.bots
            )
        )
        self.runtime = ProjectRuntime(self.handlers)
        self.addAsyncCleanup(self.runtime.close)

    def project(self):
        return self.projects.get(self.access, self.pid)

    def request(self, action="wake", key="wake"):
        return self.runtime.lifecycle.request(
            self.access,
            self.pid,
            action,
            expected_version=self.project()["version"],
            request_key=key,
        )

    async def test_explicit_start_registers_worker_and_starts_supervised_loops_once(
        self,
    ):
        self.assertNotIn(KIND, self.worker.registered_kinds)
        await asyncio.gather(self.runtime.start(), self.runtime.start())
        self.assertTrue(self.runtime.ready)
        self.assertIs(self.runtime.io.projects, self.runtime.store)
        self.assertIs(self.runtime.io.usage, self.runtime.usage)
        self.assertIs(self.runtime.browser.store.io, self.runtime.io)
        self.assertIn(KIND, self.worker.registered_kinds)
        self.assertEqual(self.worker._timeouts[KIND], 3600)
        self.assertEqual(
            [item.args[0] for item in self.app.background_tasks.start.call_args_list],
            ["project-usage", "project-maintenance", "project-files", "project-autosave"],
        )
        self.assertEqual(self.runtime.backends["oci"].quota_mode, "hard")
        self.assertEqual(
            self.runtime.backends["oci"].files.operations,
            {
                "snapshot_workspace",
                "restore_version",
                "prune_rebuildable",
                "rebuild_dependencies",
                "check_restore_source",
                "publish_restore",
            },
        )
        run = self.request()
        await self.worker.run_once()
        self.assertEqual(self.jobs.get(run["job_id"]).status, "succeeded")
        self.assertEqual(self.project()["status"], "active")
        self.assertEqual(self.docker.serial, 1)
        self.assertGreater(self.bot.get_group_member_list.await_count, 1)

    async def test_original_account_and_private_owner_survive_store_restart(self):
        await self.runtime.start()
        self.assertEqual(self.project()["bot_native_user_id"], "301")
        await self.runtime.close()
        self.assertIsNone(self.runtime.io)
        self.assertIsNone(self.runtime.browser)
        self.app.job_worker = DurableJobWorker(
            self.jobs, logger=self.logger, worker_id="replacement"
        )
        runtime = ProjectRuntime(self.handlers)
        self.addAsyncCleanup(runtime.close)
        await runtime.start()
        access = await runtime.resolve_access(self.project(), self.access.principal_id)
        self.assertEqual(access, replace(self.access, admin=True))
        scope = replace(self.scope, kind="private", native_conversation_id="201")
        private = runtime.store.create(
            ProjectAccess(scope, self.access.principal_id, member=True), "Private"
        )
        self.bot.get_group_member_list.reset_mock()
        await runtime.resolve_access(private, self.access.principal_id)
        self.bot.get_group_member_list.assert_not_awaited()
        other = self.ledger.ensure_principal_identity("onebot-v11", "202", "B")
        with self.assertRaises(PermissionError):
            await runtime.resolve_access(private, other)

    async def test_task_reconciler_is_supervised_and_checks_actual_account_membership(self):
        from src.plugins.ai_chat.project_blobs import ProjectFile
        from src.plugins.ai_chat.project_tasks import ProjectTaskStore
        from src.plugins.ai_chat.subagents import SubAgentStore
        from tests.test_project_tasks import key, task_packet

        self.app.subagent_store = SubAgentStore(self.app.database)
        self.addCleanup(self.app.subagent_store.close)
        await asyncio.gather(self.runtime.start(), self.runtime.start())
        names = [item.args[0] for item in self.app.background_tasks.start.call_args_list]
        self.assertEqual(names.count("project-tasks"), 1)
        self.runtime.store.snapshot(self.access, self.pid, [ProjectFile("index.html", b"first")],
            expected_version=self.project()["version"])
        self.runtime.store.open(self.access, self.pid)
        task = ProjectTaskStore(self.runtime.store, self.app.subagent_store).prepare(
            self.access, self.pid, expected_version=self.project()["version"], base_version=1,
            request_key=key("runtime-reconcile"), packet=task_packet(self.access), decision=None,
            dispatch={"bot_id": "301"}, max_parallelism=2, max_steps=4)
        self.app.subagent_store.set_task_state(task.task_id, "failed")
        self.bots.clear()
        await self.runtime.task_reconciler.run_once()
        self.assertEqual(self.project()["locked_task_id"], task.task_id)
        self.bots["301"] = self.bot
        self.bot.get_group_member_list.return_value = [{"user_id": 301}]
        await self.runtime.task_reconciler.run_once()
        self.assertEqual(self.project()["locked_task_id"], task.task_id)
        self.bot.get_group_member_list.return_value = [{"user_id": 201}, {"user_id": 301}]
        await self.runtime.task_reconciler.run_once()
        self.assertIsNone(self.project()["locked_task_id"])
        self.assertEqual(self.docker.serial, 0)

    async def test_no_fallback_to_different_online_bot(self):
        await self.runtime.start()
        self.bots.clear()
        other = Mock(spec=Bot)
        other.self_id = "302"
        other.get_group_member_list = AsyncMock()
        self.bots["302"] = other
        with self.assertRaises(JobDeferred):
            await self.runtime.resolve_access(self.project(), self.access.principal_id)
        other.get_group_member_list.assert_not_called()
        foreign = replace(
            self.access, scope=replace(self.scope, bot_native_user_id="302")
        )
        with self.assertRaises(PermissionError):
            self.projects.get(foreign, self.pid)
        self.assertEqual(self.projects.list(foreign), [])

    async def test_disconnection_defers_without_spending_retry_or_creating_container(
        self,
    ):
        await self.runtime.start()
        run = self.request()
        self.bots.clear()
        await self.worker.run_once()
        job = self.jobs.get(run["job_id"])
        self.assertEqual(job.status, "pending")
        self.assertEqual(job.attempts, 0)
        self.assertEqual(self.docker.serial, 0)
        self.assertEqual(self.project()["lifecycle_job_id"], run["job_id"])
        self.assertEqual(
            self.runtime.lifecycle.journal(self.access, self.pid)[0]["status"],
            "pending",
        )
        self.bots["301"] = self.bot
        resumed = self.jobs.claim_due(
            self.worker.worker_id, kinds=(KIND,), now=int(time.time()) + 31
        )[0]
        await self.worker._execute(resumed)
        self.assertEqual(self.jobs.get(run["job_id"]).status, "succeeded")
        self.assertEqual(self.docker.serial, 1)

    async def test_membership_revoked_during_effect_preserves_checkpoint_for_recovery(
        self,
    ):
        await self.runtime.start()
        run = self.request()
        backend = self.runtime.backends["oci"]
        execute = backend.execute

        async def revoke_after_effect(*args):
            result = await execute(*args)
            self.bot.get_group_member_list.return_value = [{"user_id": 301}]
            return result

        with patch.object(
            backend, "execute", side_effect=revoke_after_effect
        ) as effects:
            await self.worker.run_once()
        self.assertEqual(effects.call_count, 1)
        journal = self.runtime.lifecycle.journal(self.access, self.pid)[0]
        self.assertEqual(journal["next_step"], 0)
        self.assertEqual(journal["status"], "needs_attention")
        self.assertEqual(self.docker.serial, 0)
        self.bot.get_group_member_list.return_value = [
            {"user_id": 201},
            {"user_id": 301},
        ]
        resumed = self.jobs.claim_due(
            self.worker.worker_id, kinds=(KIND,), now=int(time.time()) + 11
        )[0]
        await self.worker._execute(resumed)
        self.assertEqual(self.jobs.get(run["job_id"]).status, "succeeded")
        self.assertEqual(self.docker.serial, 1)

    async def test_membership_is_checked_fresh_and_revocation_blocks_effects(self):
        await self.runtime.start()
        await self.runtime.resolve_access(self.project(), self.access.principal_id)
        self.bot.get_group_member_list.return_value = [{"user_id": 301}]
        with self.assertRaises(PermissionError):
            await self.runtime.resolve_access(self.project(), self.access.principal_id)
        run = self.request()
        await self.worker.run_once()
        self.assertNotEqual(self.jobs.get(run["job_id"]).status, "succeeded")
        self.assertEqual(self.docker.serial, 0)
        self.bot.get_group_member_list.assert_awaited_with(group_id=101, no_cache=True)

    async def test_bad_roster_and_missing_bot_membership_never_grant_access(self):
        await self.runtime.start()
        for result in (
            None,
            {"user_id": 201},
            [{"user_id": True}],
            [{"user_id": "all"}],
        ):
            self.bot.get_group_member_list.return_value = result
            with self.assertRaises(JobDeferred):
                await self.runtime.resolve_access(
                    self.project(), self.access.principal_id
                )
        self.bot.get_group_member_list.side_effect = RuntimeError(
            "private API response"
        )
        with self.assertRaisesRegex(JobDeferred, "temporarily unavailable"):
            await self.runtime.resolve_access(self.project(), self.access.principal_id)
        self.bot.get_group_member_list.side_effect = None
        self.bot.get_group_member_list.return_value = [{"user_id": 201}]
        with self.assertRaises(PermissionError):
            await self.runtime.resolve_access(self.project(), self.access.principal_id)

    async def test_group_disable_allowlist_revocation_and_changed_identity_block_work(
        self,
    ):
        await self.runtime.start()
        self.handlers.group_enabled.return_value = False
        with self.assertRaises(PermissionError):
            await self.runtime.resolve_access(self.project(), self.access.principal_id)
        self.handlers.group_enabled.return_value = True
        self.app.settings = replace(self.app.settings, sandbox_allowed_users=set())
        with self.assertRaises(PermissionError):
            await self.runtime._execution_access(
                self.project(), self.access.principal_id
            )
        with self.assertRaises(PermissionError):
            await self.runtime.resolve_access(self.project(), 999999)
        with self.assertRaises(PermissionError):
            await self.runtime.resolve_access(
                {**self.project(), "bot_native_user_id": ""}, self.access.principal_id
            )

    async def test_unready_or_closed_runtime_does_not_execute_and_loops_stop(self):
        with self.assertRaises(JobDeferred):
            await self.runtime.execute(Mock())
        await self.runtime.start()
        await self.runtime.close()
        self.assertFalse(self.runtime.ready)
        await self.runtime.sample_forever()
        await self.runtime.maintain_forever()
        with self.assertRaises(RuntimeError):
            await self.runtime.start()
        with self.assertRaises(JobDeferred):
            await self.runtime.execute(Mock())

    async def test_unsupported_quota_runner_fails_closed_and_closes_store(self):
        with patch.object(
            self.client, "health", AsyncMock(return_value={"channel_ready": True})
        ):
            with self.assertRaisesRegex(RuntimeError, "quota capability"):
                await self.runtime.start()
        self.assertIsNone(self.runtime.store)
        self.assertFalse(self.runtime.ready)
        self.assertNotIn(KIND, self.worker.registered_kinds)
        self.app.background_tasks.start.assert_not_called()

    async def test_soft_mode_is_reported_before_first_workspace_exists(self):
        health = await self.client.health()
        with patch.object(
            self.client,
            "health",
            AsyncMock(return_value={**health, "project_quota_mode": "soft"}),
        ):
            await self.runtime.start()
        result = await self.runtime.sampler.run_once()
        self.assertEqual(result, [{"project_id": self.pid, "sampled": True}])
        status = self.runtime.usage.status(self.access, self.pid)
        self.assertEqual(status["quota_mode"], "soft")
        self.assertEqual(status["used_bytes"], 0)
        self.assertTrue(status["complete"])
        self.assertEqual(self.docker.serial, 0)

    async def test_installation_mismatch_rejected(self):
        self.app.settings = replace(self.app.settings, project_instance_id="f" * 32)
        with self.assertRaisesRegex(RuntimeError, "another project installation"):
            await self.runtime.start()
        self.assertNotIn(KIND, self.worker.registered_kinds)

    async def test_vm_composes_retained_disk_gate_and_file_adapter_without_booting(
        self,
    ):
        from src.plugins.ai_chat.vm_sandbox import VmSandboxManager
        from src.plugins.ai_chat.project_vm_executor import VmProjectBackend

        self.app.settings = replace(
            self.app.settings, sandbox_backend="vm", project_instance_id="a" * 32
        )
        manager = VmSandboxManager(
            image=str(self.root / "base.qcow2"), root=str(self.root / "vms")
        )
        manager._run = AsyncMock()
        self.app.sandbox_manager = manager
        await self.runtime.start()
        self.assertEqual(set(self.runtime.backends), {"vm"})
        backend = self.runtime.backends["vm"]
        self.assertIsInstance(backend, VmProjectBackend)
        self.assertIs(manager.project_mounts, backend.runtime.mounts)
        self.assertEqual(backend.runtime.disks.root, manager.root / "project-volumes")
        self.assertEqual(backend.runtime.disks.instance_id, "a" * 32)
        self.assertIn("rebuild_dependencies", backend.files.operations)
        manager._run.assert_not_awaited()
        await self.runtime.close()
        self.assertIsNone(manager.project_mounts)

    async def test_cancelled_store_construction_does_not_leak_connection(self):
        import threading

        began, finish = threading.Event(), threading.Event()
        opened = Mock()

        def construct(*args):
            began.set()
            finish.wait(5)
            return opened

        with patch(
            "src.plugins.ai_chat.project_runtime.ProjectStore", side_effect=construct
        ):
            task = asyncio.create_task(self.runtime.start())
            await asyncio.to_thread(began.wait, 5)
            task.cancel()
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        opened.close.assert_called_once()
        self.assertTrue(self.runtime.closed)

    async def test_startup_failure_prevents_other_workers_from_running(self):
        from src.plugins.ai_chat.onebot_workers import start_background_tasks

        self.app.project_runtime = self.runtime
        with patch.object(self.client, "health", AsyncMock(return_value={})):
            with self.assertRaisesRegex(RuntimeError, "quota capability"):
                await start_background_tasks(self.app, self.handlers, Mock())
        self.app.background_tasks.start.assert_not_called()


class ProjectRuntimeConfigurationTests(unittest.TestCase):
    def test_disabled_is_inert_and_does_not_open_stores(self):
        app = SimpleNamespace(settings=replace(settings, projects_enabled=False))
        install_projects(SimpleNamespace(context=app))
        self.assertFalse(hasattr(app, "project_runtime"))
        validate_project_settings(app.settings)

    def test_explicit_prerequisites_and_policy_are_validated(self):
        base = replace(
            settings,
            projects_enabled=True,
            sandbox_enabled=True,
            ledger_enabled=True,
            turn_journal_enabled=True,
            outbox_enabled=True,
            durable_jobs_enabled=True,
            sandbox_backend="vm",
            project_instance_id="a" * 32,
        )
        validate_project_settings(base)
        for field in (
            "sandbox_enabled",
            "ledger_enabled",
            "turn_journal_enabled",
            "outbox_enabled",
            "durable_jobs_enabled",
        ):
            with self.assertRaises(RuntimeError):
                validate_project_settings(replace(base, **{field: False}))
        with self.assertRaises(RuntimeError):
            validate_project_settings(replace(base, project_instance_id=""))
        with self.assertRaises(RuntimeError):
            validate_project_settings(
                replace(
                    base,
                    sandbox_backend="oci",
                    sandbox_runner_socket="",
                    sandbox_runner_token="",
                )
            )
        with self.assertRaises(ValueError):
            validate_project_settings(replace(base, project_hibernate_days=90))

    def test_app_identity_fallback_and_defaults(self):
        with patch.dict(
            "os.environ",
            {
                "AI_PROJECTS_ENABLED": "false",
                "AI_APP_INSTANCE_ID": "c" * 32,
                "AI_PROJECT_INSTANCE_ID": "",
            },
        ):
            loaded = Settings.from_env()
        self.assertFalse(loaded.projects_enabled)
        self.assertEqual(loaded.project_instance_id, "c" * 32)
        self.assertEqual(loaded.project_idle_stop_minutes, 30)
        self.assertEqual(loaded.project_hibernate_days, 7)
        self.assertEqual(loaded.project_archive_days, 60)
