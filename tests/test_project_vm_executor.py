"""Full PG/CAS/guest-file lifecycle; libvirt, mounts and systemd are fixtures."""

import asyncio
import copy
from dataclasses import replace
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

from tests import test_project_vm_provision as provision
from tests.test_project_vm_files import GuestFixture
from tests.test_project_vm_dependencies import DependencyRuntimeFixture
from tests.postgres_fixture import postgres_for_test
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.project_executor import (
    ProjectBackendUnavailable,
    ProjectLifecycleExecutor,
)
from src.plugins.ai_chat.project_file_executor import ProjectFileExecutor
from src.plugins.ai_chat.project_lifecycle import (
    KIND,
    ProjectLifecycle,
    transition_plan,
)
from src.plugins.ai_chat.project_vm_executor import VmProjectBackend
from src.plugins.ai_chat.projects import ProjectAccess, ProjectConflict, ProjectStore
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.sandbox_runtime.errors import SandboxError
from src.sandbox_runtime.project_vm_disks import VmProjectDisks
from src.sandbox_runtime.project_vm_file_guest import (
    HELPER_PATH as FILES_PATH,
    ProjectGuestFiles,
)
from src.sandbox_runtime.project_vm_files import VmProjectFiles
from src.sandbox_runtime.project_vm_gate import GuestDiskError
from src.sandbox_runtime.project_vm_runtime import VmProjectRuntime
from src.sandbox_runtime.project_vm_usage_guest import measure
from src.sandbox_runtime.project_volumes import MIB


class VmExecutorFixture(provision.VmProvisionFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.host.calls.clear()
        db = postgres_for_test(self)
        self.db = db
        self.projects = ProjectStore(db, self.root / "blobs")
        self.jobs = DurableJobStore(db, lease_seconds=60)
        ledger = MessageLedger(db)
        for store in (self.projects, self.jobs, ledger):
            self.addCleanup(store.close)
        principal = ledger.ensure_principal_identity("onebot-v11", "201", "A")
        self.scope = ConversationScope("onebot-v11", "group", "101", "201", "301")
        self.access = ProjectAccess(self.scope, principal, member=True)
        self.pid = self.projects.create(
            self.access, "VM lifecycle", backend="vm", quota_bytes=64 * MIB
        )["project_id"]
        self.lifecycle = ProjectLifecycle(self.projects, self.jobs)
        self.data_root = self.root / "lifecycle-data"
        self.guests = {}
        self.request_number = 0
        self.manager._guest_exec_raw = AsyncMock(side_effect=self.guest)
        self.compose()

    def compose(self):
        self.store = VmProjectDisks(self.data_root, provision.INSTANCE, host=self.host)
        self.runtime = VmProjectRuntime(self.manager, self.store, host=self.host)
        self.manager.project_mounts = self.runtime.mounts
        self.files = ProjectFileExecutor(
            self.lifecycle,
            VmProjectFiles(self.runtime.mounts),
            resolve_access=lambda project, actor: self.access,
        )
        self.backend = VmProjectBackend(self.runtime, files=self.files)
        self.executor = ProjectLifecycleExecutor(
            self.lifecycle,
            {"vm": self.backend},
            resolve_access=lambda project, actor: self.access,
        )

    def record(self):
        return json.loads((self.data_root / f"p{self.pid}/volume.json").read_text())

    def project(self):
        return self.projects.get(self.access, self.pid)

    def request(self, action):
        self.request_number += 1
        return self.lifecycle.request(
            self.access,
            self.pid,
            action,
            expected_version=self.project()["version"],
            request_key=f"{action}-{self.request_number}",
        )

    def claim(self, **kwargs):
        return self.jobs.claim_due("worker", kinds=(KIND,), **kwargs)[0]

    async def transition(self, action):
        self.request(action)
        return await self.executor(self.claim())

    async def virsh(self, *args, **kwargs):
        if args[0] == "shutdown":
            self.domains[args[1]]["state"] = "shut off"
            self.done("shutdown")
            return "shutdown"
        if args[0] == "undefine":
            del self.domains[args[1]]
            self.host.attached = set()
            self.done("undefine")
            return "undefined"
        return await super().virsh(*args, **kwargs)

    def guest_fixture(self, sid, metadata):
        if sid in self.guests:
            return self.guests[sid]
        f = GuestFixture(
            self.root / "guests" / sid, {**metadata["project"], "initialize": False}
        )
        f.workspace.rmdir()
        f.workspace = (
            self.root / f"retained-generation-{metadata['project']['generation']}"
        )
        f.workspace.mkdir(exist_ok=True)
        f.mounted = False
        f.used_bytes = MIB

        def verify():
            if not f.mounted:
                raise GuestDiskError("Fixture project disk is unmounted")
            return {
                **metadata["project"],
                "mounted": True,
                "verified": True,
                "initialization_sealed": True,
                "used_bytes": f.used_bytes,
            }

        def unmount():
            f.mounted = False
            return {
                k: metadata["project"][k]
                for k in ("project_id", "generation", "filesystem_uuid")
            } | {"mounted": False, "verified": True}

        def trim():
            return {
                k: v for k, v in verify().items() if k != "initialization_sealed"
            } | {"discard_completed": True}

        f.gate.workspace.verify.side_effect = verify
        f.gate.workspace.unmount.side_effect = unmount
        f.gate.workspace.trim.side_effect = trim
        f.runtime = DependencyRuntimeFixture(f)
        f.files = ProjectGuestFiles(
            f.gate,
            root=f.root / "journal",
            workspace=f.workspace,
            dependency_runtime=f.runtime,
        )
        self.guests[sid] = f
        return f

    async def guest(self, sid, path, args, **kwargs):
        if "cloud-init" in " ".join(args):
            self.done("cloud-init")
            return 0, b"done", b""
        metadata = json.loads(
            (self.manager._directory(sid) / "metadata.json").read_text()
        )
        f = self.guest_fixture(sid, metadata)
        if args[-2:] == [FILES_PATH, "--usage"]:
            request = json.loads(kwargs["input_bytes"])
            reply = await asyncio.to_thread(measure, f.gate, request, workspace=f.workspace)
        elif args[-1] == FILES_PATH:
            request = json.loads(kwargs["input_bytes"])
            reply = await asyncio.to_thread(f.files.operate, request)
            if request["action"] == "dependency_run" and f.runtime.state == "running":
                await asyncio.to_thread(f.files.dependencies.worker, request["fence"])
                f.runtime.state = "exited"
            self.done(request["action"])
        elif args[-1].endswith("project_gate.py"):
            request = json.loads(kwargs["input_bytes"])
            action, epoch = request["action"], request["epoch"]
            if action in {"trim", "unmount"}:
                reply = await asyncio.to_thread(f.gate.disk_operation, action, epoch)
            else:
                reply = await asyncio.to_thread(getattr(f.gate, action), epoch)
            self.done(action)
        else:
            self.assertIn(args[-1], {"mount", "verify"})
            if args[-1] == "mount":
                f.mounted = True
            reply = f.gate.workspace.verify()
            self.done(args[-1])
        return 0, json.dumps(reply).encode(), b""

    async def dependency_project(self):
        await self.transition("wake")
        p = self.project()
        self.projects.update(
            self.access,
            self.pid,
            expected_version=p["version"],
            rebuild_command="npm ci",
        )
        f = self.guests[p["sandbox_id"]]
        (f.workspace / "package.json").write_text('{"name":"fixture"}')
        (f.workspace / "package-lock.json").write_text('{"lockfileVersion":3}')
        (f.workspace / "source.js").write_bytes(b"retained source")
        (f.workspace / "node_modules/pkg").mkdir(parents=True)
        (f.workspace / "node_modules/pkg/temporary").write_bytes(b"cache")
        await self.transition("stop")
        return f

    async def until(self, job, operation):
        while (step := self.lifecycle.next_step(job))["operation"] != operation:
            result = await self.backend.execute(job, step, self.access)
            self.lifecycle.acknowledge(job, step, result)
        return step

    async def inspect(self):
        return await self.runtime.inspect(
            self.scope.key, self.pid, generation=self.project()["workspace_generation"]
        )

class VmExecutorTests(VmExecutorFixture, unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_active_vm_keeps_domain_and_reopens_guest_gate(self):
        await self.transition("wake")
        original = self.project()
        guest = self.guests[original["sandbox_id"]]
        (guest.workspace / "report.txt").write_bytes(b"VM snapshot")
        starts = self.effects.count("start")
        result = await self.transition("snapshot")
        self.assertEqual(result["snapshot"]["version_no"], 1)
        self.assertEqual(self.effects.count("start"), starts)
        for key in ("status", "sandbox_id", "workspace_generation", "state_changed_at"):
            self.assertEqual(self.project()[key], original[key], key)
        await self.backend.io_evidence(self.project())
        self.assertEqual(self.projects.read_file(self.access, self.pid, 1, "report.txt"), b"VM snapshot")

    async def test_snapshot_stopped_vm_exports_then_restores_stopped_state(self):
        await self.transition("wake")
        guest = self.guests[self.project()["sandbox_id"]]
        (guest.workspace / "report.txt").write_bytes(b"VM snapshot")
        await self.transition("stop")
        original = self.project()
        (guest.workspace / "report.txt").write_bytes(b"VM new snapshot")
        result = await self.transition("snapshot")
        self.assertEqual(result["snapshot"]["version_no"], 2)
        self.assertEqual((await self.inspect())["state"], "stopped")
        self.assertEqual(self.project()["sandbox_id"], original["sandbox_id"])
        self.assertEqual(self.project()["state_changed_at"], original["state_changed_at"])
        self.assertEqual(self.projects.read_file(self.access, self.pid, 2, "report.txt"), b"VM new snapshot")
        await self.transition("wake")
        await self.backend.io_evidence(self.project())

    async def test_snapshot_plan_requires_quiescence_and_release_order(self):
        await self.transition("wake")
        _, plan = transition_plan(self.project(), "snapshot", None)
        self.backend.preflight(plan)
        plan["steps"].remove("quiesce_workspace")
        with self.assertRaises(ProjectBackendUnavailable):
            self.backend.preflight(plan)

    async def test_tool_evidence_requires_current_epoch_and_no_guest_writers(self):
        await self.transition("wake")
        project = self.project()
        evidence = await self.backend.io_evidence(project)
        self.assertTrue(evidence["quiescent"])
        self.assertEqual(evidence["fence"], project["lifecycle_fence"])
        guest = self.guests[self.record()["sandbox_id"]]
        guest.commands.sandbox_processes.return_value = True
        with self.assertRaises(ProjectConflict):
            await self.backend.io_evidence(project)
        guest.commands.sandbox_processes.return_value = False
        with self.assertRaises(ProjectConflict):
            await self.backend.io_evidence({**project, "lifecycle_fence": project["lifecycle_fence"] + 1})

    async def test_full_stop_hibernate_archive_restore_and_dependency_lifecycle(self):
        f = await self.dependency_project()
        first = self.project()["sandbox_id"]
        first_data = self.store._path(self.data_root / f"p{self.pid}", self.record())
        first_inode = first_data.stat().st_ino
        self.assertEqual((await self.inspect())["state"], "stopped")
        await self.transition("wake")
        self.assertEqual(self.project()["sandbox_id"], first)
        self.assertTrue((f.workspace / "node_modules/pkg/temporary").exists())
        await self.transition("stop")
        await self.transition("hibernate")
        self.assertEqual(self.domains, {})
        self.assertFalse(self.manager._directory(first).exists())
        self.assertEqual(first_data.stat().st_ino, first_inode)
        self.assertFalse((f.workspace / "node_modules").exists())
        self.assertEqual((f.workspace / "source.js").read_bytes(), b"retained source")
        self.assertEqual(len(self.projects.versions(self.access, self.pid)), 1)
        await self.transition("wake")
        second = self.project()["sandbox_id"]
        self.assertNotEqual(first, second)
        self.assertEqual(first_data.stat().st_ino, first_inode)
        self.assertEqual(self.guests[second].runtime.installs, 1)
        self.assertEqual((f.workspace / "node_modules/dep").read_bytes(), b"installed")
        await self.transition("stop")
        await self.transition("hibernate")
        await self.transition("archive")
        self.assertFalse(first_data.exists())
        self.assertTrue((await self.inspect())["deleted"])
        await self.transition("wake")
        p = self.project()
        self.assertEqual(p["workspace_generation"], 2)
        restored = self.guests[p["sandbox_id"]]
        self.assertNotEqual(f.workspace, restored.workspace)
        self.assertEqual(
            (restored.workspace / "source.js").read_bytes(), b"retained source"
        )
        self.assertEqual(
            (restored.workspace / "node_modules/dep").read_bytes(), b"installed"
        )
        self.assertEqual(restored.runtime.installs, 1)
        self.assertEqual((await self.inspect())["guest"]["mode"], "active")
        self.assertIsNone(p["lifecycle_job_id"])
        self.assertEqual(self.host.calls.count("create"), 2)

    async def test_lost_boot_ack_new_lease_keeps_domain_and_disk(self):
        self.request("wake")
        job = self.claim()
        step = await self.until(job, "boot_workspace")
        await self.backend.execute(job, step, self.access)
        original = copy.deepcopy(self.record()["workspace"])
        self.compose()
        replacement = self.claim(now=int(time.time()) + 61)
        resumed = self.lifecycle.next_step(replacement)
        self.assertEqual(resumed["fence"], step["fence"])
        self.assertNotEqual(resumed["request_id"], step["request_id"])
        with self.assertRaises(ProjectConflict):
            await self.executor(job)
        await self.executor(replacement)
        self.assertEqual(self.record()["workspace"], original)
        self.assertEqual(self.effects.count("define"), 1)
        self.assertEqual(self.effects.count("start"), 1)
        self.assertEqual(self.host.calls.count("create"), 1)

    async def test_lost_retirement_ack_reconciles_without_another_boot(self):
        f = await self.dependency_project()
        self.request("hibernate")
        job = self.claim()
        step = await self.until(job, "remove_workspace")
        await self.backend.execute(job, step, self.access)
        starts = self.effects.count("start")
        self.compose()
        await self.executor(self.claim(now=int(time.time()) + 61))
        self.assertEqual(self.project()["status"], "hibernated")
        self.assertEqual(self.effects.count("start"), starts)
        self.assertEqual(self.effects.count("undefine"), 1)
        self.assertEqual((f.workspace / "source.js").read_bytes(), b"retained source")

    async def test_dependency_completion_before_db_ack_is_not_installed_twice(self):
        await self.dependency_project()
        await self.transition("hibernate")
        self.request("wake")
        job = self.claim()
        step = await self.until(job, "rebuild_dependencies")
        await self.backend.execute(job, step, self.access)
        f = self.guests[self.record()["sandbox_id"]]
        self.compose()
        await self.executor(self.claim(now=int(time.time()) + 61))
        self.assertEqual(f.runtime.installs, 1)
        self.assertEqual(f.runtime.starts, 1)
        self.assertEqual(self.project()["status"], "active")

    async def test_lost_guest_activation_reply_retains_reservation_and_resumes(self):
        self.request("wake")
        job = self.claim()
        await self.until(job, "release_workspace")
        self.after["activate"] = OSError("lost active receipt")
        with self.assertRaises(OSError):
            await self.executor(job)
        self.assertTrue(self.record()["maintenance"])
        self.assertEqual(self.project()["status"], "stopped")
        self.compose()
        await self.executor(job)
        self.assertEqual(self.project()["status"], "active")
        self.assertIsNone(self.record()["maintenance"])
        self.assertEqual(self.effects.count("start"), 1)

    async def test_revoked_membership_after_effect_never_releases_to_users(self):
        self.request("wake")
        job = self.claim()
        step = await self.until(job, "boot_workspace")
        await self.backend.execute(job, step, self.access)
        self.access = replace(self.access, member=False)
        with self.assertRaises(PermissionError):
            await self.executor(job)
        self.assertTrue(self.record()["maintenance"])
        self.assertEqual(
            self.guests[self.record()["sandbox_id"]].gate._state()["mode"], "quiesced"
        )

    async def test_failed_dependency_never_marks_active_or_discards_retained_files(
        self,
    ):
        f = await self.dependency_project()
        await self.transition("hibernate")
        self.request("wake")
        job = self.claim()
        await self.until(job, "rebuild_dependencies")
        current = self.guests[self.record()["sandbox_id"]]
        current.runtime.code = 2
        with self.assertRaises((ProjectConflict, SandboxError)):
            await self.executor(job)
        self.assertEqual(self.project()["status"], "hibernated")
        self.assertTrue(self.record()["maintenance"])
        self.assertEqual((f.workspace / "source.js").read_bytes(), b"retained source")
        self.assertEqual(current.gate._state()["mode"], "quiesced")

    async def test_final_state_rejects_closed_gate_and_wrong_epoch(self):
        await self.transition("wake")
        f = self.guests[self.project()["sandbox_id"]]
        fence = self.record()["fence"]
        f.gate.quiesce(fence + 1)
        with self.assertRaises(GuestDiskError):
            await self.inspect()
        f.gate.activate(fence + 2)
        with self.assertRaises(GuestDiskError):
            await self.inspect()

    async def test_stopped_release_rejects_replaced_domain(self):
        await self.transition("wake")
        self.request("stop")
        job = self.claim()
        await self.until(job, "release_workspace")
        identifier = self.record()["workspace"]["domain_uuid"]
        self.domains[identifier]["xml"] = self.domains[identifier]["xml"].replace(
            "unit='MiB'", "unit='GiB'"
        )
        # ElementTree serialization may use double quotes.
        self.domains[identifier]["xml"] = self.domains[identifier]["xml"].replace(
            'unit="MiB"', 'unit="GiB"'
        )
        with self.assertRaises(SandboxError):
            await self.executor(job)
        self.assertTrue(self.record()["maintenance"])

    async def test_retired_inspection_rejects_reappearing_system_directory(self):
        await self.dependency_project()
        sid = self.project()["sandbox_id"]
        await self.transition("hibernate")
        self.manager._directory(sid).mkdir(mode=0o700)
        with self.assertRaises(SandboxError):
            await self.inspect()

    async def test_preflight_checks_full_order_before_effects(self):
        _, plan = transition_plan(self.project(), "wake", None)
        for changes in (
            {"protocol": 2},
            {"backend": "oci"},
            {"reservation": "bad"},
            {"steps": ["delete_volume", "verify_archived"]},
            {
                "steps": [
                    "reserve_workspace",
                    "boot_workspace",
                    "restore_version",
                    "release_workspace",
                    "verify_active",
                ]
            },
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises(ProjectBackendUnavailable),
            ):
                self.backend.preflight({**plan, **changes})
        self.assertFalse((self.data_root / f"p{self.pid}").exists())
        self.assertEqual(self.domains, {})

    async def test_pending_or_foreign_project_cannot_be_inspected(self):
        self.request("wake")
        job = self.claim()
        await self.until(job, "release_workspace")
        with self.assertRaises(SandboxError):
            await self.runtime.inspect(self.scope.key, self.pid, generation=1)
        await self.executor(job)
        with self.assertRaises(SandboxError):
            await self.runtime.inspect("different-group", self.pid, generation=1)

    async def test_final_verification_rejects_guest_writers(self):
        self.request("wake")
        job = self.claim()
        await self.until(job, "verify_active")
        f = self.guests[self.record()["sandbox_id"]]
        f.commands.sandbox_processes.return_value = True
        with self.assertRaises(ProjectConflict):
            await self.executor(job)
        self.assertEqual(self.project()["status"], "stopped")
        f.commands.sandbox_processes.return_value = False
        await self.executor(job)
        self.assertEqual(self.project()["status"], "active")

    async def test_helper_success_flag_alone_is_not_completion(self):
        self.request("wake")
        job = self.claim()
        await self.until(job, "verify_active")
        observed = await self.runtime.inspect(self.scope.key, self.pid, generation=1)
        for changes in (
            {"state": "stopped"},
            {"generation": 999},
            {"pending": True},
            {"maintenance_active": True},
            {"attached": False},
            {"fence": 2**52},
            {"fence": 1},
        ):
            with (
                self.subTest(changes=changes),
                patch.object(
                    self.runtime, "inspect", return_value={**observed, **changes}
                ),
                self.assertRaises(ProjectConflict),
            ):
                await self.executor(job)
        self.assertEqual(self.project()["status"], "stopped")

    async def test_lost_snapshot_reply_does_not_shutdown_before_publication(self):
        await self.transition("wake")
        f = self.guests[self.project()["sandbox_id"]]
        (f.workspace / "unfinished.txt").write_bytes(b"must survive")
        self.request("stop")
        job = self.claim()
        self.after["snapshot"] = OSError("snapshot reply lost")
        with self.assertRaises(OSError):
            await self.executor(job)
        self.assertEqual(self.effects.count("shutdown"), 0)
        self.assertEqual(self.projects.versions(self.access, self.pid), [])
        self.assertTrue(self.record()["pending"])
        self.compose()
        await self.executor(job)
        self.assertEqual(self.project()["status"], "stopped")
        self.assertEqual(len(self.projects.versions(self.access, self.pid)), 1)
        self.assertEqual((f.workspace / "unfinished.txt").read_bytes(), b"must survive")
        self.assertEqual(self.effects.count("shutdown"), 1)

    async def test_archived_restore_resumes_lost_chunk_with_same_generation(self):
        await self.dependency_project()
        await self.transition("hibernate")
        await self.transition("archive")
        self.request("wake")
        job = self.claim()
        self.after["restore_file"] = OSError("restore chunk reply lost")
        with self.assertRaises(OSError):
            await self.executor(job)
        original = copy.deepcopy(self.record()["workspace"])
        self.assertEqual(self.record()["generation"], 2)
        self.assertTrue(self.record()["pending"])
        self.compose()
        await self.executor(self.claim(now=int(time.time()) + 61))
        f = self.guests[self.project()["sandbox_id"]]
        self.assertEqual(self.record()["workspace"], original)
        self.assertEqual(self.project()["workspace_generation"], 2)
        self.assertEqual((f.workspace / "source.js").read_bytes(), b"retained source")
        self.assertEqual(f.runtime.installs, 1)
        self.assertEqual(self.host.calls.count("create"), 2)

    async def test_retired_inspection_rejects_reappearing_domain(self):
        await self.dependency_project()
        original = copy.deepcopy(self.domains)
        await self.transition("hibernate")
        self.domains.update(original)
        with self.assertRaises(SandboxError):
            await self.inspect()

    async def test_host_journal_change_during_inspection_is_rejected(self):
        await self.transition("wake")
        original = self.store.inspection_record
        calls = 0

        def changed(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = original(*args, **kwargs)
            if calls == 2:
                result["status"]["fence"] += 1
            return result

        with (
            patch.object(self.store, "inspection_record", side_effect=changed),
            self.assertRaises(SandboxError),
        ):
            await self.inspect()
