"""Real PostgreSQL and Unix RPC; Docker/kernel are explicit local fixtures.

This exercises durable composition, not native Linux or retained-file acceptance.
"""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.project_executor import (
    DockerProjectBackend,
    ProjectBackendUnavailable,
    ProjectLifecycleExecutor,
)
from src.plugins.ai_chat.project_lifecycle import (
    KIND,
    ProjectLifecycle,
    transition_plan,
)
from src.plugins.ai_chat.projects import ProjectAccess, ProjectConflict, ProjectStore
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker
from src.sandbox_runtime.client import SandboxClient
from src.sandbox_runtime.docker import DockerSandboxManager, SandboxError
from src.sandbox_runtime.project_volumes import MIB, ProjectVolumes
from src.sandbox_runtime.server import SandboxRunner
from tests.postgres_fixture import postgres_for_test
from tests.test_durable_jobs import RecordingLogger
from tests.test_project_volumes import HostFixture
from tests.test_project_workspaces import DockerFixture, IMAGE
from tests.test_sandbox_runner import INSTANCE, TOKEN


class ProjectExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gj-ex-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "state").mkdir(mode=0o700)
        db = postgres_for_test(self)
        self.projects = ProjectStore(db, self.root / "blobs")
        self.jobs = DurableJobStore(db, lease_seconds=60)
        ledger = MessageLedger(db)
        for store in (self.projects, self.jobs, ledger):
            self.addCleanup(store.close)
        principal = ledger.ensure_principal_identity("onebot-v11", "201", "A")
        self.scope = ConversationScope("onebot-v11", "group", "101", "201", "301")
        self.access = ProjectAccess(self.scope, principal, member=True)
        self.pid = self.projects.create(self.access, "Worker", quota_bytes=64 * MIB)[
            "project_id"
        ]
        self.lifecycle = ProjectLifecycle(self.projects, self.jobs)
        self.host = HostFixture()
        self.volumes = ProjectVolumes(
            self.root / "state/project-volumes", INSTANCE, host=self.host
        )
        self.driver = DockerSandboxManager(
            namespace=INSTANCE, image=IMAGE, network="none", max_file_bytes=MIB
        )
        self.docker = DockerFixture(self.driver, self.volumes)
        self.host.in_use = self.docker.in_use
        self.driver._run = self.docker.run
        self.driver.list = self.docker.listing
        self.driver._list_by_label = self.docker.list_by_label
        self.driver._ensure_nix_volume = AsyncMock()
        self.runner = SandboxRunner(
            self.driver,
            socket_path=self.root / "socket/runner.sock",
            state_directory=self.root / "state",
            token=TOKEN,
            project_volumes=self.volumes,
        )
        await self.runner.start()
        self.addAsyncCleanup(self.runner.close)
        self.client = SandboxClient(
            str(self.root / "socket/runner.sock"), TOKEN, max_file_bytes=MIB
        )
        self.backend = DockerProjectBackend(self.client)
        self.executor = ProjectLifecycleExecutor(
            self.lifecycle,
            {"oci": self.backend},
            resolve_access=lambda project, actor: self.access,
        )
        self.logger = RecordingLogger()
        self.worker = DurableJobWorker(
            self.jobs, logger=self.logger, worker_id="worker"
        )

    def request(self, action="wake"):
        project = self.projects.get(self.access, self.pid)
        return self.lifecycle.request(
            self.access,
            self.pid,
            action,
            expected_version=project["version"],
            request_key=action,
        )

    def claim(self, **kwargs):
        return self.jobs.claim_due("worker", kinds=(KIND,), **kwargs)[0]

    async def test_explicit_worker_wakes_and_atomically_finishes_verified_project(self):
        run = self.request()
        self.assertNotIn(KIND, self.worker._handlers)
        self.executor.register(self.worker)
        await self.worker.run_once()
        project = self.projects.get(self.access, self.pid)
        self.assertEqual(project["status"], "active")
        self.assertIsNone(project["lifecycle_job_id"])
        self.assertEqual(self.jobs.get(run["job_id"]).status, "succeeded")
        self.assertEqual(self.docker.serial, 1)
        self.assertEqual(self.logger.warnings, [])
        observed = await self.client.project_workspace(
            "status", self.scope.key, self.pid, generation=1
        )
        self.assertEqual(observed["sandbox_id"], project["sandbox_id"])
        self.assertEqual(observed["state"], "running")
        self.assertFalse(observed["maintenance_active"])
        self.assertNotIn("reservation", run["plan"])
        self.assertNotIn(
            "reservation", self.lifecycle.journal(self.access, self.pid)[0]["plan"]
        )

    async def test_lost_start_ack_and_lease_takeover_do_not_create_twice(self):
        self.request()
        job = self.claim()
        while (step := self.lifecycle.next_step(job))["operation"] != "start_workspace":
            evidence = await self.backend.execute(job, step, self.access)
            self.lifecycle.acknowledge(job, step, evidence)
        await self.backend.execute(job, step, self.access)
        old_request, old_fence = step["request_id"], step["fence"]
        replacement = self.claim(now=int(time.time()) + 61)
        new_step = self.lifecycle.next_step(replacement)
        self.assertNotEqual(new_step["request_id"], old_request)
        self.assertEqual(new_step["fence"], old_fence)
        with self.assertRaises(ProjectConflict):
            await self.executor(job)
        await self.executor(replacement)
        self.assertEqual(self.docker.serial, 1)
        self.assertEqual(self.projects.get(self.access, self.pid)["status"], "active")

    async def test_cancel_between_effect_and_ack_preserves_reservation_for_successor(
        self,
    ):
        self.request()
        job = self.claim()
        execute = self.backend.execute
        paused = asyncio.Event()

        async def interrupted(*args):
            evidence = await execute(*args)
            if args[1]["operation"] == "reserve_workspace":
                paused.set()
                await asyncio.Event().wait()
            return evidence

        with patch.object(self.backend, "execute", side_effect=interrupted):
            running = asyncio.create_task(self.executor(job))
            try:
                await asyncio.wait_for(paused.wait(), 5)
            finally:
                running.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await running
        self.assertTrue(
            self.volumes.status(self.scope.key, self.pid)["maintenance_active"]
        )
        replacement = self.claim(now=int(time.time()) + 61)
        await self.executor(replacement)
        self.assertFalse(
            self.volumes.status(self.scope.key, self.pid)["maintenance_active"]
        )
        self.assertEqual(self.docker.serial, 1)

    async def test_missing_file_adapter_rejects_whole_stop_before_mutation(self):
        self.request()
        await self.executor(self.claim())
        run = self.request("stop")
        job = self.claim()
        commands, calls = list(self.docker.commands), list(self.host.calls)
        with self.assertRaises(ProjectBackendUnavailable):
            await self.executor(job)
        self.assertEqual(self.docker.commands, commands)
        self.assertEqual(self.host.calls, calls)
        self.assertEqual(
            self.lifecycle.journal(self.access, self.pid)[0]["status"],
            "needs_attention",
        )
        self.assertEqual(
            self.projects.get(self.access, self.pid)["lifecycle_job_id"], run["job_id"]
        )
        self.assertTrue(next(iter(self.docker.containers.values()))["State"]["Running"])

    async def test_missing_or_revoked_identity_cannot_start_physical_work(self):
        self.request()
        job = self.claim()
        for access in (
            None,
            replace(self.access, member=False),
            replace(
                self.access, scope=replace(self.scope, native_conversation_id="999")
            ),
        ):
            self.executor.resolve_access = lambda project, actor: access
            with self.assertRaises(PermissionError):
                await self.executor(job)
        self.assertEqual(self.host.calls, [])
        self.assertEqual(self.docker.commands, [])

    async def test_access_revocation_after_effect_does_not_ack_or_continue(self):
        self.request()
        job = self.claim()
        execute = self.backend.execute

        async def revoke(*args):
            evidence = await execute(*args)
            self.executor.resolve_access = lambda project, actor: replace(
                self.access, member=False
            )
            return evidence

        with patch.object(self.backend, "execute", side_effect=revoke):
            with self.assertRaises(PermissionError):
                await self.executor(job)
        self.assertEqual(self.lifecycle.next_step(job)["operation"], "create_volume")
        self.assertEqual(self.docker.serial, 0)
        self.assertIsNotNone(
            self.projects.get(self.access, self.pid)["lifecycle_job_id"]
        )

    async def test_external_stop_before_final_verification_cannot_publish_success(self):
        self.request()
        job = self.claim()
        execute = self.backend.execute

        async def stop_before_verify(*args):
            if args[1]["operation"] == "verify_active":
                next(iter(self.docker.containers.values()))["State"]["Running"] = False
            return await execute(*args)

        with patch.object(self.backend, "execute", side_effect=stop_before_verify):
            with self.assertRaises(ProjectConflict):
                await self.executor(job)
        project = self.projects.get(self.access, self.pid)
        self.assertEqual(project["status"], "stopped")
        self.assertIsNotNone(project["lifecycle_job_id"])
        self.assertNotEqual(self.jobs.get(job.job_id).status, "succeeded")

    async def test_unknown_plan_or_reservation_fails_preflight(self):
        self.request()
        job = self.claim()
        _, run = self.lifecycle.context(job)
        for changes in (
            {"protocol": 1},
            {"reservation": "bad"},
            {"reservation": 1},
            {"backend": "vm"},
            {"steps": ["shell"]},
            {"steps": []},
        ):
            with self.assertRaises(ProjectBackendUnavailable):
                self.backend.preflight({**run["plan"], **changes})
        self.assertEqual(self.host.calls, [])

    async def test_archiving_hibernated_project_does_not_stop_a_deleted_container(self):
        project = self.projects.get(self.access, self.pid)
        generation, plan = transition_plan(
            {**project, "status": "hibernated", "workspace_generation": 1},
            "archive",
            {"version_no": 1, "manifest_sha256": "a" * 64},
        )
        self.assertEqual(generation, 1)
        self.assertEqual(
            plan["steps"],
            [
                "reserve_workspace",
                "mount_volume",
                "snapshot_workspace",
                "remove_workspace",
                "unmount_volume",
                "delete_volume",
                "release_workspace",
                "verify_archived",
            ],
        )

    async def test_helper_error_does_not_expose_private_reservation(self):
        self.request()
        job = self.claim()
        _, run = self.lifecycle.context(job)
        self.docker.after["run"] = RuntimeError("interrupted container creation")
        with self.assertRaises(SandboxError):
            await self.executor(job)
        public = json.dumps(self.lifecycle.journal(self.access, self.pid))
        self.assertNotIn(run["plan"]["reservation"], public)
        self.assertTrue(
            self.volumes.status(self.scope.key, self.pid)["maintenance_active"]
        )
