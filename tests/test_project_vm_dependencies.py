"""Real retained files and journals, explicit systemd/QGA fixtures (not native VM)."""

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.local_runtime.files import write_json
from src.project_content import manifest_bytes
from src.sandbox_runtime.errors import SandboxError
from src.sandbox_runtime.project_vm_dependency_guest import (
    LinuxDependencyRuntime,
    UV_VERSION,
)
from src.sandbox_runtime.project_vm_file_guest import ProjectGuestFiles, TRANSFER_BYTES
from src.sandbox_runtime.project_vm_files import VmProjectFiles
from src.sandbox_runtime.project_vm_guest import GuestDiskError
from tests import test_project_vm_files as fixtures

RESERVATION = fixtures.RESERVATION


class DependencyRuntimeFixture:
    def __init__(self, fixture):
        self.fixture = fixture
        self.state, self.starts, self.installs = None, 0, 0
        self.boot = "27abb2ef-3b81-4a4f-a98b-56059457dbb3"
        self.mutate = False
        self.code = 0

    def boot_id(self):
        return self.boot

    def start(self, run):
        self.starts += 1
        self.state = "running"

    def inspect(self, run):
        return self.state

    def stop(self, run):
        self.state = None

    def install_and_run(self, gate, run):
        self.installs += 1
        root = self.fixture.workspace
        (root / "node_modules").mkdir(exist_ok=True)
        (root / "node_modules/dep").write_bytes(b"installed")
        if self.mutate:
            (root / "source.js").write_bytes(b"unwanted change")
        return self.code


def source_fixture(f):
    (f.workspace / "package.json").write_text('{"name":"fixture"}')
    (f.workspace / "package-lock.json").write_text('{"lockfileVersion":3}')
    (f.workspace / "source.js").write_bytes(b"retained source")
    snapshot, _ = f.call("snapshot", rebuild_command="npm ci")
    page, _ = f.call("page", manifest_sha256=snapshot["manifest_sha256"], index=0)
    document = {
        "format": snapshot["format"],
        "rebuild": snapshot["rebuild"],
        "files": page["files"],
    }
    return document, manifest_bytes(document, quota_bytes=f.config["quota_bytes"])


class GuestDependencyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="gaoji-vm-dependency-")
        self.addCleanup(directory.cleanup)
        self.f = fixtures.GuestFixture(Path(directory.name))
        self.f.gate.quiesce(5)
        self.document, self.encoded = source_fixture(self.f)
        self.digest = hashlib.sha256(self.encoded).hexdigest()
        self.runtime = DependencyRuntimeFixture(self.f)
        self.reopen()
        self.f.gate.workspace.verify.return_value = {
            "used_bytes": 1024,
            "quota_bytes": self.f.config["quota_bytes"],
        }

    def reopen(self, root=None):
        self.f.files = ProjectGuestFiles(
            self.f.gate,
            root=root or self.f.root / "journal",
            workspace=self.f.workspace,
            dependency_runtime=self.runtime,
        )

    def call(self, action, **options):
        args = dict(fence=7, manifest_sha256=self.digest)
        args.update(options)
        return self.f.call("dependency_" + action, **args)[0]

    def begin(self):
        return self.call("begin", manifest_size=len(self.encoded), tasks=64)

    def prepare(self):
        self.begin()
        return self.call("manifest", offset=0, payload=self.encoded)

    def worker(self):
        try:
            self.f.files.dependencies.worker(7)
        except Exception:
            self.runtime.state = "failed"
            raise
        else:
            self.runtime.state = "exited"

    def test_fresh_guest_imports_metadata_without_overwriting_retained_source(self):
        self.reopen(root=self.f.root / "fresh-journal")
        self.assertTrue(self.prepare()["source_ready"])
        self.assertEqual(self.call("run")["state"], "starting")
        self.worker()
        self.assertEqual(self.call("status")["state"], "completed")
        self.assertEqual(
            (self.f.workspace / "source.js").read_bytes(), b"retained source"
        )
        self.assertEqual(
            (self.f.workspace / "node_modules/dep").read_bytes(), b"installed"
        )
        self.assertEqual(self.runtime.installs, 1)

    def test_chunks_and_replayed_final_chunk_have_no_duplicate_installer(self):
        self.begin()
        self.call("manifest", offset=0, payload=self.encoded[:30])
        self.reopen()
        self.assertFalse(self.begin()["source_ready"])
        with self.assertRaises(SandboxError):
            self.call("manifest", offset=0, payload=b"WRONG")
        self.call("manifest", offset=0, payload=self.encoded)
        self.assertTrue(
            self.call("manifest", offset=0, payload=self.encoded)["source_ready"]
        )
        self.call("run")
        self.reopen()
        self.assertTrue(self.begin()["source_ready"])
        self.call("run")
        self.assertEqual(self.runtime.starts, 1)
        self.worker()
        self.call("run")
        self.assertFalse(self.begin()["pending"])
        self.call("status")
        self.assertEqual(self.runtime.installs, 1)

    def test_preparation_crash_before_fence_is_retryable(self):
        original = self.f.files.volumes._begin
        with patch.object(
            self.f.files.volumes, "_begin", side_effect=OSError("interrupted admission")
        ):
            with self.assertRaises(OSError):
                self.begin()
        self.assertEqual(self.f.record()["fence"], 6)
        self.assertFalse(self.f.record()["pending"])
        with patch.object(self.f.files.volumes, "_begin", wraps=original):
            self.assertTrue(self.prepare()["source_ready"])
        self.assertEqual(self.runtime.starts, 0)

    def test_completion_before_volume_commit_is_reconciled_not_reinstalled(self):
        self.prepare()
        self.call("run")
        self.worker()
        with patch.object(
            self.f.files.volumes, "_finish", side_effect=OSError("lost commit")
        ):
            with self.assertRaises(OSError):
                self.call("status")
        self.assertTrue(self.f.record()["pending"])
        self.reopen()
        self.assertEqual(self.begin()["state"], "running")
        self.assertEqual(self.call("run")["state"], "completed")
        self.assertFalse(self.f.record()["pending"])
        self.assertEqual(self.runtime.installs, 1)

    def test_running_status_does_not_wait_for_installer_io_lock(self):
        self.prepare()
        self.call("run")
        with self.f.gate.maintenance(7):
            self.assertTrue(self.begin()["pending"])
            self.assertEqual(self.call("status")["state"], "starting")

    def test_lost_start_reply_reconciles_existing_supervisor(self):
        self.prepare()
        original = self.runtime.start

        def lost(run):
            original(run)
            raise OSError("lost systemd acknowledgement")

        with (
            patch.object(self.runtime, "start", side_effect=lost),
            self.assertRaises(OSError),
        ):
            self.call("run")
        self.reopen()
        self.call("run")
        self.assertEqual(self.runtime.starts, 1)
        self.worker()
        self.assertEqual(self.call("run")["state"], "completed")

    def test_never_started_supervisor_may_start_once_at_same_boot(self):
        self.prepare()
        with (
            patch.object(self.runtime, "start", side_effect=OSError("not dispatched")),
            self.assertRaises(OSError),
        ):
            self.call("run")
        self.assertEqual(self.runtime.starts, 0)
        self.call("status")
        self.assertEqual(self.runtime.starts, 0)
        self.call("run")
        self.assertEqual(self.runtime.starts, 1)

    def test_reboot_does_not_repeat_an_uncertain_installer(self):
        self.prepare()
        self.call("run")
        self.runtime.state = None
        self.runtime.boot = "different-boot"
        self.assertEqual(self.call("run")["state"], "failed")
        self.assertEqual(self.runtime.starts, 1)
        self.assertTrue(self.f.record()["pending"])

    def test_deadline_prevents_new_start_and_drains_existing_worker(self):
        for running in (False, True):
            with self.subTest(running=running):
                self.setUp()
                self.prepare()
                self.call("run")
                self.runtime.state = "running" if running else None
                self.f.files.dependencies.update(7, deadline=int(time.time()) - 1)
                self.assertEqual(self.call("run")["state"], "failed")
                self.assertIsNone(self.runtime.state)
                self.assertEqual(self.runtime.starts, 1)

    def test_lockfile_change_or_retained_change_blocks_start(self):
        self.prepare()
        (self.f.workspace / "source.js").write_bytes(b"changed")
        with self.assertRaisesRegex(SandboxError, "retained"):
            self.call("run")
        (self.f.workspace / "source.js").write_bytes(b"retained source")
        (self.f.workspace / "package-lock.json").write_text('{"lockfileVersion":2}')
        with self.assertRaisesRegex(SandboxError, "inputs changed"):
            self.call("run")
        self.assertEqual(self.runtime.starts, 0)

    def test_script_changes_retained_bytes_or_fails_do_not_complete(self):
        for mutation, code in ((True, 0), (False, 9)):
            with self.subTest(mutation=mutation, code=code):
                self.setUp()
                self.prepare()
                self.call("run")
                self.runtime.mutate, self.runtime.code = mutation, code
                with self.assertRaises(SandboxError):
                    self.worker()
                self.assertEqual(self.call("status")["state"], "failed")
                self.assertTrue(self.f.record()["pending"])
                self.assertEqual(self.runtime.installs, 1)
                self.call("run")
                self.assertEqual(self.runtime.installs, 1)

    def test_background_child_blocks_completion(self):
        self.prepare()
        self.call("run")
        original = self.runtime.install_and_run

        def leftover(gate, run):
            code = original(gate, run)
            self.f.commands.empty.return_value = False
            return code

        with patch.object(self.runtime, "install_and_run", side_effect=leftover):
            with self.assertRaisesRegex(SandboxError, "drain"):
                self.worker()
        with self.assertRaises(GuestDiskError):
            self.call("status")
        self.assertTrue(self.f.record()["pending"])
        self.assertEqual(self.runtime.starts, 1)

    def test_quota_rejects_before_dispatch_and_success_needs_fresh_tree(self):
        self.prepare()
        self.f.gate.workspace.verify.return_value = {
            "used_bytes": 96,
            "quota_bytes": 100,
        }
        with self.assertRaisesRegex(SandboxError, "dependency_limit"):
            self.call("run")
        self.assertEqual(self.runtime.starts, 0)
        self.f.gate.workspace.verify.return_value = {
            "used_bytes": 10,
            "quota_bytes": 100,
        }
        self.call("run")
        self.worker()
        self.call("run")
        (self.f.workspace / "source.js").write_bytes(b"changed after completion")
        with self.assertRaises(SandboxError):
            self.call("status")

    def test_reservation_fence_source_and_resource_contract_are_frozen(self):
        self.prepare()
        for args in (
            {"reservation": "c" * 32},
            {"fence": 6},
            {"manifest_sha256": "f" * 64},
        ):
            with self.subTest(args=args), self.assertRaises(SandboxError):
                self.call("run", **args)
        with self.assertRaises(SandboxError):
            self.call("begin", manifest_size=len(self.encoded), tasks=128)
        for tasks in (True, 0, 4097):
            with self.assertRaises(SandboxError):
                self.call("begin", manifest_size=len(self.encoded), tasks=tasks)
        self.assertEqual(self.runtime.starts, 0)

    def test_corrupt_or_replaced_journal_does_not_address_another_service(self):
        self.prepare()
        path = self.f.root / f"journal/p{self.f.config['project_id']}/dependency.json"
        run = json.loads(path.read_text())
        run["unit"] = "unrelated.service"
        write_json(path, run)
        with self.assertRaises(SandboxError):
            self.call("run")
        self.assertEqual(self.runtime.starts, 0)


class DependencyBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.holder = fixtures.VmFileBridgeTests()
        await self.holder.asyncSetUp()
        self.addCleanup(self.holder.holder.fixture.doCleanups)
        self.f = self.holder.fixture
        self.client, self.manager = self.holder.client, self.holder.manager
        self.document, self.encoded = source_fixture(self.f)
        self.digest = hashlib.sha256(self.encoded).hexdigest()
        self.runtime = DependencyRuntimeFixture(self.f)
        self.f.files.dependencies.runtime = self.runtime
        self.f.gate.workspace.verify.return_value = {
            "used_bytes": 100,
            "quota_bytes": self.f.config["quota_bytes"],
        }
        self.context = AsyncMock()

    async def prepare(self):
        return await self.client.project_rebuild_prepare(
            "scope",
            1,
            generation=1,
            fence=7,
            reservation=RESERVATION,
            manifest=self.document,
            check_context=self.context,
        )

    async def call(self, action="run"):
        return await self.client.project_rebuild(
            action,
            "scope",
            1,
            generation=1,
            fence=7,
            reservation=RESERVATION,
            manifest_sha256=self.digest,
        )

    async def test_host_upload_then_poll_resumes_worker_and_acknowledges_completion(
        self,
    ):
        self.assertTrue((await self.prepare())["source_ready"])
        self.assertTrue((await self.call())["pending"])
        self.assertTrue(self.holder.holder.fixture.record()["pending"])
        self.client = VmProjectFiles(self.holder.bridge)
        await self.prepare()
        await self.call()
        self.assertEqual(self.runtime.starts, 1)
        self.f.files.dependencies.worker(7)
        self.runtime.state = "exited"
        self.assertFalse((await self.call())["pending"])
        self.assertEqual((await self.call("status"))["state"], "completed")
        self.assertFalse(self.holder.holder.fixture.record()["pending"])
        self.assertGreaterEqual(self.context.await_count, 3)

    async def test_lost_final_upload_and_finish_receipts_reconcile_both_journals(self):
        original = self.holder.guest
        lost_action = "dependency_manifest"

        async def lost(*args, **kwargs):
            result = await original(*args, **kwargs)
            if json.loads(kwargs["input_bytes"])["action"] == lost_action:
                raise SandboxError("lost QGA reply")
            return result

        self.manager._guest_exec_raw.side_effect = lost
        with self.assertRaises(SandboxError):
            await self.prepare()
        lost_action = ""
        await self.prepare()
        await self.call()
        self.f.files.dependencies.worker(7)
        self.runtime.state = "exited"
        lost_action = "dependency_run"
        with self.assertRaises(SandboxError):
            await self.call()
        self.assertTrue(self.holder.holder.fixture.record()["pending"])
        lost_action = ""
        self.assertEqual((await self.call())["state"], "completed")
        self.assertEqual(self.runtime.installs, 1)

    async def test_invalid_completion_and_changed_limit_cannot_release_host(self):
        await self.prepare()
        with self.assertRaises(SandboxError):
            await self.client.project_files(
                "dependency_begin",
                "scope",
                1,
                generation=1,
                fence=7,
                reservation=RESERVATION,
                manifest_sha256=self.digest,
                manifest_size=len(self.encoded),
                tasks=self.manager.pids_limit + 1,
            )
        original = self.holder.guest

        async def wrong(*args, **kwargs):
            code, data, error = await original(*args, **kwargs)
            reply = json.loads(data)
            reply["receipt"].update(state="completed", pending=False, exit_code=True)
            return code, json.dumps(reply).encode(), error

        self.manager._guest_exec_raw.side_effect = wrong
        with self.assertRaises(SandboxError):
            await self.call()
        self.assertTrue(self.holder.holder.fixture.record()["pending"])

    async def test_manifest_stream_rechecks_membership_between_chunks(self):
        self.context.side_effect = [None, PermissionError("membership revoked")]
        with self.assertRaises(PermissionError):
            await self.prepare()
        self.assertEqual(self.runtime.starts, 0)
        self.assertFalse(self.f.files.dependencies.runtime.installs)
        for call in self.manager._guest_exec_raw.await_args_list:
            self.assertLessEqual(
                len(json.loads(call.kwargs["input_bytes"])["payload"]),
                (TRANSFER_BYTES + 2) // 3 * 4,
            )


class LinuxDependencyRuntimeTests(unittest.TestCase):
    def run_record(self):
        return {
            "unit": "gaoji-dependency-" + "a" * 32 + ".service",
            "fence": 7,
            "arguments": {"tasks": 64, "manifest_sha256": "b" * 64},
            "deadline": int(time.time()) + 900,
            "command": "npm ci --ignore-scripts",
        }

    def test_transient_supervisor_is_bound_and_does_not_restart(self):
        run = self.run_record()
        command = LinuxDependencyRuntime.start_command(run)
        self.assertIn("--property=RemainAfterExit=yes", command)
        self.assertIn("--property=Restart=no", command)
        self.assertIn("--property=KillMode=control-group", command)
        self.assertIn(
            "--description=" + LinuxDependencyRuntime.description(run), command
        )
        self.assertEqual(command[-2:], ["--dependency-worker", "7"])
        self.assertNotIn("--slice=gaojiproject.slice", command)

    def test_installer_is_unprivileged_with_no_inherited_environment(self):
        gate = Mock()
        gate.commands.run.return_value = 0
        with (
            patch.object(Path, "is_file", return_value=True),
            patch.object(LinuxDependencyRuntime, "trusted_path"),
        ):
            self.assertEqual(
                LinuxDependencyRuntime.install_and_run(gate, self.run_record()), 0
            )
        argv, timeout, tasks = gate.commands.run.call_args.args
        self.assertEqual(
            argv[:6], ["/usr/sbin/runuser", "-u", "sandbox", "--", "/usr/bin/env", "-i"]
        )
        self.assertEqual(
            argv[-1], "cd /workspace && exec /usr/bin/npm ci --ignore-scripts"
        )
        self.assertEqual(tasks, 64)
        self.assertLessEqual(timeout, 900)

    def test_uv_uses_repository_pin_even_when_tool_already_exists(self):
        gate = Mock()
        gate.commands.run.return_value = 0
        run = {**self.run_record(), "command": "uv sync --frozen"}
        with (
            patch.object(Path, "is_file", return_value=True),
            patch.object(LinuxDependencyRuntime, "trusted_path"),
        ):
            LinuxDependencyRuntime.install_and_run(gate, run)
        first = gate.commands.run.call_args_list[0].args[0]
        self.assertIn("--isolated", first)
        self.assertIn("uv==" + UV_VERSION, first)
        self.assertIn("runuser", gate.commands.run.call_args.args[0][0])

    def test_foreign_supervisor_identity_is_rejected(self):
        run = self.run_record()
        values = dict(
            Id=run["unit"],
            LoadState="loaded",
            ActiveState="active",
            SubState="exited",
            Description=LinuxDependencyRuntime.description(run),
            User="root",
            Result="success",
            ExecMainStatus="0",
        )
        runtime = LinuxDependencyRuntime()
        with patch("subprocess.run") as execute:
            execute.return_value = subprocess.CompletedProcess(
                [], 0, "\n".join(f"{k}={v}" for k, v in values.items())
            )
            self.assertEqual(runtime.inspect(run), "exited")
            values["Description"] = "foreign"
            execute.return_value.stdout = "\n".join(
                f"{k}={v}" for k, v in values.items()
            )
            with self.assertRaises(SandboxError):
                runtime.inspect(run)


if __name__ == "__main__":
    unittest.main()
