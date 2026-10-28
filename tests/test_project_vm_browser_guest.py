"""Real guest journal/lock tests with explicit systemd and virtio fixtures."""

import base64
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from src.sandbox_runtime.project_browser_bundle import build_bundle
from src.sandbox_runtime.project_vm_browser_guest import (
    CHANNEL_NAME, HELPER_PATH, WORKER_PATH, LinuxGuestBrowser, ProjectGuestBrowser, _unit,
)
from src.sandbox_runtime.project_vm_gate import GuestDiskError, LinuxGuestCommands, ProjectGuestGate, guest_lock
from tests.test_project_vm_guest import binding

SESSION = "a" * 32
OTHER = "b" * 32


class GuestBrowserTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gaoji-browser-guest-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.guest = Mock(spec=LinuxGuestCommands)
        self.guest.empty.return_value = True
        self.guest.sandbox_processes.return_value = False
        self.gate = ProjectGuestGate(binding().guest_config(), state_path=self.root / "gate.json",
                                    lock_root=self.root, commands=self.guest, workspace=Mock())
        self.gate.activate(2)
        self.commands = Mock(spec=LinuxGuestBrowser)
        self.present, self.active = False, False
        self.commands.inspect.side_effect = lambda _: {"present": self.present, "active": self.active}
        self.commands.start.side_effect = self.started
        self.guest.stop.side_effect = self.stopped
        self.browser = self.reopen()

    def started(self, *args):
        self.present, self.active = True, True

    def stopped(self, *args):
        self.present, self.active = False, False

    def reopen(self):
        return ProjectGuestBrowser(self.gate, root=self.root / "browser", commands=self.commands)

    def start(self, **changes):
        return self.browser.start(**{"epoch": 2, "session": SESSION, "tasks": 128, **changes})

    def test_start_is_persisted_before_effect_and_not_repeated_after_reopen(self):
        def start_effect(session, tasks):
            self.assertEqual(self.reopen()._state()["state"], "starting")
            self.started()
            raise OSError("QGA lost the reply")
        self.commands.start.side_effect = start_effect
        with self.assertRaises(OSError):
            self.start()
        self.browser = self.reopen()
        result = self.start()
        self.assertEqual(result["state"], "starting")
        self.assertTrue(result["unit_active"])
        self.commands.start.assert_called_once_with(SESSION, 128)
        self.assertEqual((self.browser.root / "state.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.browser.root.stat().st_mode & 0o777, 0o700)

    def test_crash_after_intent_before_systemd_never_relaunches(self):
        save = self.browser._save
        def interrupted(state):
            save(state)
            raise OSError("interrupted after fsync")
        with patch.object(self.browser, "_save", side_effect=interrupted), self.assertRaises(OSError):
            self.start()
        self.browser = self.reopen()
        result = self.start()
        self.assertFalse(result["unit_present"])
        self.commands.start.assert_not_called()
        self.assertEqual(self.browser.stop(2, SESSION)["state"], "closed")
        self.assertEqual(self.start()["state"], "closed")
        self.commands.start.assert_not_called()
        self.start(session=OTHER)
        self.commands.start.assert_called_once_with(OTHER, 128)

    def test_live_session_cannot_be_rebound_or_replaced(self):
        self.start()
        for changes in ({"epoch": 3}, {"tasks": 256}, {"session": OTHER}):
            with self.subTest(changes=changes), self.assertRaises(GuestDiskError):
                self.start(**changes)
        for method in (self.browser.status, self.browser.stop):
            with self.assertRaises(GuestDiskError):
                method(3, SESSION)
            with self.assertRaises(GuestDiskError):
                method(2, OTHER)
        self.guest.stop.assert_not_called()
        self.commands.start.assert_called_once()

    def test_mount_epoch_other_writers_or_foreign_unit_block_launch(self):
        for kind in ("epoch", "closed", "mount", "cgroup", "user", "unit", "toolchain"):
            with self.subTest(kind=kind):
                self.gate.workspace.verify.side_effect = GuestDiskError("wrong mount") if kind == "mount" else None
                self.guest.empty.return_value = kind != "cgroup"
                self.guest.sandbox_processes.return_value = kind == "user"
                self.present = kind == "unit"
                self.commands.prepare.side_effect = GuestDiskError("not installed") if kind == "toolchain" else None
                if kind == "closed":
                    self.gate._save({**self.gate.identity, "epoch": 2, "mode": "closing"})
                else:
                    self.gate._save({**self.gate.identity, "epoch": 2, "mode": "active"})
                with self.assertRaises(GuestDiskError):
                    self.start(epoch=3 if kind == "epoch" else 2)
                self.assertFalse((self.browser.root / "state.json").exists())
        self.commands.start.assert_not_called()

    def test_invalid_identity_and_limits_do_not_reach_systemd(self):
        for changes in ({"session": "../x"}, {"session": "A" * 32}, {"session": True},
                        {"epoch": True}, {"epoch": 0}, {"tasks": True}, {"tasks": 31}, {"tasks": 4097}):
            with self.subTest(changes=changes), self.assertRaises(GuestDiskError):
                self.start(**changes)
        self.commands.start.assert_not_called()

    def test_worker_holds_guest_io_lock_and_cannot_restart_or_run_after_stop(self):
        self.start()
        def work():
            self.assertEqual(self.reopen()._state()["state"], "running")
            with self.assertRaises(TimeoutError), guest_lock(self.root / "gaoji-project-io.lock", timeout=0):
                pass
            # Control observations must remain available while the browser owns IO.
            self.assertEqual(self.browser.status(2, SESSION)["state"], "running")
            return 7
        self.commands.worker.side_effect = work
        self.assertEqual(self.browser.worker(SESSION), 7)
        with self.assertRaises(GuestDiskError):
            self.browser.worker(SESSION)
        self.browser.stop(2, SESSION)
        with self.assertRaises(GuestDiskError):
            self.browser.worker(SESSION)
        self.commands.worker.assert_called_once()
        with guest_lock(self.root / "gaoji-project-io.lock", timeout=0):
            pass

    def test_revoked_epoch_prevents_delayed_worker_start(self):
        self.start()
        self.gate.quiesce(3)
        with self.assertRaises(GuestDiskError):
            self.browser.worker(SESSION)
        self.commands.worker.assert_not_called()
        self.assertEqual(self.browser.stop(2, SESSION)["state"], "closed")

    def test_stop_requires_independent_unit_and_cgroup_observation(self):
        self.start()
        self.guest.stop.side_effect = None
        with self.assertRaises(GuestDiskError):
            self.browser.stop(2, SESSION)
        self.assertEqual(self.browser._state()["state"], "closing")
        self.active = False
        self.guest.empty.return_value = False
        with self.assertRaises(GuestDiskError):
            self.browser.stop(2, SESSION)
        self.guest.empty.return_value = True
        result = self.browser.stop(2, SESSION)
        self.assertTrue(result["cgroup_empty"])
        self.assertEqual(result["state"], "closed")
        self.assertEqual(self.guest.stop.call_args.args, (_unit(SESSION),))

    def test_lost_stop_ack_is_reconciled_and_does_not_start_anything(self):
        self.start()
        def stop_effect(*args):
            self.stopped()
            raise OSError("lost stop acknowledgement")
        self.guest.stop.side_effect = stop_effect
        with self.assertRaises(OSError):
            self.browser.stop(2, SESSION)
        self.guest.stop.side_effect = self.stopped
        self.assertEqual(self.reopen().stop(2, SESSION)["state"], "closed")
        self.commands.start.assert_called_once()

    def test_foreign_unit_or_corrupt_journal_is_not_adopted_or_stopped(self):
        self.start()
        self.commands.inspect.side_effect = GuestDiskError("unit changed")
        with self.assertRaises(GuestDiskError):
            self.browser.stop(2, SESSION)
        self.guest.stop.assert_not_called()
        original = self.browser._state()
        for changes in ({"epoch": True}, {"project_id": True}, {"filesystem_uuid": "other"},
                        {"state": []}, {"tasks": False}, {"session_id": "other"}, {"extra": 1}):
            self.browser._save({**original, **changes})
            with self.subTest(changes=changes), self.assertRaises(GuestDiskError):
                self.browser.status(2, SESSION)

    def test_journal_path_permissions_and_link_attacks_are_rejected(self):
        self.start()
        path = self.browser.root / "state.json"
        other = self.browser.root / "other.json"
        os.link(path, other)
        with self.assertRaises(GuestDiskError):
            self.browser.status(2, SESSION)
        other.unlink()
        path.chmod(0o644)
        with self.assertRaises(GuestDiskError):
            self.browser.status(2, SESSION)
        path.chmod(0o600)
        path.rename(other)
        path.symlink_to(other)
        with self.assertRaises(OSError):
            self.browser.status(2, SESSION)
        self.browser.root.chmod(0o755)
        with self.assertRaises(GuestDiskError):
            self.browser.status(2, SESSION)


class GuestBrowserContractTests(unittest.TestCase):
    def test_browser_exec_drops_root_and_has_no_application_environment(self):
        from types import SimpleNamespace
        from src.sandbox_runtime import project_vm_browser_guest as guest

        with tempfile.TemporaryDirectory(prefix="gaoji-virtio-fixture-") as temporary:
            path = Path(temporary) / "channel"
            path.touch(mode=0o600)
            fd = os.open(path, os.O_RDWR)
            real = os.fstat(fd)
            info = SimpleNamespace(st_mode=stat.S_IFCHR | 0o600, st_uid=0,
                                   st_dev=real.st_dev, st_ino=real.st_ino, st_rdev=0)
            with patch.object(Path, "resolve", return_value=Path("/dev/vport1p2")), \
                 patch.object(Path, "stat", return_value=info), \
                 patch.object(Path, "read_text", return_value=CHANNEL_NAME), \
                 patch.object(guest.os, "open", return_value=fd), \
                 patch.object(guest.os, "fstat", return_value=info), \
                 patch.object(guest.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
                self.assertEqual(LinuxGuestBrowser().worker(), 0)
            options = run.call_args.kwargs
            self.assertEqual((options["user"], options["group"], options["extra_groups"]), (1000, 1000, ()))
            self.assertEqual(options["cwd"], "/workspace")
            self.assertEqual(set(options["env"]), {"HOME", "PATH", "LANG"})
            self.assertIs(options["stdin"], options["stdout"])
            self.assertTrue(options["stdin"].closed)

    def test_toolchain_rejects_writable_dependencies_and_outside_directory_links(self):
        from src.sandbox_runtime import project_vm_browser_guest as guest

        with tempfile.TemporaryDirectory(prefix="gaoji-toolchain-fixture-") as temporary:
            root = Path(temporary)
            (root / "lib").mkdir()
            (root / "lib64").symlink_to(root / "lib")
            package = root / "lib/package.py"
            package.write_text("pass")
            with patch.object(guest, "PYTHON_PATH", str(root / "bin/python")), \
                 patch.object(guest, "_trusted_file"):
                LinuxGuestBrowser().prepare()
                package.chmod(0o666)
                with self.assertRaisesRegex(GuestDiskError, "user-writable"):
                    LinuxGuestBrowser().prepare()
                package.chmod(0o644)
                (root / "lib64").unlink()
                (root / "lib64").symlink_to(root.parent)
                with self.assertRaisesRegex(GuestDiskError, "directory link"):
                    LinuxGuestBrowser().prepare()

    def test_unit_is_bounded_and_uses_only_the_fixed_guest_entrypoint(self):
        command = LinuxGuestBrowser().command(SESSION, 128)
        for option in ("--property=NoNewPrivileges=yes", "--property=Restart=no",
                       "--property=KillMode=control-group", "--property=RuntimeMaxSec=3600s",
                       "--property=MemoryMax=1073741824", "--property=CPUQuota=100%",
                       "--property=TasksMax=128", "--property=ProtectControlGroups=yes",
                       "--property=StandardError=null"):
            self.assertIn(option, command)
        self.assertEqual(command[-5:], ["/usr/bin/python3", "-I", HELPER_PATH, "--worker", SESSION])
        self.assertNotIn("--no-block", command)
        self.assertNotIn("--wait", command)

    def test_unit_status_requires_owned_transient_policy(self):
        observed = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running",
                    "Transient": "yes", "Description": "Gaoji project browser " + SESSION,
                    "Slice": "gaojiproject.slice", "Restart": "no", "KillMode": "control-group"}
        def run(fields, code=0):
            result = subprocess.CompletedProcess([], code, "\n".join(f"{k}={v}" for k, v in fields.items()).encode())
            with patch("subprocess.run", return_value=result):
                return LinuxGuestBrowser().inspect(SESSION)
        self.assertEqual(run(observed), {"present": True, "active": True})
        for changes in ({"Transient": "no"}, {"Description": "other"}, {"Slice": "system.slice"},
                        {"Restart": "always"}, {"KillMode": "process"}, {"ActiveState": "unknown"}):
            with self.subTest(changes=changes), self.assertRaises(GuestDiskError):
                run({**observed, **changes})
        self.assertEqual(run({"LoadState": "not-found", "ActiveState": "inactive"}, 1),
                         {"present": False, "active": False})
        with self.assertRaises(GuestDiskError):
            run({}, 1)

    def test_cloud_init_contains_separate_root_control_and_unprivileged_worker_archives(self):
        from src.plugins.ai_chat.vm_sandbox import VmSandboxManager
        config = json.loads(VmSandboxManager._cloud_config(project_disk=binding()).split("\n", 1)[1])
        files = {item["path"]: item for item in config["write_files"]}
        self.assertEqual(files[HELPER_PATH]["permissions"], "0700")
        self.assertEqual(files[WORKER_PATH]["permissions"], "0755")
        for control, path in ((True, HELPER_PATH), (False, WORKER_PATH)):
            data = base64.b64decode(files[path]["content"])
            self.assertEqual(data, build_bundle(guest_control=control))
            with zipfile.ZipFile(io.BytesIO(data)) as bundle:
                self.assertNotIn("src/plugins/ai_chat/config.py", bundle.namelist())
                if not control:
                    self.assertNotIn("src/sandbox_runtime/project_vm_browser_guest.py", bundle.namelist())
        self.assertIn(CHANNEL_NAME, files["/etc/udev/rules.d/70-gaoji-browser.rules"]["content"])
        # No browser download on every VM boot; toolchain preparation remains lazy.
        self.assertNotIn("chromium", config["packages"])

    def test_control_archive_imports_in_isolated_python_without_application(self):
        with tempfile.TemporaryDirectory(prefix="gaoji-browser-bundle-") as temporary:
            path = Path(temporary) / "control.pyz"
            path.write_bytes(build_bundle(guest_control=True))
            script = "import sys; sys.path.insert(0, sys.argv[1]); from src.sandbox_runtime.project_vm_browser_guest import ProjectGuestBrowser; print(ProjectGuestBrowser.__name__)"
            result = subprocess.run([sys.executable, "-I", "-c", script, str(path)],
                                    capture_output=True, check=True, timeout=10, cwd=temporary)
            self.assertEqual(result.stdout.strip(), b"ProjectGuestBrowser")
