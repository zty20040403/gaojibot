from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import shutil
import subprocess
import threading
from pathlib import Path
from unittest.mock import patch

from test_desktop_backups import Fixture, FakeController, FakeMaintenance, CHECKSUM, IMAGE, DEPENDENCY
from src.local_runtime.backups import BackupError
from src.local_runtime.backup_transfer import BackupTransfer, import_id, metadata
from src.local_runtime.files import read_json, write_json
from src.local_runtime.manager import ManagerServer
from src.local_runtime.qq_handover import read_handover, require_handover
from src.local_runtime.setup_clients import SetupError


class TransferTests(Fixture):
    def setUp(self):
        super().setUp()
        (self.root / "jobs").mkdir()
        job = "9" * 32
        write_json(self.root / "jobs" / (job + ".json"), {"instance_id": self.identifier, "checksum": CHECKSUM,
            "runtime_image": IMAGE, "docker": "/test/docker", "docker_host": "unix:///test/engine"})
        release = self.root / "releases" / CHECKSUM
        release.mkdir(parents=True)
        write_json(release / "images.lock.json", {"postgres": DEPENDENCY, "napcat": DEPENDENCY})
        write_json(self.directory / "config/control.json", {"port": 12345, "token": "test-token"})
        self.c = FakeController(job, self.root)
        self.transfer = BackupTransfer(self.c)
        source = Fixture()
        source.setUp()
        self.addCleanup(source.doCleanups)
        self.source = source
        record = source.publish()
        self.data = (source.backups.path(record["id"]) / "snapshot.tar").read_bytes()
        self.info = metadata({key: record[key] for key in ("schema", "id", "runtime_checksum", "bytes", "sha256", "database_bytes", "created_at", "revision", "files")} | {"source_instance_id": source.identifier})

    def start_server(self):
        self.c.control["port"] = 0
        server = ManagerServer(self.c)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown(); server.server_close(); thread.join(2)
        self.addCleanup(stop)
        return server

    def test_native_node_client_roundtrip_against_real_manager(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node required for desktop-to-runtime integration")
        record = self.publish()
        server = self.start_server()
        script = Path(__file__).resolve().parents[1] / "desktop/tests/backup-runtime-roundtrip.cjs"
        result = subprocess.run([node, str(script), json.dumps({"access": {"port": server.server_port,
            "token": "test-token", "instance_id": self.identifier}, "backupId": record["id"],
            "filename": str(self.root / "encrypted.gaojibak")})], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["imported_id"], import_id(self.identifier, record["sha256"]))
        self.assertEqual(self.c.calls, [])
        self.assertEqual((self.directory / "database/original").read_bytes(), b"original database")

    def test_foreign_import_is_private_deduplicated_and_does_not_restore(self):
        self.assertFalse(self.transfer.receipt(self.info)["imported"])
        result = self.transfer.import_archive(io.BytesIO(self.data), self.info, 2)
        self.assertTrue(self.transfer.receipt(self.info)["imported"])
        self.assertEqual(result["id"], import_id(self.identifier, self.info["sha256"]))
        self.assertFalse(result["already_imported"])
        self.assertEqual(self.c.calls, [])
        self.assertEqual(self.c.running, {"bot", "database", "napcat"})
        self.assertEqual((self.directory / "database/original").read_bytes(), b"original database")
        self.assertTrue(self.transfer.import_archive(io.BytesIO(), self.info, 2)["already_imported"])
        self.assertEqual(len(self.backups.list()), 1)
        self.assertTrue(self.backups.list()[0]["imported"])
        self.assertEqual(self.backups.list()[0]["source_instance_id"], self.source.identifier)
        with self.transfer.export(result["id"]) as (info, stream):
            self.assertEqual(stream.read(), self.data)
            self.assertEqual(info["source_instance_id"], self.source.identifier)
            self.assertEqual(info["id"], result["id"])
        archive = self.backups.path(result["id"]) / "snapshot.tar"
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)

    def test_foreign_restore_preserves_target_identity_ports_and_stops_qq(self):
        identifier = self.transfer.import_archive(io.BytesIO(self.data), self.info, 2)["id"]
        ports = self.store.read(self.identifier)["ports"]
        (self.directory / "state/notes.txt").write_text("target state")
        self.c.operation = {"id": "8" * 32, "kind": "restore", "revision": 2, "status": "queued", "backup_id": identifier}
        self.c.persist()
        FakeMaintenance(self.c).run()
        self.assertEqual(self.c.operation["status"], "completed", self.c.operation)
        current = self.store.read(self.identifier)
        self.assertEqual(current["id"], self.identifier)
        self.assertEqual(current["ports"], ports)
        self.assertEqual((self.directory / "config/admin.secret").read_bytes(), self.source.original_secret)
        self.assertEqual((self.directory / "state/notes.txt").read_text(), "群聊记忆")
        self.assertEqual(self.c.running, {"database"})
        self.assertFalse(read_json(self.c.desired_path)["bot_enabled"])
        self.assertEqual((self.source.directory / "database/original").read_bytes(), b"original database")
        self.assertEqual(read_handover(self.directory)["source_instance_id"], self.source.identifier)
        restarted = FakeController("9" * 32, self.root)
        restarted.render(False)
        napcat = read_json(self.directory / "compose.json")["services"]["napcat"]
        self.assertEqual(napcat["profiles"], ["qq-handover"])
        self.assertEqual(napcat["restart"], "no")

    def test_foreign_restore_failure_restores_previous_handover_and_data(self):
        identifier = self.transfer.import_archive(io.BytesIO(self.data), self.info, 2)["id"]
        require_handover(self.directory, "6" * 32, "7" * 32)
        previous = read_handover(self.directory)
        self.c.running = {"database"}
        self.c.render(False)
        self.c.operation = {"id": "8" * 32, "kind": "restore", "revision": 2, "status": "queued", "backup_id": identifier}
        self.c.persist()
        maintenance = FakeMaintenance(self.c)
        original = maintenance.replace_candidate
        def replace(candidate):
            original(candidate)
            raise SetupError("fixture failure after replacing data")
        with patch.object(maintenance, "replace_candidate", side_effect=replace):
            maintenance.run()
        self.assertEqual(self.c.operation["status"], "failed", self.c.operation)
        self.assertEqual(read_handover(self.directory), previous)
        self.assertEqual((self.directory / "database/original").read_bytes(), b"original database")
        self.assertEqual(self.c.running, {"database"})
        self.assertFalse((self.directory / "config/maintenance.json").exists())

    def test_same_instance_restore_does_not_bypass_pending_handover(self):
        record = self.publish()
        require_handover(self.directory, "6" * 32, "7" * 32)
        self.c.running = {"database"}
        self.c.render(False)
        self.c.operation = {"id": "8" * 32, "kind": "restore", "revision": 2, "status": "queued", "backup_id": record["id"]}
        self.c.persist()
        FakeMaintenance(self.c).run()
        self.assertEqual(self.c.operation["status"], "completed", self.c.operation)
        self.assertEqual(read_handover(self.directory)["state"], "pending")
        self.assertEqual(self.c.running, {"database"})

    def test_failed_foreign_restore_removes_only_its_new_gate(self):
        identifier = self.transfer.import_archive(io.BytesIO(self.data), self.info, 2)["id"]
        self.c.operation = {"id": "8" * 32, "kind": "restore", "revision": 2, "status": "queued", "backup_id": identifier}
        self.c.persist()
        maintenance = FakeMaintenance(self.c)
        def fail(_candidate):
            self.assertEqual(read_handover(self.directory)["operation_id"], "8" * 32)
            raise SetupError("fixture failure before data replacement")
        with patch.object(maintenance, "replace_candidate", side_effect=fail):
            maintenance.run()
        self.assertEqual(self.c.operation["status"], "failed", self.c.operation)
        self.assertIsNone(read_handover(self.directory))
        self.assertEqual(self.c.running, {"bot", "database", "napcat"})
        self.assertEqual((self.directory / "database/original").read_bytes(), b"original database")

    def test_import_rejects_corrupt_truncated_wrong_version_and_false_source(self):
        cases = [(self.data[:-10], self.info), (b"x" * len(self.data), self.info),
            (self.data, self.info | {"runtime_checksum": "f" * 64}),
            (self.data, self.info | {"source_instance_id": "f" * 32}),
            (self.data, self.info | {"files": 2}), (self.data, self.info | {"created_at": 1})]
        for data, info in cases:
            with self.subTest(info=info), self.assertRaises(BackupError):
                self.transfer.import_archive(io.BytesIO(data), info, 2)
            self.assertEqual(list(self.backups.root.iterdir()), [])
            self.assertEqual(self.c.calls, [])

    def test_space_revision_symlink_and_changed_configuration_leave_no_import(self):
        with patch("src.local_runtime.backup_transfer.shutil.disk_usage", return_value=shutil._ntuple_diskusage(100, 99, 1)), self.assertRaises(BackupError):
            self.transfer.import_archive(io.BytesIO(self.data), self.info, 2)
        with self.assertRaises(BackupError):
            self.transfer.import_archive(io.BytesIO(self.data), self.info, 1)
        temporary = self.backups.root / (".import-" + import_id(self.identifier, self.info["sha256"]))
        temporary.symlink_to(self.source.directory)
        with self.assertRaises(BackupError):
            self.transfer.import_archive(io.BytesIO(self.data), self.info, 2)
        temporary.unlink()
        outer = self
        class Changed(io.BytesIO):
            def read(self, amount):
                result = super().read(amount)
                outer.store.update(outer.identifier, outer.config["settings"], expected_revision=2)
                return result
        with self.assertRaisesRegex(BackupError, "配置已变化"):
            self.transfer.import_archive(Changed(self.data), self.info, 2)
        self.assertEqual(list(self.backups.root.iterdir()), [])

    def test_manager_http_export_import_and_authentication(self):
        server = self.start_server()
        def call(route, body, headers):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("POST", route, body, headers)
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()
        headers = {"Authorization": "Bearer test-token", "Content-Type": "application/octet-stream",
            "X-Gaoji-Backup": base64.b64encode(json.dumps(self.info).encode()).decode(), "X-Gaoji-Revision": "2"}
        status, _, data = call("/backup/import", self.data, headers)
        self.assertEqual(status, 200, data)
        identifier = json.loads(data)["data"]["id"]
        status, response_headers, body = call("/backup/export", json.dumps({"id": identifier}),
            {"Authorization": "Bearer test-token", "Content-Type": "application/json"})
        self.assertEqual(status, 200, body[:500])
        self.assertEqual(body, self.data)
        self.assertEqual(response_headers["Cache-Control"], "no-store")
        self.assertEqual(hashlib.sha256(body).hexdigest(), self.info["sha256"])
        status, _, data = call("/backup/import/status", json.dumps({"metadata": self.info}),
            {"Authorization": "Bearer test-token", "Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["data"], {"id": identifier, "imported": True, "already_imported": True})
        for forbidden in [{"Authorization": "Bearer wrong"}, {"Origin": "https://untrusted.invalid"}]:
            self.assertEqual(call("/backup/import", self.data, headers | forbidden)[0], 403)
        self.assertEqual(call("/backup/import", self.data, headers | {"X-Gaoji-Backup": "not-base64"})[0], 400)
        self.assertEqual(call("/backup/import", self.data, headers | {"X-Gaoji-Revision": "1"})[0], 400)
        self.assertEqual(len(self.backups.list()), 1)
