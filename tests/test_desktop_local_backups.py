from unittest.mock import Mock, patch
import unittest

from src.local_runtime.desktop import DesktopController, DesktopMaintenance, sync_config_mounts
from src.local_runtime.files import read_json, write_json
from src.local_runtime.maintenance import write_bytes
from src.local_runtime.qq_handover import require_handover
from src.local_runtime.setup_clients import SetupError
from test_desktop_backups import Fixture, FakeController, FakeMaintenance, Crash, CHECKSUM, IMAGE, DEPENDENCY


class LocalController(DesktopController):
    services = FakeController.services

    def __init__(self, job, root):
        super().__init__(job, root, root / "releases" / CHECKSUM)
        self.running, self.calls = {"bot", "database", "napcat"}, []
        self.capacity.engine_inventory = lambda: (self.directory, [], 0)

    def compose(self, arguments, **kwargs):
        if arguments[0] == "up":
            for source, target in (("config/admin.secret", "runtime-config/admin.secret"),
                                   ("config/postgres.password", "database-config/postgres-password")):
                assert (self.directory / source).read_bytes() == (self.directory / target).read_bytes()
        return FakeController.compose(self, arguments, **kwargs)


class LocalMaintenance(DesktopMaintenance):
    command = FakeMaintenance.command


class DesktopBackupTests(Fixture):
    def setUp(self):
        super().setUp()
        self.job = "9" * 32
        (self.root / "jobs").mkdir()
        write_json(self.root / "jobs" / (self.job + ".json"), {
            "instance_id": self.identifier, "job": self.job, "checksum": CHECKSUM,
            "runtime_image": IMAGE, "docker": "/test/docker", "docker_host": "unix:///test/engine",
            "volume": "gaoji-desktop-" + self.job + "-data", "network": "gaoji-desktop-" + self.job,
        })
        release = self.root / "releases" / CHECKSUM
        release.mkdir(parents=True)
        write_json(release / "images.lock.json", {"postgres": DEPENDENCY, "napcat": DEPENDENCY})
        write_json(self.directory / "config/control.json", {"port": 12345, "token": "t" * 43})
        write_json(self.root / "desktop-install.json", {"status": "awaiting_setup", "stage": "ready"})
        self.c = LocalController(self.job, self.root)
        write_json(self.c.desired_path, {"bot_enabled": True, "revision": 2})
        self.c.render(True)

    def operation(self, kind, identifier, **extra):
        self.c.operation = {"id": identifier, "kind": kind, "revision": self.c.store.read(self.identifier)["revision"],
            "status": "queued", **extra}
        self.c.persist()
        return LocalMaintenance(self.c)

    def test_backup_restore_uses_named_volume_and_isolated_candidate_network(self):
        self.operation("backup", "1" * 32).run()
        self.assertEqual(self.c.operation["status"], "completed", self.c.operation)
        (self.directory / "state/notes.txt").write_text("newer state")
        maintenance = self.operation("restore", "2" * 32, backup_id="1" * 32)
        documents = []
        original = maintenance.candidate_document

        def capture(*args, **kwargs):
            document = original(*args, **kwargs)
            documents.append(document)
            return document

        with patch.object(maintenance, "candidate_document", capture):
            maintenance.run()
        self.assertEqual(self.c.operation["status"], "completed", self.c.operation)
        self.assertEqual((self.directory / "state/notes.txt").read_text(), "群聊记忆")
        self.assertEqual(self.c.napcat.base, "http://napcat:6099")
        self.assertNotIn("bot", self.c.running)
        self.assertEqual(documents[0]["networks"], {"instance": {"internal": True}})
        self.assertNotIn("napcat", documents[0]["services"])
        for service in documents[0]["services"].values():
            self.assertNotIn("ports", service)
            for mount in service["volumes"]:
                self.assertEqual(mount["type"], "volume")
                self.assertIn("/recovery/" + "2" * 32 + "/candidate/", mount["volume"]["subpath"])
                self.assertNotIn("/config/", mount["volume"]["subpath"])

    def test_crash_recovery_refreshes_mounted_credentials_before_restart(self):
        self.operation("backup", "1" * 32).run()
        newer_secret = b"d" * 32
        write_bytes(self.directory / "config/admin.secret", newer_secret)
        private = read_json(self.directory / "config/secrets.json")
        private["database_password"] = "n" * 48
        write_json(self.directory / "config/secrets.json", private)
        write_bytes(self.directory / "config/postgres.password", private["database_password"].encode())
        self.c.render(True)
        maintenance = self.operation("restore", "2" * 32, backup_id="1" * 32)
        replace = maintenance.replace_candidate

        def crash(candidate):
            replace(candidate)
            raise Crash()

        with patch.object(maintenance, "replace_candidate", crash), self.assertRaises(Crash):
            maintenance.run()
        LocalMaintenance(self.c).run(recovery_only=True)
        self.assertEqual(self.c.operation["status"], "completed", self.c.operation)
        self.assertEqual((self.directory / "runtime-config/admin.secret").read_bytes(), newer_secret)
        self.assertEqual((self.directory / "database-config/postgres-password").read_text(), "n" * 48)
        self.assertEqual(self.c.running, {"bot", "database", "napcat"})

    def test_restart_does_not_start_writers_before_maintenance_recovery(self):
        write_json(self.directory / "config/maintenance.json", {"kind": "backup"})
        self.c.resume = Mock()
        self.c.initialize()
        self.assertEqual(self.c.calls, [])
        self.c.resume.assert_called_once()
        self.assertEqual(read_json(self.c.install_path)["status"], "awaiting_setup")

    def test_foreign_qq_stays_stopped_after_controller_restart(self):
        require_handover(self.directory, "a" * 32, "2" * 32)
        self.c.render(False)
        self.c.resume = Mock()
        self.c.initialize()
        self.assertEqual(self.c.calls, [["up", "-d", "database"]])
        document = read_json(self.directory / "compose.json")
        self.assertTrue(document["services"]["napcat"].get("profiles"))
        result = self.c.submit({"id": "3" * 32, "kind": "resume_qq", "revision": 2, "confirm": True})
        self.assertEqual(result["kind"], "resume_qq")

    def test_backup_operations_reach_persistent_controller_queue(self):
        self.c.resume = Mock()
        result = self.c.submit({"id": "1" * 32, "kind": "backup", "revision": 2})
        self.assertEqual(result["kind"], "backup")
        self.assertEqual(read_json(self.c.operation_path)["id"], "1" * 32)
        self.assertIsInstance(self.c.maintenance(), DesktopMaintenance)

    def test_config_mount_symlink_is_rejected(self):
        folder = self.directory / "runtime-config"
        for file in folder.iterdir():
            file.unlink()
        folder.rmdir()
        folder.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(SetupError):
            sync_config_mounts(self.directory)


if __name__ == "__main__":
    unittest.main()
