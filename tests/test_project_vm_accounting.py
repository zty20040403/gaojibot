"""VM usage and maintenance through real PostgreSQL; OS drivers are fixtures."""

import copy
from dataclasses import replace
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tests.test_project_vm_executor import VmExecutorFixture
from src.plugins.ai_chat.project_maintenance import ProjectMaintenance
from src.plugins.ai_chat.project_usage import (
    ProjectQuotaRejected,
    ProjectUsageSampler,
    ProjectUsageStore,
)
from src.plugins.ai_chat.projects import ProjectConflict
from src.plugins.ai_chat.subagents import SubAgentStore
from src.sandbox_runtime.errors import SandboxError
from src.sandbox_runtime.project_volume_journal import MIB


class VmAccountingTests(VmExecutorFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.host.used_bytes = Mock(return_value=2 * MIB)
        self.usage = ProjectUsageStore(self.projects)

    def sampler(self):
        return ProjectUsageSampler(
            self.usage,
            {"vm": self.backend},
            enabled=True,
            resolve_access=lambda project, actor: self.access,
        )

    def scheduler(self):
        return ProjectMaintenance(
            self.lifecycle,
            {"vm": self.backend},
            enabled=True,
            resolve_access=lambda project, actor: self.access,
        )

    async def test_full_lifecycle_samples_actual_usage_without_waking_stopped_guests(
        self,
    ):
        initial = await self.backend.sample_usage(self.project())
        self.assertEqual(initial["used_bytes"], 0)
        await self.dependency_project()
        await self.transition("wake")
        p = self.project()
        f = self.guests[p["sandbox_id"]]
        f.used_bytes = 3 * MIB
        for action, expected, rebuildable in (
            (None, 3 * MIB, True),
            ("stop", 2 * MIB, False),
            ("hibernate", 2 * MIB, False),
            ("archive", 0, True),
        ):
            if action:
                await self.transition(action)
            effects = list(self.effects)
            before = self.project()
            await self.sampler().run_once()
            status = self.usage.status(self.access, self.pid)
            self.assertEqual(status["used_bytes"], expected)
            self.assertEqual(status["rebuildable_bytes"] is not None, rebuildable)
            for key in ("version", "last_active_at", "lifecycle_fence"):
                self.assertEqual(before[key], self.project()[key])
            self.assertEqual(effects, self.effects)
            self.usage.require(self.access, self.pid)
        self.assertEqual(self.host.used_bytes.call_count, 2)

    async def test_failure_preserves_last_known_and_denies_quota_admission(self):
        await self.dependency_project()
        await self.sampler().run_once()
        self.host.used_bytes.side_effect = SandboxError("unverified dirty filesystem")
        await self.sampler().run_once()
        status = self.usage.status(self.access, self.pid)
        self.assertIsNone(status["used_bytes"])
        self.assertEqual(status["last_known_used_bytes"], 2 * MIB)
        with self.assertRaises(ProjectQuotaRejected) as raised:
            self.usage.require(self.access, self.pid)
        self.assertEqual(raised.exception.code, "project_usage_unknown")

    async def test_offline_vm_start_or_file_change_during_sampling_rejects_receipt(
        self,
    ):
        await self.dependency_project()
        original = self.host.used_bytes

        def started(*args, **kwargs):
            next(iter(self.domains.values()))["state"] = "running"
            return 2 * MIB

        self.host.used_bytes = started
        with self.assertRaises(SandboxError):
            await self.backend.sample_usage(self.project())
        next(iter(self.domains.values()))["state"] = "shut off"

        def changed(path, **kwargs):
            with path.open("ab") as stream:
                stream.write(b"concurrent writer")
            return 2 * MIB

        self.host.used_bytes = changed
        with self.assertRaises(SandboxError):
            await self.backend.sample_usage(self.project())
        self.host.used_bytes = original

    async def test_retired_consumer_reappearing_and_wrong_epoch_fail_closed(self):
        await self.dependency_project()
        await self.transition("hibernate")
        self.host.attached = {"gaoji-vm-other"}
        with self.assertRaises(SandboxError):
            await self.backend.sample_usage(self.project())
        self.host.attached = set()
        with self.assertRaises(ProjectConflict):
            await self.backend.sample_usage(
                {**self.project(), "lifecycle_fence": 123456}
            )
        self.assertFalse(
            await self.backend.maintenance_ready(
                {**self.project(), "lifecycle_fence": 123456}
            )
        )

    async def test_sampler_rechecks_live_owner_after_inspection(self):
        await self.transition("wake")
        sampler = self.sampler()
        identities = iter((self.access, replace(self.access, member=False)))
        sampler.resolve_access = lambda project, actor: next(identities)
        await sampler.run_once()
        self.assertIsNone(self.usage.status(self.access, self.pid)["used_bytes"])

    async def test_scheduler_runs_durable_stop_hibernate_archive_without_resetting_activity(
        self,
    ):
        await self.transition("wake")
        now = self.project()["last_active_at"]
        for offset, action, state in (
            (1800, "stop", "stopped"),
            (7 * 86400, "hibernate", "hibernated"),
            (60 * 86400, "archive", "archived"),
        ):
            result = await self.scheduler().run_once(now=now + offset)
            self.assertEqual(result[0]["action"], action)
            self.assertEqual(await self.scheduler().run_once(now=now + offset + 1), [])
            await self.executor(self.claim(now=now + offset))
            self.assertEqual(self.project()["status"], state)
            self.assertEqual(self.project()["last_active_at"], now)

    async def test_active_writers_or_unconfirmed_delivery_prevent_idle_maintenance(
        self,
    ):
        await self.transition("wake")
        f = self.guests[self.project()["sandbox_id"]]
        before = copy.deepcopy(self.record())
        now = self.project()["last_active_at"] + 1800
        f.commands.sandbox_processes.return_value = True
        self.assertEqual(
            (await self.scheduler().run_once(now=now))[0]["skipped"], "ProjectConflict"
        )
        f.commands.sandbox_processes.return_value = False
        tasks = SubAgentStore(self.db)
        self.addCleanup(tasks.close)
        task = tasks.create_task(
            scope_key=self.scope.key,
            conversation_id="101",
            requester_user_id=201,
            trigger_message_id=None,
            objective="deliver PDF",
            max_parallelism=1,
            max_steps=1,
        )
        with self.projects._transaction() as cursor:
            cursor.execute(
                "UPDATE subagent_tasks SET project_id=?,status='completed',plan_json=? WHERE task_id=?",
                (
                    self.pid,
                    json.dumps({"contract": {"delivery_required": True}}),
                    task.task_id,
                ),
            )
        self.assertEqual(
            (await self.scheduler().run_once(now=now))[0]["skipped"], "ProjectConflict"
        )
        self.assertEqual(before, self.record())
        self.assertIsNone(self.project()["lifecycle_job_id"])

    async def test_wrong_guest_identity_and_epoch_never_publish_usage(self):
        await self.transition("wake")
        guest = self.manager._guest_exec_raw

        async def changed(*args, **kwargs):
            code, data, error = await guest(*args, **kwargs)
            receipt = json.loads(data)
            receipt["epoch"] += 1
            return code, json.dumps(receipt).encode(), error

        with patch.object(
            self.manager, "_guest_exec_raw", AsyncMock(side_effect=changed)
        ):
            with self.assertRaises(SandboxError):
                await self.backend.sample_usage(self.project())
