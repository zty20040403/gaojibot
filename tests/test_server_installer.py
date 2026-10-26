from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch


MODULE = Path(__file__).parents[1] / "packaging/server/installer.py"
spec = importlib.util.spec_from_file_location("server_installer_under_test", MODULE)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class BundleVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def bundle(self, *, path="src/example.py", extra=None, invalid_hash=False):
        content = b"# release input\n"
        manifest = {"schema": 1, "version": "test", "files": [{"path": path, "bytes": len(content),
            "sha256": "0" * 64 if invalid_hash else hashlib.sha256(content).hexdigest()}]}
        archive = self.root / "test.tar.gz"
        with tarfile.open(archive, "w:gz") as stream:
            for name, data in (("manifest.json", json.dumps(manifest).encode()), (path, content)):
                member = tarfile.TarInfo(name); member.size = len(data)
                stream.addfile(member, io.BytesIO(data))
            if extra:
                member = tarfile.TarInfo(extra[0])
                if extra[1] == "symlink":
                    member.type = tarfile.SYMTYPE; member.linkname = "/etc/passwd"
                    stream.addfile(member)
                else:
                    member.size = 3; stream.addfile(member, io.BytesIO(b"bad"))
        return archive, installer.digest(archive)

    def test_verified_bundle_and_repeat_extraction(self):
        archive, checksum = self.bundle()
        target = self.root / "release"
        self.assertEqual(installer.extract_bundle(archive, target, checksum)["version"], "test")
        self.assertEqual(installer.extract_bundle(archive, target, checksum)["version"], "test")
        self.assertEqual((target / "src/example.py").read_bytes(), b"# release input\n")

    def test_wrong_archive_digest_rejected_before_extraction(self):
        archive, _ = self.bundle()
        target = self.root / "release"
        with self.assertRaises(installer.InstallError):
            installer.extract_bundle(archive, target, "0" * 64)
        self.assertFalse(target.exists())

    def test_traversal_symlinks_duplicate_and_unlisted_files_rejected(self):
        for arguments in ({"path": "../escape.py"}, {"path": "/tmp/escape.py"}, {"extra": ("evil", "symlink")},
                {"extra": (".env", "file")}, {"extra": ("src/example.py", "file")}, {"invalid_hash": True}):
            with self.subTest(arguments=arguments):
                archive, checksum = self.bundle(**arguments)
                target = self.root / "release"
                with self.assertRaises(installer.InstallError):
                    installer.extract_bundle(archive, target, checksum)
                self.assertFalse(target.exists())
                self.assertFalse(list(self.root.glob(".extract-*")))

    def test_unmanaged_destination_not_overwritten(self):
        archive, checksum = self.bundle()
        target = self.root / "release"; target.mkdir()
        (target / "keep").write_text("existing")
        with self.assertRaises(installer.InstallError):
            installer.extract_bundle(archive, target, checksum)
        self.assertEqual((target / "keep").read_text(), "existing")

    def test_image_acceptance_is_unprivileged_offline_and_mounts_no_user_data(self):
        instance = installer.Installer.__new__(installer.Installer)
        instance.save = Mock()
        instance.run = Mock()
        instance.docker = "/verified/docker"
        instance.check_runtime("gaoji-app-runtime:" + "a" * 64)
        from src.local_runtime.releases import RUNTIME_CHECK_TIMEOUT
        self.assertEqual(instance.run.call_args.kwargs["timeout"], RUNTIME_CHECK_TIMEOUT)
        arguments = instance.run.call_args.args[0]
        self.assertEqual(arguments[-1], "check-runtime")
        self.assertEqual(arguments[arguments.index("--user") + 1], "10001:10001")
        self.assertEqual(arguments[arguments.index("--network") + 1], "none")
        self.assertIn("--read-only", arguments)
        self.assertNotIn("--volume", arguments)
        self.assertNotIn("--mount", arguments)

    def test_failed_image_check_stops_before_database_creation(self):
        from types import SimpleNamespace
        archive, checksum = self.bundle()
        original_is_dir = Path.is_dir
        def directory_exists(path):
            return str(path) == "/run/systemd/system" or original_is_dir(path)
        with patch.object(installer, "ROOT", self.root / "install"), \
                patch.object(installer.os, "uname", return_value=SimpleNamespace(machine="x86_64")), \
                patch.object(Path, "is_dir", directory_exists), \
                patch.object(installer.shutil, "disk_usage", return_value=SimpleNamespace(free=20 * 1024**3)):
            instance = installer.Installer("a" * 32, archive, checksum)
            instance.docker = "/verified/docker"
            instance.environment["DOCKER_HOST"] = "unix:///isolated/docker.sock"
            instance.setup_engine = Mock()
            instance.run = Mock(return_value="image-id")
            with patch.object(instance, "check_runtime", side_effect=installer.InstallError("image check failed")):
                with self.assertRaisesRegex(installer.InstallError, "image check failed"):
                    instance.execute()
            self.assertEqual(instance.state["status"], "failed")
            self.assertEqual(instance.state["stage"], "runtime")
            self.assertFalse(instance.state.get("database_started", False))
            self.assertFalse(any("compose" in call.args[0] for call in instance.run.call_args_list))

    def test_database_gate_requires_pg17_and_available_vector(self):
        instance = installer.Installer.__new__(installer.Installer)
        prefix = ["/verified/docker", "compose", "--file", "/owned/compose.json"]
        for result in ("true:false", "false:true", "", "t:t"):
            instance.run = Mock(return_value=result)
            with self.assertRaisesRegex(installer.InstallError, "pgvector"):
                instance.check_database(prefix)
        instance.run = Mock(return_value="true:true\n")
        instance.check_database(prefix)
        arguments = instance.run.call_args.args[0]
        self.assertEqual(arguments[:len(prefix)], prefix)
        self.assertIn("pg_available_extensions", arguments[-1])

    def test_purged_installation_cannot_be_replayed_even_when_previously_ready(self):
        instance = installer.Installer.__new__(installer.Installer)
        instance.state = {"purged_at": 1, "status": "awaiting_setup"}
        instance.run = Mock()
        instance.save = Mock()
        with self.assertRaisesRegex(installer.InstallError, "已被明确删除"):
            instance.execute()
        instance.run.assert_not_called()
        instance.save.assert_not_called()

    def test_installer_waiting_for_lock_does_not_overwrite_purge_tombstone(self):
        archive, checksum = self.bundle()
        with patch.object(installer, "ROOT", self.root / "install"):
            instance = installer.Installer("a" * 32, archive, checksum)
            instance.run = Mock()
            original_lock = installer.fcntl.flock
            def finish_purge_while_waiting(stream, mode):
                original_lock(stream, mode)
                value = dict(instance.state, purged_at=1, purge_task="b" * 32)
                installer.atomic(instance.record, value)
            with patch.object(installer.fcntl, "flock", side_effect=finish_purge_while_waiting):
                with self.assertRaisesRegex(installer.InstallError, "已被明确删除"):
                    instance.execute()
            self.assertEqual(json.loads(instance.record.read_text())["purged_at"], 1)
            instance.run.assert_not_called()

    def test_completed_boot_restores_public_status_without_reinstalling(self):
        archive, checksum = self.bundle()
        with patch.object(installer, "ROOT", self.root / "install"):
            instance = installer.Installer("a" * 32, archive, checksum)
            instance.save(status="awaiting_setup", stage="verify", completed=["prepare", "engine", "runtime", "database", "service", "verify"])
            canonical = instance.record.read_bytes()
            installer.atomic(instance.status_path, {"schema": 1, "job": "a" * 32,
                "status": "running", "stage": "prepare", "completed": []}, 0o644)
            instance.run = Mock()
            instance.execute()
            self.assertEqual(json.loads(instance.status_path.read_text())["status"], "awaiting_setup")
            self.assertEqual(json.loads(instance.status_path.read_text())["completed"], instance.state["completed"])
            self.assertEqual(instance.record.read_bytes(), canonical)
            instance.run.assert_not_called()

    def test_completed_boot_rechecks_purge_after_waiting_for_lock(self):
        archive, checksum = self.bundle()
        with patch.object(installer, "ROOT", self.root / "install"):
            instance = installer.Installer("a" * 32, archive, checksum)
            instance.save(status="awaiting_setup")
            original_lock = installer.fcntl.flock
            def finish_purge(stream, mode):
                original_lock(stream, mode)
                installer.atomic(instance.record, dict(instance.state, purged_at=1))
            with patch.object(installer.fcntl, "flock", side_effect=finish_purge):
                with self.assertRaisesRegex(installer.InstallError, "已被明确删除"):
                    instance.execute()
            self.assertEqual(json.loads(instance.record.read_text())["purged_at"], 1)

    def test_distribution_pins_pgvector_instead_of_plain_postgres(self):
        images = json.loads((MODULE.parent / "images.lock.json").read_text())
        self.assertRegex(images["postgres"], r"^docker.io/pgvector/pgvector:[^@]+-pg17-bookworm@sha256:[a-f0-9]{64}$")


if __name__ == "__main__":
    unittest.main()
