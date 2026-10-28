"""Frozen browser dependencies; package-manager effects are never run here."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.sandbox_runtime.project_browser_bundle import guest_requirements
from src.sandbox_runtime import project_vm_browser_install as install
from src.sandbox_runtime.project_vm_guest import GuestDiskError


class BrowserInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.manifest = self.root / "requirements.txt"
        self.manifest.write_text(guest_requirements())
        self.commands = []
        self.verify = Mock()
        for name, value in (("STATE_ROOT", self.state), ("REQUIREMENTS_PATH", str(self.manifest)),
                            ("PYTHON_PATH", str(self.root / "venv/bin/python")), ("_require_guest", Mock()),
                            ("_run", self.run_package), ("_verify", self.verify),
                            ("_trusted_file", lambda path, **kwargs: Path(path)), ("LinuxGuestBrowser", Mock())):
            patcher = patch.object(install, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_package(self, argv, **kwargs):
        self.commands.append(argv)
        if argv[0] == "/usr/bin/apt-cache":
            return "Package: chromium\nVersion: 140.0.7339.80-1~deb13u1\n"
        if argv[0] == "/usr/bin/apt-get" and "install" in argv:
            self.assertEqual(json.loads((self.state / "toolchain.json").read_text())["state"], "prepared")
        return None

    def test_frozen_wheels_and_versioned_chromium_then_verified_reuse(self):
        digest, versions = install._requirements()
        self.assertEqual(len(digest), 64)
        self.assertEqual(set(versions), {"playwright", "pyee", "greenlet", "typing-extensions"})
        with patch.object(install.shutil, "disk_usage", return_value=SimpleNamespace(free=2 * 1024**3)):
            install.install()
        ready = json.loads((self.state / "toolchain.json").read_text())
        self.assertEqual(ready["state"], "ready")
        self.assertEqual(ready["versions"], versions)
        pip = next(c for c in self.commands if "pip" in c)
        for flag in ("--isolated", "--only-binary=:all:", "--require-hashes", "--no-deps", "--no-cache-dir"):
            self.assertIn(flag, pip)
        self.commands.clear()
        install.install()
        self.assertEqual(self.commands, [])
        self.assertEqual(self.verify.call_count, 2)

    def test_interrupted_install_resumes_same_chromium_version(self):
        original = self.run_package
        def interrupted(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[0] == "/usr/bin/apt-get" and "install" in argv:
                raise GuestDiskError("fixture interruption")
            return result
        with patch.object(install, "_run", interrupted), self.assertRaises(GuestDiskError):
            install.install()
        self.commands.clear()
        install.install()
        self.assertFalse(any(c[0] == "/usr/bin/apt-cache" for c in self.commands))
        apt = next(c for c in self.commands if c[0] == "/usr/bin/apt-get")
        self.assertIn("chromium=140.0.7339.80-1~deb13u1", apt)

    def test_invalid_manifest_or_changed_lock_fails_without_package_commands(self):
        for value in ("playwright", "playwright==1.0", "--index-url https://other.invalid", guest_requirements() * 2):
            self.manifest.write_text(value)
            with self.subTest(value=value[:40]), self.assertRaises(GuestDiskError):
                install.install()
        self.assertEqual(self.commands, [])
        self.manifest.write_text(guest_requirements())
        install.install()
        self.commands.clear()
        self.manifest.write_text(guest_requirements().replace("playwright==", "playwright==99."))
        with self.assertRaises(GuestDiskError):
            install.install()
        self.assertEqual(self.commands, [])

    def test_disk_shortage_or_failed_verification_never_publishes_ready(self):
        with patch.object(install.shutil, "disk_usage", return_value=SimpleNamespace(free=0)):
            with self.assertRaises(GuestDiskError):
                install.install()
        self.assertFalse((self.state / "toolchain.json").exists())
        self.verify.side_effect = GuestDiskError("fixture invalid wheel install")
        with self.assertRaises(GuestDiskError):
            install.install()
        self.assertEqual(json.loads((self.state / "toolchain.json").read_text())["state"], "prepared")

    def test_malformed_journal_state_is_rejected_cleanly(self):
        install.install()
        path = self.state / "toolchain.json"
        plan = json.loads(path.read_text())
        plan["state"] = []
        install._save(path, plan)
        self.commands.clear()
        with self.assertRaises(GuestDiskError):
            install.install()
        self.assertEqual(self.commands, [])


class BrowserGuestPlatformTests(unittest.TestCase):
    def test_only_administrator_on_the_frozen_guest_release_can_install(self):
        for uid, distro, version, python in ((501, "debian", "13", (3, 13)), (0, "ubuntu", "24", (3, 13)),
                                             (0, "debian", "12", (3, 13)), (0, "debian", "13", (3, 11))):
            with patch.object(install.os, "geteuid", return_value=uid), \
                 patch.object(install.platform, "freedesktop_os_release", return_value={"ID": distro, "VERSION_ID": version}), \
                 patch.object(install.sys, "version_info", python), self.assertRaises(GuestDiskError):
                install._require_guest()
