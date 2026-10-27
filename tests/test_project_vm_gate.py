"""Real filesystem/lock epoch tests; systemd and guest mounts are explicit fixtures."""

import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.sandbox_runtime.project_vm_gate import (
    GuestDiskError, LinuxGuestCommands, ProjectGuestGate, guest_lock,
)
from src.sandbox_runtime.project_vm_io import host_lock
from src.sandbox_runtime.docker import SandboxError
from tests.test_project_vm_guest import binding
from tests import test_project_vm_mounts as mount_tests

RESERVATION, SID = mount_tests.RESERVATION, mount_tests.SID


class GateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gaoji-vm-gate-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.commands = Mock(spec=LinuxGuestCommands)
        self.commands.empty.return_value = True
        self.commands.sandbox_processes.return_value = False
        self.commands.run.return_value = 7
        self.workspace = Mock()
        self.gate = self.reopen()

    def reopen(self):
        return ProjectGuestGate(binding().guest_config(), state_path=self.root / "gate.json",
                                lock_root=self.root, commands=self.commands, workspace=self.workspace)

    def run_command(self, epoch=2, **changes):
        return self.gate.run(**{"epoch": epoch, "argv": ["/bin/sh", "-lc", "echo $HOME"],
                               "timeout": 10, "tasks": 128, **changes})

    def test_closed_by_default_and_reopen_preserves_epoch(self):
        with self.assertRaises(GuestDiskError):
            self.run_command()
        self.commands.run.assert_not_called()
        self.assertEqual(self.gate.activate(2)["mode"], "active")
        self.gate = self.reopen()
        self.assertEqual(self.run_command(), 7)
        self.assertEqual(self.gate.activate(2)["epoch"], 2)
        self.assertEqual(self.gate.state_path.stat().st_mode & 0o777, 0o600)

    def test_inspection_is_read_only_and_requires_exact_active_epoch(self):
        self.workspace.verify.return_value = {"used_bytes": 1234}
        with self.assertRaises(GuestDiskError):
            self.gate.inspect(2)
        self.gate.activate(2)
        before = self.gate.state_path.read_bytes()
        observed = self.gate.inspect(2)
        self.assertEqual(observed["used_bytes"], 1234)
        self.assertFalse(observed["busy"])
        self.commands.empty.return_value = False
        self.assertTrue(self.gate.inspect(2)["busy"])
        for epoch in (1, 3, True):
            with self.assertRaises(GuestDiskError):
                self.gate.inspect(epoch)
        self.assertEqual(self.gate.state_path.read_bytes(), before)

    def test_quiescence_revokes_old_requests_even_after_reactivation(self):
        self.gate.activate(2)
        self.assertEqual(self.gate.quiesce(3)["mode"], "quiesced")
        self.assertEqual(self.reopen().quiesce(3)["mode"], "quiesced")
        with self.assertRaises(GuestDiskError):
            self.run_command()
        self.gate.activate(4)
        with self.assertRaises(GuestDiskError):
            self.run_command()
        self.assertEqual(self.run_command(4), 7)
        self.commands.run.assert_called_once()

    def test_running_foreground_and_failed_stop_leave_closed_journal(self):
        self.gate.activate(2)
        with guest_lock(self.root / "gaoji-project-io.lock", shared=True):
            with self.assertRaisesRegex(GuestDiskError, "did not drain"):
                self.gate.quiesce(3, timeout=0)
        self.assertEqual(self.reopen()._state()["mode"], "closing")
        with self.assertRaises(GuestDiskError):
            self.gate.activate(4)
        self.assertEqual(self.gate.quiesce(3)["mode"], "quiesced")
        self.gate.activate(4)
        self.commands.stop.side_effect = GuestDiskError("stop lost")
        with self.assertRaises(GuestDiskError):
            self.gate.quiesce(5)
        self.assertEqual(self.reopen()._state()["mode"], "closing")
        with self.assertRaises(GuestDiskError):
            self.run_command(4)

    def test_population_or_escaped_user_process_blocks_acknowledgement(self):
        for attribute in ("empty", "sandbox_processes"):
            with self.subTest(attribute=attribute):
                self.commands.empty.return_value = attribute != "empty"
                self.commands.sandbox_processes.return_value = attribute == "sandbox_processes"
                with self.assertRaises(GuestDiskError):
                    self.gate.quiesce(3)
                self.assertEqual(self.gate._state()["mode"], "closing")
        self.commands.empty.return_value = True
        self.commands.sandbox_processes.return_value = False
        self.gate.quiesce(3)
        self.gate.activate(4)

    def test_new_activation_requires_verified_mount_and_empty_guest(self):
        self.workspace.verify.side_effect = GuestDiskError("wrong disk")
        with self.assertRaises(GuestDiskError):
            self.gate.activate(2)
        self.assertFalse(self.gate.state_path.exists())
        self.workspace.verify.side_effect = None
        self.commands.sandbox_processes.return_value = True
        with self.assertRaises(GuestDiskError):
            self.gate.activate(2)
        self.assertFalse(self.gate.state_path.exists())

    def test_stale_or_invalid_controls_do_not_replace_state(self):
        self.gate.activate(2)
        before = self.gate.state_path.read_bytes()
        for method, epoch in (("activate", 1), ("activate", 3), ("quiesce", 1),
                              ("quiesce", 2), ("activate", True), ("quiesce", 2**53)):
            with self.subTest(method=method, epoch=epoch), self.assertRaises(GuestDiskError):
                getattr(self.gate, method)(epoch)
            self.assertEqual(self.gate.state_path.read_bytes(), before)

    def test_invalid_commands_never_reach_systemd(self):
        self.gate.activate(2)
        for change in ({"argv": []}, {"argv": ["sh"]}, {"argv": ["/bin/sh", "a\0b"]},
                       {"argv": ["/bin/sh", 7]}, {"timeout": True}, {"timeout": 361},
                       {"tasks": 2}, {"tasks": 4097}, {"epoch": True},
                       {"argv": ["/bin/sh", "x" * (1024**2 + 1)]}):
            with self.subTest(change=list(change)), self.assertRaises(GuestDiskError):
                self.run_command(**change)
        self.commands.run.assert_not_called()

    def test_corruption_and_wrong_disk_are_not_treated_as_missing(self):
        self.gate.activate(2)
        original = self.gate._state()
        for changes in ({"project_id": True}, {"filesystem_uuid": "other"}, {"epoch": True},
                        {"mode": []}, {"extra": 1}):
            self.gate.state_path.write_text(json.dumps({**original, **changes}))
            with self.subTest(changes=changes), self.assertRaises(GuestDiskError):
                self.run_command()
        self.gate.state_path.write_text("broken")
        with self.assertRaises(GuestDiskError):
            self.gate.activate(3)

    def test_journal_symlinks_hardlinks_and_public_permissions_are_rejected(self):
        self.gate.activate(2)
        other = self.root / "other.json"
        os.link(self.gate.state_path, other)
        with self.assertRaises(GuestDiskError):
            self.gate.activate(2)
        other.unlink()
        self.gate.state_path.chmod(0o644)
        with self.assertRaises(GuestDiskError):
            self.run_command()
        self.gate.state_path.rename(other)
        self.gate.state_path.symlink_to(other)
        with self.assertRaises(OSError):
            self.run_command()

    def test_lost_save_acknowledgement_replays_same_control(self):
        save = self.gate._save
        def lost(state):
            save(state)
            raise OSError("host lost reply")
        with patch.object(self.gate, "_save", side_effect=lost), self.assertRaises(OSError):
            self.gate.activate(2)
        self.assertEqual(self.reopen().activate(2)["mode"], "active")
        with patch.object(self.gate, "_save", side_effect=lost), self.assertRaises(OSError):
            self.gate.quiesce(3)
        self.assertEqual(self.reopen().quiesce(3)["mode"], "quiesced")

    def test_guest_command_lock_survives_client_process_boundary(self):
        self.gate.activate(2)
        child = subprocess.Popen([sys.executable, "-c", "import fcntl,sys; "
            "f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_SH); "
            "print('locked', flush=True); sys.stdin.readline()",
            str(self.root / "gaoji-project-io.lock")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            with self.assertRaises(GuestDiskError):
                self.gate.quiesce(3, timeout=0)
        finally:
            child.communicate("release\n", timeout=5)
        self.assertEqual(self.gate.quiesce(3)["mode"], "quiesced")

    def test_disk_maintenance_requires_quiescence_and_empty_guest(self):
        for action in ("trim", "unmount"):
            with self.assertRaises(GuestDiskError):
                self.gate.disk_operation(action, 2)
        self.gate.activate(2)
        with self.assertRaises(GuestDiskError):
            self.gate.disk_operation("unmount", 3)
        self.gate.quiesce(3)
        for attribute in ("empty", "sandbox_processes"):
            self.commands.empty.return_value = attribute != "empty"
            self.commands.sandbox_processes.return_value = attribute == "sandbox_processes"
            with self.assertRaisesRegex(GuestDiskError, "writers"):
                self.gate.disk_operation("unmount", 4)
        self.workspace.unmount.assert_not_called()
        self.workspace.trim.assert_not_called()

    def test_unmount_replay_does_not_require_a_mounted_workspace(self):
        self.gate.quiesce(3)
        self.workspace.verify.side_effect = GuestDiskError("already unmounted")
        self.workspace.unmount.return_value = {"mounted": False, "verified": True}
        expected = {"mounted": False, "verified": True, "epoch": 4, "mode": "quiesced"}
        self.assertEqual(self.gate.disk_operation("unmount", 4), expected)
        self.assertEqual(self.reopen().disk_operation("unmount", 4), expected)
        self.workspace.verify.assert_not_called()
        self.workspace.verify.side_effect = None
        self.gate.activate(5)
        self.gate.quiesce(6)
        with self.assertRaises(GuestDiskError):
            self.gate.disk_operation("unmount", 4)
        self.assertEqual(self.workspace.unmount.call_count, 2)

    def test_disk_maintenance_validates_action_epoch_and_propagates_failure(self):
        self.gate.quiesce(3)
        for action, epoch in (("format", 4), ("unmount", True), ("trim", 2), ("trim", 2**53)):
            with self.subTest(action=action, epoch=epoch), self.assertRaises(GuestDiskError):
                self.gate.disk_operation(action, epoch)
        self.workspace.trim.side_effect = GuestDiskError("trim failed")
        with self.assertRaisesRegex(GuestDiskError, "trim failed"):
            self.gate.disk_operation("trim", 4)
        self.assertEqual(self.gate._state()["mode"], "quiesced")
        self.workspace.unmount.assert_not_called()


class CommandContractTests(unittest.TestCase):
    def test_command_keeps_literal_shell_dollars_and_bounds_whole_cgroup(self):
        command = LinuxGuestCommands.command(["/bin/sh", "-lc", 'echo "$HOME" ${X} $$'], 30, 128, "unit.service")
        self.assertEqual(command[-1], 'echo "$$HOME" $${X} $$$$')
        for option in ("--wait", "--pipe", "--collect", "--property=KillMode=control-group",
                       "--property=RuntimeMaxSec=30s", "--property=TasksMax=128",
                       "--property=NoNewPrivileges=yes", "--property=ProtectControlGroups=yes"):
            self.assertIn(option, command)

    def test_failed_command_is_stopped_and_population_checked(self):
        backend = LinuxGuestCommands()
        backend.require_cgroups = Mock()
        backend.stop = Mock()
        backend.empty = Mock(return_value=True)
        with patch("subprocess.run", return_value=Mock(returncode=9)):
            self.assertEqual(backend.run(["/bin/false"], 30, 128), 9)
        backend.stop.assert_called_once()
        backend.empty.assert_called_once_with(backend.stop.call_args.args[0])
        backend.empty.return_value = False
        with patch("subprocess.run", return_value=Mock(returncode=0)), self.assertRaises(GuestDiskError):
            backend.run(["/bin/true"], 30, 128)

    def test_missing_population_is_not_empty_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = LinuxGuestCommands()
            backend.cgroup_root = Path(directory)
            with self.assertRaises(GuestDiskError):
                backend.empty()
            (backend.cgroup_root / "cgroup.controllers").touch()
            self.assertTrue(backend.empty())
            group = backend.cgroup_root / "gaojiproject.slice"
            group.mkdir()
            self.assertFalse(backend.empty())
            (group / "cgroup.events").write_text("populated 1\n")
            self.assertFalse(backend.empty())
            (group / "cgroup.events").write_text("populated 0\n")
            self.assertTrue(backend.empty())


class BridgeGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        holder = mount_tests.MountBridgeTests()
        await holder.asyncSetUp()
        self.addCleanup(holder.fixture.doCleanups)
        for name in ("fixture", "store", "host", "manager", "bridge", "metadata", "ready"):
            setattr(self, name, getattr(holder, name))

    async def test_distinct_managers_cannot_reserve_during_file_transfer(self):
        await self.ready()
        other = copy.copy(self.manager)
        other._create_lock, other._exec_locks = asyncio.Lock(), {}
        bridge = type(self.bridge)(other, self.store)
        async with self.manager.io_lock(SID):
            with self.assertRaisesRegex(SandboxError, "file transfer"):
                await bridge.reserve("reserve", "scope", 1, generation=1, fence=7, reservation="c" * 32)
        self.assertEqual(self.fixture.record()["fence"], 6)

    async def test_quiesce_requires_current_reservation_and_valid_receipt(self):
        await self.ready()
        await self.bridge.reserve("reserve", "scope", 1, generation=1, fence=7, reservation="c" * 32)
        for fence, reservation in ((6, "c" * 32), (7, RESERVATION)):
            with self.assertRaises(SandboxError):
                await self.bridge.quiesce("scope", SID, self.metadata, fence=fence, reservation=reservation)
        result = await self.bridge.quiesce("scope", SID, self.metadata, fence=7, reservation="c" * 32)
        self.assertEqual(result["mode"], "quiesced")
        self.manager._guest_exec.side_effect = None
        self.manager._guest_exec.return_value = (0, b"{}", b"")
        with self.assertRaises(SandboxError):
            await self.bridge.reserve("release", "scope", 1, generation=1, fence=8, reservation="c" * 32)
        self.assertTrue(self.fixture.record()["maintenance"])

    async def test_project_exec_wraps_root_installers_and_user_commands_not_verification(self):
        await self.ready()
        self.manager._guest_exec = type(self.manager)._guest_exec.__get__(self.manager)
        self.manager._guest_exec_raw = AsyncMock(return_value=(0, b"", b""))
        self.manager.project_mounts.authorize = AsyncMock(return_value={"io_epoch": 6})
        async with self.manager.io_lock(SID):
            await self.manager._owned_running("scope", SID)
            await self.manager._guest_exec(SID, "/usr/bin/python3", ["-c", "print('$HOME')"], timeout=30)
        call = self.manager._guest_exec_raw.await_args
        request = json.loads(call.kwargs["input_bytes"])
        self.assertEqual(request, {"action": "run", "epoch": 6,
            "argv": ["/usr/bin/python3", "-c", "print('$HOME')"], "timeout": 30, "tasks": 256})
        self.assertIsNone(self.manager._io_scope.get())
        await self.manager._guest_exec(SID, "/bin/true", [])
        self.assertEqual(self.manager._guest_exec_raw.await_args.args, (SID, "/bin/true", []))

    async def test_lost_release_reply_retries_without_changing_epoch(self):
        await self.ready()
        await self.bridge.reserve("reserve", "scope", 1, generation=1, fence=7, reservation="c" * 32)
        await self.bridge.quiesce("scope", SID, self.metadata, fence=7, reservation="c" * 32)
        save = self.store._save
        def lost(directory, record):
            save(directory, record)
            if record["intent"]["operation"] == "workspace_release":
                raise OSError("release receipt lost")
        with patch.object(self.store, "_save", side_effect=lost), self.assertRaises(OSError):
            await self.bridge.reserve("release", "scope", 1, generation=1, fence=8, reservation="c" * 32)
        self.assertTrue(self.fixture.record()["pending"])
        self.assertIsNone(self.fixture.record()["maintenance"])
        self.manager._guest_exec.reset_mock()
        result = await self.bridge.reserve("release", "scope", 1, generation=1, fence=8, reservation="c" * 32)
        self.assertFalse(result["pending"])
        self.assertEqual(result["fence"], 8)
        self.manager._guest_exec.assert_not_awaited()

    async def test_guest_activation_failure_retains_reservation_and_blocks_io(self):
        await self.ready()
        await self.bridge.reserve("reserve", "scope", 1, generation=1, fence=7, reservation="c" * 32)
        self.manager._guest_exec.side_effect = SandboxError("guest reply lost")
        with self.assertRaises(SandboxError):
            await self.bridge.reserve("release", "scope", 1, generation=1, fence=8, reservation="c" * 32)
        self.assertEqual(self.fixture.record()["fence"], 7)
        with self.assertRaises(SandboxError):
            await self.manager.exec("scope", SID, "must-not-run")

    async def test_context_is_reset_when_user_operation_is_cancelled(self):
        entered = asyncio.Event()
        async def operation():
            async with self.manager.io_lock(SID):
                self.manager._io_scope.set({"id": SID, "epoch": 6, "tasks": 256})
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(operation())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        async with self.manager.io_lock(SID, timeout=0):
            self.assertEqual(self.manager._io_scope.get(), {"id": SID})

    async def test_external_process_holding_io_lock_blocks_maintenance(self):
        await self.ready()
        lock = self.manager.root / f".io-{SID}.lock"
        child = subprocess.Popen([sys.executable, "-c", "import fcntl,sys; "
            "f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX); "
            "print('locked', flush=True); sys.stdin.readline()", str(lock)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(await asyncio.to_thread(child.stdout.readline), "locked\n")
            with self.assertRaisesRegex(SandboxError, "file transfer"):
                await self.bridge.reserve("reserve", "scope", 1, generation=1, fence=7, reservation="c" * 32)
        finally:
            await asyncio.to_thread(child.communicate, "release\n", timeout=5)
        self.assertEqual(self.fixture.record()["fence"], 6)


class HostLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_waits_for_acquisition_then_releases_descriptor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "io.lock"
            entered = []
            async def waiting():
                async with host_lock(path):
                    entered.append(True)
            async with host_lock(path):
                task = asyncio.create_task(waiting())
                await asyncio.sleep(0.05)
                task.cancel()
                await asyncio.sleep(0.05)
                self.assertFalse(task.done())
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(entered, [])
            async with host_lock(path, timeout=0):
                pass

    async def test_special_lock_files_cannot_block_worker_threads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "io.lock"
            os.mkfifo(path, 0o600)
            with self.assertRaises(ValueError):
                async with host_lock(path):
                    self.fail("FIFO admitted")
