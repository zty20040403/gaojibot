"""Rollback actual fixture files via PostgreSQL, CAS and private backend adapters."""

from dataclasses import replace
import os
import time
import unittest
from unittest.mock import patch

import nonebot
from alembic import command
from alembic.config import Config

nonebot.init()

from src.plugins.ai_chat.project_blobs import ProjectFile
from src.plugins.ai_chat.project_restore import ProjectRestores
from src.plugins.ai_chat.projects import ProjectConflict
from src.plugins.ai_chat.project_commands import command_result
from src.bot_storage.schema import HEAD_REVISION
from src.sandbox_runtime.errors import SandboxError
from tests import test_project_file_executor as oci
from tests import test_project_vm_executor as vm
from tests import test_project_tools as tools
from tests.postgres_fixture import postgres_for_test


def fixture_mount_contents(test):
    """Emulate files disappearing from a mountpoint, not kernel mount behavior."""
    mount, unmount = test.host.mount, test.host.unmount

    def storage(device):
        image = next(
            row["back-file"] for row in test.host.loops() if row["name"] == device
        )
        path = test.root / "fixture-filesystems" / test.host.filesystems[image]["UUID"]
        path.mkdir(parents=True, exist_ok=True)
        return path

    def attach(device, target):
        mount(device, target)
        for item in storage(device).iterdir():
            item.rename(target / item.name)

    def detach(target):
        device = next(
            row["source"] for row in test.host.mounts() if row["target"] == str(target)
        )
        directory = storage(device)
        for item in target.iterdir():
            item.rename(directory / item.name)
        unmount(target)

    test.host.mount, test.host.unmount = attach, detach


class RestoreMixin:
    def restore(self, version=1, *, key=None):
        project = self.projects.get(self.access, self.pid)
        return self.lifecycle.request(
            self.access,
            self.pid,
            "restore",
            expected_version=project["version"],
            request_key=key or f"restore-{project['version']}",
            restore_version=version,
        )

    def status(self):
        return ProjectRestores(self.projects).status(self.access, self.pid)


class OciRestoreTests(RestoreMixin, unittest.IsolatedAsyncioTestCase):
    claim = oci.ProjectFileExecutorTests.claim
    wake = oci.ProjectFileExecutorTests.wake
    save = oci.ProjectFileExecutorTests.save

    async def asyncSetUp(self):
        await oci.ProjectFileExecutorTests.asyncSetUp(self)
        fixture_mount_contents(self)

    def request(self, action="wake"):
        project = self.projects.get(self.access, self.pid)
        return self.lifecycle.request(
            self.access,
            self.pid,
            action,
            expected_version=project["version"],
            request_key=f"{action}-{project['version']}",
        )

    async def baseline(self):
        root = await self.wake()
        (root / "index.html").write_text("version one")
        self.save()
        await self.executor(self.claim())
        (root / "index.html").write_text("unsaved version two")
        (root / "extra.txt").write_text("not in version one")
        return root

    async def test_browser_only_format_three_rollback_keeps_login_and_omits_cache(self):
        root = await self.wake()
        cache = root / ".gaoji/browser/Default/Cache"
        cache.mkdir(parents=True)
        (cache / "data").write_bytes(b"cache")
        (cache.parent / "Cookies").write_bytes(b"original login")
        self.save()
        await self.executor(self.claim())
        original = self.projects.versions(self.access, self.pid)[0]
        (cache.parent / "Cookies").write_bytes(b"modified login")
        self.restore()
        await self.executor(self.claim())
        current = self.projects.get(self.access, self.pid)
        restored = self.volumes.binding(self.scope.key, self.pid, generation=current["workspace_generation"])
        self.assertEqual((restored / ".gaoji/browser/Default/Cookies").read_bytes(), b"original login")
        self.assertFalse((restored / ".gaoji/browser/Default/Cache").exists())
        self.assertEqual(self.projects.versions(self.access, self.pid)[0]["manifest_sha256"], original["manifest_sha256"])

    async def test_active_rollback_saves_unsaved_work_and_creates_verified_new_version(
        self,
    ):
        await self.baseline()
        before = self.projects.get(self.access, self.pid)
        self.restore()
        self.assertFalse(self.status()["restored"])
        result = await self.executor(self.claim())
        current = self.projects.get(self.access, self.pid)
        self.assertEqual(
            current["workspace_generation"], before["workspace_generation"] + 1
        )
        self.assertNotEqual(current["sandbox_id"], before["sandbox_id"])
        root = self.volumes.binding(self.scope.key, self.pid, generation=2)
        self.assertEqual((root / "index.html").read_text(), "version one")
        self.assertFalse((root / "extra.txt").exists())
        self.assertEqual(
            self.projects.read_file(self.access, self.pid, 2, "extra.txt"),
            b"not in version one",
        )
        self.assertEqual(
            self.projects.read_file(self.access, self.pid, 2, "index.html"),
            b"unsaved version two",
        )
        self.assertEqual(result["snapshot"]["version_no"], 3)
        self.assertTrue(self.status()["restored"])
        await self.backend.io_evidence(current)
        self.restore(2)
        await self.executor(self.claim())
        root = self.volumes.binding(self.scope.key, self.pid, generation=3)
        self.assertEqual((root / "extra.txt").read_text(), "not in version one")
        self.assertEqual((root / "index.html").read_text(), "unsaved version two")
        self.assertEqual(len(self.projects.versions(self.access, self.pid)), 4)

    async def test_stopped_hibernated_and_archived_sources_restore_on_fresh_volumes(
        self,
    ):
        await self.baseline()
        for state in ("stop", "hibernate", "archive"):
            for action in {
                "stop": ["stop"],
                "hibernate": ["stop", "hibernate"],
                "archive": ["stop", "hibernate", "archive"],
            }[state]:
                self.request(action)
                await self.executor(self.claim())
            generation = self.projects.get(self.access, self.pid)[
                "workspace_generation"
            ]
            self.restore()
            await self.executor(self.claim())
            root = self.volumes.binding(
                self.scope.key, self.pid, generation=generation + 1
            )
            self.assertEqual((root / "index.html").read_text(), "version one")
            self.assertTrue(self.status()["restored"])

    async def test_unmaterialized_project_restores_retained_version(self):
        row = self.projects.get(self.access, self.pid)
        self.projects.snapshot(
            self.access,
            self.pid,
            [ProjectFile("note.txt", b"retained")],
            expected_version=row["version"],
        )
        self.restore()
        await self.executor(self.claim())
        root = self.volumes.binding(self.scope.key, self.pid, generation=1)
        self.assertEqual((root / "note.txt").read_bytes(), b"retained")
        self.assertEqual(self.status()["version"], "v2")

    async def test_corrupt_source_stops_before_existing_workspace_is_touched(self):
        root = await self.baseline()
        manifest = self.projects.manifest(self.access, self.pid, 1)
        blob = self.projects.blobs.path(manifest["files"][0]["sha256"])
        blob.chmod(0o600)
        blob.write_bytes(b"corrupt")
        self.restore()
        count = len(self.docker.commands)
        with self.assertRaises(ValueError):
            await self.executor(self.claim())
        self.assertEqual((root / "index.html").read_text(), "unsaved version two")
        self.assertEqual(len(self.docker.commands), count)
        self.assertEqual(self.status()["status"], "needs_attention")
        self.assertFalse(self.status()["restored"])

    async def test_failed_backup_never_removes_original_data(self):
        root = await self.baseline()
        self.restore()
        with patch.object(
            self.projects, "stage_snapshot_file", side_effect=OSError("no space")
        ):
            with self.assertRaises(OSError):
                await self.executor(self.claim())
        self.assertEqual((root / "extra.txt").read_text(), "not in version one")
        self.assertFalse(self.status()["restored"])
        volume = self.volumes.status(self.scope.key, self.pid, generation=1)
        self.assertFalse(volume.get("deleted", False))

    async def test_lost_publication_receipt_resumes_without_duplicate_version(self):
        await self.baseline()
        self.restore()
        job = self.claim()
        publish = ProjectRestores.publish

        def lost(*args):
            publish(*args)
            raise OSError("publication committed but response lost")

        with patch.object(ProjectRestores, "publish", side_effect=lost, autospec=True):
            with self.assertRaises(OSError):
                await self.executor(job)
        self.assertEqual(len(self.projects.versions(self.access, self.pid)), 3)
        self.assertFalse(self.status()["restored"])
        successor = self.claim(now=int(time.time()) + 61)
        with self.assertRaises(ProjectConflict):
            await self.executor(job)
        await self.executor(successor)
        self.assertEqual(len(self.projects.versions(self.access, self.pid)), 3)
        self.assertTrue(self.status()["restored"])

    async def test_old_volume_delete_receipt_loss_recovers_same_rollback(self):
        await self.baseline()
        self.restore()
        job = self.claim()
        original = self.client.project_volume

        async def lost(action, *args, **kwargs):
            result = await original(action, *args, **kwargs)
            if action == "delete":
                raise SandboxError("lost deletion receipt")
            return result

        with patch.object(self.client, "project_volume", side_effect=lost):
            with self.assertRaises(SandboxError):
                await self.executor(job)
        self.assertEqual(
            self.projects.read_file(self.access, self.pid, 2, "extra.txt"),
            b"not in version one",
        )
        self.assertFalse(self.status()["restored"])
        await self.executor(self.claim(now=int(time.time()) + 61))
        self.assertEqual(self.status()["version"], "v3")
        self.assertEqual(len(self.projects.versions(self.access, self.pid)), 3)

    async def test_publication_transaction_failure_and_schema_downgrade_keep_journal(
        self,
    ):
        await self.baseline()
        self.restore()
        event = self.projects._event

        def crash(cursor, pid, access, kind, *args):
            if kind == "version_restored":
                raise RuntimeError("transaction failure")
            return event(cursor, pid, access, kind, *args)

        with patch.object(self.projects, "_event", side_effect=crash):
            with self.assertRaises(RuntimeError):
                await self.executor(self.claim())
        self.assertEqual(len(self.projects.versions(self.access, self.pid)), 2)
        db = postgres_for_test(self)
        with patch.dict(
            os.environ,
            {
                "AI_POSTGRES_DSN": os.environ["TEST_POSTGRES_DSN"],
                "AI_POSTGRES_SCHEMA": db.schema,
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "restore journals"):
                command.downgrade(Config("alembic.ini"), "0052_project_autosave")
        db.require_revision(HEAD_REVISION)
        await self.executor(self.claim(now=int(time.time()) + 61))
        self.assertEqual(self.status()["version"], "v3")

    async def test_scope_permissions_and_replay_are_bound_to_original_target(self):
        await self.baseline()
        with self.assertRaises(PermissionError):
            self.lifecycle.request(
                replace(self.access, member=False),
                self.pid,
                "restore",
                expected_version=None,
                request_key="denied",
                restore_version=1,
            )
        first = self.restore(key="once")
        self.projects.create(self.access, "other")
        replay = self.lifecycle.request(
            self.access,
            None,
            "restore",
            expected_version=None,
            request_key="once",
            restore_version=1,
        )
        self.assertEqual(replay["job_id"], first["job_id"])
        with self.assertRaises(ProjectConflict):
            self.restore(2, key="once")
        with self.assertRaises(ProjectConflict):
            self.projects.snapshot(self.access, self.pid, [], expected_version=None)
        self.executor.resolve_access = lambda project, actor: replace(
            self.access, member=False
        )
        with self.assertRaises(PermissionError):
            await self.executor(self.claim())
        self.assertFalse(self.status()["restored"])


class VmRestoreTests(
    RestoreMixin, vm.VmExecutorFixture, unittest.IsolatedAsyncioTestCase
):
    async def test_stopped_restore_rebuilds_target_recipe_once_after_lost_ack(self):
        original = await self.dependency_project()
        self.projects.update(
            self.access,
            self.pid,
            expected_version=self.project()["version"],
            rebuild_command="",
        )
        (original.workspace / "source.js").write_bytes(b"new unsaved source")
        self.restore()
        job = self.claim()
        step = await self.until(job, "rebuild_dependencies")
        self.assertEqual(step["arguments"]["rebuild_command"], "npm ci")
        self.assertFalse(self.status()["restored"])
        self.assertEqual(len(self.projects.versions(self.access, self.pid)), 3)
        await self.backend.execute(job, step, self.access)
        restored = self.guests[self.record()["sandbox_id"]]
        self.compose()
        await self.executor(self.claim(now=int(time.time()) + 61))
        self.assertEqual(restored.runtime.installs, 1)
        self.assertEqual(restored.runtime.starts, 1)
        self.assertEqual(
            (restored.workspace / "node_modules/dep").read_bytes(), b"installed"
        )
        self.assertFalse((restored.workspace / "node_modules/pkg/temporary").exists())
        self.assertEqual(
            (restored.workspace / "source.js").read_bytes(), b"retained source"
        )
        self.assertEqual(
            self.projects.read_file(self.access, self.pid, 2, "source.js"),
            b"new unsaved source",
        )
        self.assertEqual(self.project()["rebuild_command"], "npm ci")
        self.assertEqual(self.status()["version"], "v3")
        self.assertEqual(len(self.projects.versions(self.access, self.pid)), 3)

    async def test_guest_restore_preserves_current_data_and_verifies_new_generation(
        self,
    ):
        await self.transition("wake")
        workspace = self.guests[self.project()["sandbox_id"]].workspace
        (workspace / "report.txt").write_text("old report")
        await self.transition("snapshot")
        (workspace / "report.txt").write_text("unsaved report")
        self.restore()
        await self.executor(self.claim())
        project = self.project()
        self.assertEqual(project["workspace_generation"], 2)
        self.assertEqual(
            self.guests[project["sandbox_id"]]
            .workspace.joinpath("report.txt")
            .read_text(),
            "old report",
        )
        self.assertEqual(
            self.projects.read_file(self.access, self.pid, 2, "report.txt"),
            b"unsaved report",
        )
        self.assertEqual(self.status()["version"], "v3")
        await self.backend.io_evidence(project)

    async def test_archived_guest_restore_starts_a_new_generation(self):
        await self.transition("wake")
        workspace = self.guests[self.project()["sandbox_id"]].workspace
        (workspace / "note.txt").write_text("original")
        for action in ("stop", "hibernate", "archive"):
            await self.transition(action)
        self.restore()
        await self.executor(self.claim())
        self.assertTrue(self.status()["restored"])
        self.assertEqual(self.project()["workspace_generation"], 2)


class RestoreToolTests(unittest.IsolatedAsyncioTestCase):
    call = tools.ProjectToolTests.call
    create = tools.ProjectToolTests.create

    async def asyncSetUp(self):
        await tools.ProjectToolTests.asyncSetUp(self)
        fixture_mount_contents(self)

    async def test_command_and_status_only_claim_completion_after_verification(self):
        created = await self.create()
        await self.worker.run_once()
        pid = int(created["project"]["handle"][4:])
        root = self.volumes.binding(self.scope.key, pid)
        (root / "hello.txt").write_text("original")
        await self.call("project_snapshot")
        await self.worker.run_once()
        (root / "hello.txt").write_text("edited")
        result = await command_result(self.handlers, self.context.event, "回滚 v1")
        self.assertIn("回滚已排队", result)
        self.assertNotIn("回滚已核实", result)
        await self.worker.run_once()
        result = await command_result(self.handlers, self.context.event, "状态")
        self.assertIn("v1 的内容恢复为 v3", result)
        self.assertTrue((await self.call("project_status"))["restore"]["restored"])
        self.assertFalse(
            (await self.call("project_restore", {"version_handle": "v0"}))["ok"]
        )
        self.assertFalse(
            (
                await self.call(
                    "project_restore", {"version_handle": "v1", "scope": "other"}
                )
            )["ok"]
        )
