from __future__ import annotations

import json
import http.client
import threading
import time
from unittest.mock import patch

from test_desktop_backups import Fixture, FakeController, Crash, CHECKSUM, IMAGE
from src.local_runtime.files import read_json, write_json
from src.local_runtime.storage import Storage, StorageError, tree_info
from src.local_runtime.setup_clients import SetupError
from src.local_runtime.validation import ConfigurationConflict
from src.local_runtime.manager import ManagerServer


class StorageTests(Fixture):
    def setUp(self):
        super().setUp()
        (self.root / "jobs").mkdir()
        job = "9" * 32
        write_json(self.root / "jobs" / (job + ".json"), {"instance_id": self.identifier, "checksum": CHECKSUM,
            "runtime_image": IMAGE, "docker": "/test/docker", "docker_host": "unix:///test/engine"})
        write_json(self.directory / "config/control.json", {"port": 12345, "token": "test-token"})
        self.c = FakeController(job, self.root)
        self.storage = self.c.storage
        self.storage.save_policy({"revision": 1, "policy": {"enabled": False, "keep_backups": 2, "keep_restore_points": 1, "min_age_days": 7}})

    def aged(self, number, days):
        identifier = f"{number:032x}"
        record = self.publish(identifier)
        record["created_at"] = int(time.time()) - days * 86400
        write_json(self.backups.path(identifier) / "metadata.json", record)
        return identifier

    def run_cleanup(self, plan=None):
        plan = plan or self.storage.plan()
        with patch.object(self.storage, "resume"):
            self.storage.submit({"id": plan["id"], "confirm": True})
        with patch.object(self.storage, "mounts", return_value=[]), patch.object(self.storage, "kernel_mounts", return_value=[]):
            self.storage.execute()
        return read_json(self.storage.job_path)

    def test_preview_retains_recent_backups_and_never_selects_live_data(self):
        ids = [self.aged(number, number * 5) for number in range(1, 6)]
        scan = self.storage.scan()
        self.assertEqual(len(scan["items"]), 5)
        plan = self.storage.plan()
        self.assertEqual([item["id"] for item in plan["items"]], ["backup:" + value for value in ids[2:]])
        self.assertGreater(plan["estimated_bytes"], 0)
        result = self.run_cleanup(plan)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["items_done"], 3)
        self.assertEqual(len(self.backups.list()), 2)
        self.assertEqual((self.directory / "database/original").read_bytes(), b"original database")
        self.assertEqual((self.directory / "files/report.pdf").read_bytes(), b"fixture-pdf")
        self.assertEqual(self.c.calls, [])
        self.assertEqual(self.storage.submit({"id": plan["id"], "confirm": True})["status"], "completed")

    def test_policy_revision_and_confirmation_are_required(self):
        with self.assertRaises(ConfigurationConflict):
            self.storage.save_policy({"revision": 1, "policy": self.storage.settings()["policy"]})
        with self.assertRaises(StorageError):
            self.storage.save_policy({"revision": 2, "policy": self.storage.settings()["policy"] | {"keep_backups": 0}})
        with self.assertRaises(StorageError):
            self.storage.submit({"id": "f" * 32, "confirm": False})
        self.assertEqual(read_json(self.storage.policy_path)["changes"][0]["revision"], 2)

    def test_failed_capacity_publication_does_not_stop_retention_schedule(self):
        called = threading.Event()
        with patch.object(self.storage, "resume"), patch.object(self.c.capacity, "scan", side_effect=OSError("disk full")), \
                patch.object(self.storage, "tick", side_effect=called.set):
            self.storage.start()
            try:
                self.assertTrue(called.wait(2), "retention was skipped after capacity failure")
            finally:
                self.storage.shutdown.set()
                self.storage.scheduler.join(timeout=3)
            self.assertFalse(self.storage.scheduler.is_alive())
    def test_mutation_after_preview_and_queued_configuration_change_delete_nothing(self):
        ids = [self.aged(number, number * 5) for number in range(1, 5)]
        plan = self.storage.plan()
        (self.backups.path(ids[-1]) / "new-file").write_text("new content")
        result = self.run_cleanup(plan)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.backups.list()), 4)
        plan = self.storage.plan()
        with patch.object(self.storage, "resume"):
            self.storage.submit({"id": plan["id"], "confirm": True})
        self.store.update(self.identifier, self.config["settings"], expected_revision=2)
        self.storage.execute()
        self.assertEqual(read_json(self.storage.job_path)["status"], "failed")
        self.assertEqual(len(self.backups.list()), 4)

    def test_symlinks_corrupt_references_and_imported_age_are_protected(self):
        ids = [self.aged(number, 30 + number) for number in range(1, 6)]
        imported = read_json(self.backups.path(ids[-1]) / "metadata.json")
        imported.update(imported=True, imported_at=int(time.time()))
        write_json(self.backups.path(ids[-1]) / "metadata.json", imported)
        (self.backups.path(ids[-2]) / "outside").symlink_to(self.source_file())
        preview = self.storage.plan()
        selected = [row["id"] for row in preview["items"]]
        self.assertNotIn("backup:" + ids[-1], selected)
        self.assertNotIn("backup:" + ids[-2], selected)
        (self.directory / "updates").mkdir()
        (self.directory / "updates" / ("f" * 32 + ".json")).write_text("invalid")
        self.assertEqual(self.storage.plan()["items"], [])

    def source_file(self):
        filename = self.root / "outside"
        filename.write_text("do not delete")
        return filename

    def test_mounted_backup_is_not_retired(self):
        ids = [self.aged(number, number * 5) for number in range(1, 5)]
        plan = self.storage.plan()
        with patch.object(self.storage, "resume"):
            self.storage.submit({"id": plan["id"], "confirm": True})
        with patch.object(self.storage, "mounts", return_value=[self.backups.path(ids[-1]) / "snapshot.tar"]):
            self.storage.execute()
        self.assertEqual(read_json(self.storage.job_path)["status"], "failed")
        self.assertEqual(len(self.backups.list()), 4)
        self.assertFalse((self.directory / ".storage-trash" / plan["id"]).exists())

    def test_status_polling_does_not_scan_or_contact_docker(self):
        with patch.object(self.storage, "_catalog") as catalog, patch.object(self.storage, "mounts") as mounts, patch("src.local_runtime.storage.tree_info") as tree:
            first, second = self.storage.status(), self.storage.status()
        self.assertIsNone(first["scan"])
        self.assertEqual(first["policy"], second["policy"])
        catalog.assert_not_called()
        mounts.assert_not_called()
        tree.assert_not_called()

    def test_same_device_system_mount_inside_copy_prevents_any_retirement(self):
        ids = [self.aged(number, number * 5) for number in range(1, 5)]
        plan = self.storage.plan()
        with patch.object(self.storage, "resume"):
            self.storage.submit({"id": plan["id"], "confirm": True})
        mounted_file = self.backups.path(ids[-1]) / "snapshot.tar"
        with patch.object(self.storage, "mounts", return_value=[]), patch.object(self.storage, "kernel_mounts", return_value=[mounted_file]):
            self.storage.execute()
        self.assertEqual(read_json(self.storage.job_path)["status"], "failed")
        self.assertEqual(len(self.backups.list()), 4)
        self.assertFalse((self.directory / ".storage-trash" / plan["id"]).exists())

    def test_quarantined_copy_mounted_after_restart_is_not_deleted(self):
        [self.aged(number, number * 5) for number in range(1, 5)]
        plan = self.storage.plan()
        with patch.object(self.storage, "resume"):
            self.storage.submit({"id": plan["id"], "confirm": True})
        with patch.object(self.storage, "mounts", return_value=[]), patch.object(self.storage, "kernel_mounts", return_value=[]), patch("src.local_runtime.storage.shutil.rmtree", side_effect=Crash()):
            with self.assertRaises(Crash):
                self.storage.execute()
        quarantine = next((self.directory / ".storage-trash" / plan["id"]).iterdir())
        restarted = Storage(self.c)
        with patch.object(restarted, "mounts", return_value=[quarantine / "snapshot.tar"]), patch.object(restarted, "kernel_mounts", return_value=[]):
            restarted.execute()
        self.assertTrue(quarantine.is_dir())
        self.assertEqual(read_json(restarted.job_path)["status"], "failed")
        self.assertTrue(restarted.status()["job"]["resumable"])
        with patch.object(restarted, "mounts", return_value=[]), patch.object(restarted, "kernel_mounts", return_value=[]):
            restarted.execute()
        self.assertEqual(read_json(restarted.job_path)["status"], "completed")
        self.assertEqual(len(self.backups.list()), 2)

    def test_cleanup_resumes_after_directory_quarantine_without_duplicate_deletion(self):
        [self.aged(number, number * 5) for number in range(1, 5)]
        plan = self.storage.plan()
        with patch.object(self.storage, "resume"):
            self.storage.submit({"id": plan["id"], "confirm": True})
        with patch.object(self.storage, "mounts", return_value=[]), patch.object(self.storage, "kernel_mounts", return_value=[]), patch("src.local_runtime.storage.shutil.rmtree", side_effect=Crash()):
            with self.assertRaises(Crash):
                self.storage.execute()
        self.assertEqual(read_json(self.storage.job_path)["status"], "running")
        restarted = Storage(self.c)
        with patch.object(restarted, "mounts", return_value=[]), patch.object(restarted, "kernel_mounts", return_value=[]):
            restarted.execute()
        self.assertEqual(read_json(restarted.job_path)["status"], "completed")
        self.assertEqual(len(self.backups.list()), 2)
        self.assertEqual(list((self.directory / ".storage-trash").iterdir()), [])

    def test_update_point_is_retired_together_with_owned_backup_and_recovery_copy(self):
        history = self.directory / "updates"
        history.mkdir()
        for number in range(1, 3):
            identifier = self.aged(number, number * 15)
            folder = self.directory / "recovery" / identifier
            folder.mkdir(parents=True)
            (folder / "old-database-copy").write_text("old data, not current database")
            write_json(history / (identifier + ".json"), {"schema": 1, "id": identifier, "backup_id": identifier,
                "committed": True, "stage": "verified", "activation_after": int(time.time()) - number * 15 * 86400,
                "original_deployment": {"instance_id": self.identifier}})
        plan = self.storage.plan()
        self.assertEqual(len(plan["items"]), 1)
        self.assertEqual(plan["items"][0]["kind"], "restore_point")
        result = self.run_cleanup(plan)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(list(history.iterdir())), 1)
        self.assertEqual(len(self.backups.list()), 1)
        self.assertEqual((self.directory / "database/original").read_bytes(), b"original database")

    def test_scheduler_is_opt_in_and_empty_scan_is_not_repeated_every_minute(self):
        with patch.object(self.storage, "plan") as plan:
            self.storage.tick()
            plan.assert_not_called()
        self.storage.save_policy({"revision": 2, "policy": self.storage.settings()["policy"] | {"enabled": True}})
        with patch.object(self.storage, "plan", return_value={"items": []}) as plan:
            self.storage.tick()
            self.storage.tick()
            self.assertEqual(plan.call_count, 1)

    def test_pending_cleanup_blocks_activation_restore_and_policy_change(self):
        plan = self.storage.plan()
        with patch.object(self.storage, "resume"):
            self.storage.submit({"id": plan["id"], "confirm": True})
        with self.assertRaisesRegex(SetupError, "清理正在进行"):
            self.c.mutable()
        with self.assertRaises(SetupError):
            self.storage.save_policy({"revision": 2, "policy": self.storage.settings()["policy"]})
        with self.assertRaises(StorageError):
            self.storage._source("database/original")
        with self.assertRaises(StorageError):
            self.storage._source("backups/../../outside")

    def test_authenticated_http_preview_and_cleanup_use_same_plan(self):
        [self.aged(number, number * 5) for number in range(1, 5)]
        self.c.control["port"] = 0
        server = ManagerServer(self.c)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown(); server.server_close(); thread.join(2)
        self.addCleanup(stop)
        def request(route, payload, **headers):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
            try:
                connection.request("POST", route, json.dumps(payload), {"Authorization": "Bearer test-token", "Content-Type": "application/json", **headers})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        self.assertEqual(request("/storage/status", {}, Authorization="Bearer invalid")[0], 403)
        self.assertEqual(request("/storage/scan", {}, Origin="https://untrusted.invalid")[0], 403)
        self.assertEqual(request("/storage/status", {"command": "delete"})[0], 400)
        self.assertEqual(request("/storage/scan", {})[0], 200)
        status, body = request("/storage/plan", {})
        self.assertEqual(status, 200)
        plan = body["data"]
        self.assertEqual(len(plan["items"]), 2)
        self.assertEqual(request("/storage/cleanup", {"id": plan["id"], "confirm": True, "path": "database"})[0], 400)
        with patch.object(self.storage, "mounts", return_value=[]), patch.object(self.storage, "kernel_mounts", return_value=[]):
            status, body = request("/storage/cleanup", {"id": plan["id"], "confirm": True})
            self.assertEqual(status, 200, body)
            self.storage.worker.join(5)
            self.assertFalse(self.storage.worker.is_alive())
        status, body = request("/storage/status", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["job"]["id"], plan["id"])
        self.assertEqual(body["data"]["job"]["status"], "completed")
        self.assertEqual(len(self.backups.list()), 2)
