from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest

import test_desktop_backups as backups
import test_desktop_updates as updates
from src.local_runtime.files import read_json, write_json
from src.local_runtime.services import controller_units, retire_installer, write_unit
from src.local_runtime.setup_clients import SetupError
from src.local_runtime.update_worker import UpdateWorker, WorkerController, validate_request
from src.local_runtime.update_dispatch import dispatch


class ServicesTests(unittest.TestCase):
    def test_retiring_bootstrap_only_removes_the_exact_owned_boot_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "units"
            job = "c" * 32
            name = f"gaoji-app-install-{job}.service"
            self.assertFalse(retire_installer(job, root))
            write_unit(name, "[Service]\n", root)
            link = root / "multi-user.target.wants" / name
            with self.assertRaises(SetupError):
                retire_installer(job, root)
            self.assertTrue(link.is_symlink())
            body = f"# Gaoji installer {job} {'d' * 64}\n[Service]\n"
            (root / name).write_text(body)
            write_unit("gaoji-app-other.service", "[Service]\n", root)
            self.assertTrue(retire_installer(job, root))
            self.assertFalse(link.is_symlink())
            self.assertEqual((root / name).read_text(), body)
            self.assertTrue((root / "multi-user.target.wants/gaoji-app-other.service").is_symlink())
            self.assertFalse(retire_installer(job, root))
            link.symlink_to("/somebody-elses.service")
            with self.assertRaises(SetupError):
                retire_installer(job, root)
            self.assertEqual(os.readlink(link), "/somebody-elses.service")

    def test_only_owned_units_are_replaced_and_boot_links_checked_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "units"
            name = "gaoji-app-control-" + "a" * 32 + ".service"
            write_unit(name, "[Unit]\nDescription=old\n", root)
            write_unit(name, "[Unit]\nDescription=new\n", root)
            self.assertIn("Description=new", (root / name).read_text())
            self.assertEqual(os.readlink(root / "multi-user.target.wants" / name), "../" + name)
            (root / name).write_text("# someone else's service\n")
            with self.assertRaises(SetupError):
                write_unit(name, "overwrite", root)
            self.assertIn("someone else's", (root / name).read_text())
            other = "gaoji-app-other.service"
            (root / "multi-user.target.wants" / other).symlink_to("/unmanaged.service")
            with self.assertRaises(SetupError):
                write_unit(other, "overwrite", root)
            self.assertFalse((root / other).exists())
            with self.assertRaises(SetupError):
                write_unit("other-person.service", "overwrite", root)


class LocalController(WorkerController):
    compose = backups.FakeController.compose
    services = backups.FakeController.services

    def __init__(self, job, root):
        super().__init__(job, root)
        self.running = {"bot", "napcat", "database"}
        self.calls = []
        self.capacity.engine_inventory = lambda: (self.directory, [], 0)


class LocalWorker(UpdateWorker):
    controller_class = LocalController
    transaction_class = updates.SimulatedUpdate

    def __init__(self, operation, root):
        super().__init__(operation, root)
        self.unit_root = root / "test-unit-registry"
        self.active = "active"
        self.calls = []
        self.fail_start = False
        self.fail_readiness = False
        self.on_stop = None

    def systemctl(self, *arguments):
        self.calls.append(arguments)
        if arguments[0] == "show":
            return self.active
        if arguments[0] == "stop":
            self.active = "inactive"
            if self.on_stop:
                self.on_stop()
        if arguments[0] == "start":
            if self.fail_start:
                raise SetupError("simulated manager failure")
            self.active = "active"
        return ""

    def install_services(self, deployment):
        for name, body in controller_units(self.root, self.controller.directory, self.request["job"], deployment, sys.executable).items():
            write_unit(name, body, self.root / "test-unit-registry")
        self.systemctl("daemon-reload")

    def wait_controller(self):
        if self.fail_readiness:
            raise SetupError("simulated controller HTTP not ready")


class WorkerFixture(updates.UpdateFixture):
    def setUp(self):
        super().setUp()
        self.c.deployment["engine_unit"] = "docker.service"
        write_json(self.root / "jobs" / (self.job + ".json"), self.c.deployment)
        self.request = {"id": "1" * 32, "job": self.job, "kind": "update", "revision": 2,
            "expected_checksum": self.old.checksum, "target_checksum": self.new.checksum}
        (self.root / "updates").mkdir()
        self.request_path = self.root / "updates" / (self.request["id"] + ".json")
        write_json(self.request_path, {"schema": 1, "request": self.request, "status": "queued"})

    def worker(self):
        return LocalWorker(self.request["id"], self.root)


class WorkerTests(WorkerFixture):
    def test_update_retires_old_bootstrap_before_replacing_version_record(self):
        worker = self.worker()
        name = f"gaoji-app-install-{self.job}.service"
        write_unit(name, "[Service]\n", worker.unit_root)
        (worker.unit_root / name).write_text(f"# Gaoji installer {self.job} {self.old.checksum}\n[Service]\n")
        link = worker.unit_root / "multi-user.target.wants" / name
        real_install = worker.install_services
        def install_services(deployment):
            self.assertFalse(link.is_symlink())
            real_install(deployment)
        worker.install_services = install_services
        worker.run()
        self.assertEqual(worker.state["status"], "completed", worker.state)
        self.assertEqual(read_json(self.root / "jobs" / (self.job + ".json"))["checksum"], self.new.checksum)
        self.assertFalse(link.is_symlink())
        self.assertTrue((worker.unit_root / name).is_file())

    def test_active_unit_without_ready_api_is_not_reported_complete(self):
        worker = self.worker()
        worker.fail_readiness = True
        worker.run()
        self.assertEqual(worker.active, "active")
        self.assertEqual(worker.state["status"], "failed")
        self.assertEqual(worker.state["stage"], "controller_unverified")
        self.assertTrue(worker.state["resumable"])
        self.assertFalse(worker.state["finished"])
        repeated = self.worker()
        repeated.run()
        self.assertEqual(repeated.state["status"], "completed", repeated.state)
        self.assertFalse(any("migrate" in call for call in repeated.controller.calls))

    def test_progress_remains_readable_with_private_worker_umask(self):
        worker = self.worker()
        mask = os.umask(0o077)
        try:
            worker.save(status="running", stage="prepare", message="checking")
        finally:
            os.umask(mask)
        self.assertEqual(worker.public_path.parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(worker.public_path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.request_path.stat().st_mode & 0o777, 0o600)

    def test_success_changes_only_scoped_units_and_updates_public_status(self):
        worker = self.worker()
        worker.run()
        self.assertEqual(read_json(self.request_path)["status"], "completed", worker.state)
        self.assertEqual(worker.active, "active")
        scope = "gaoji-app-control-" + self.identifier + ".service"
        for call in worker.calls:
            if call[0] != "daemon-reload":
                self.assertEqual(call[1], scope)
        for unit in (self.root / "test-unit-registry").glob("*.service"):
            self.assertIn(self.new.checksum, unit.read_text())
        value = read_json(worker.public_path)
        self.assertEqual(value["target_checksum"], self.new.checksum)
        self.assertNotIn("private-test-model-key", str(value))
        repeated = self.worker()
        repeated.run()
        self.assertEqual(repeated.calls, [])

    def test_unverified_controller_restart_is_retryable_without_repeating_data_migration(self):
        worker = self.worker()
        worker.fail_start = True
        worker.run()
        self.assertEqual(worker.state["stage"], "controller_unverified", worker.state)
        (self.directory / "state/notes.txt").write_text("keep newest data")
        repeated = self.worker()
        repeated.active = "inactive"
        repeated.run()
        self.assertEqual(repeated.state["status"], "completed", repeated.state)
        self.assertEqual(repeated.active, "active")
        self.assertEqual((self.directory / "state/notes.txt").read_text(), "keep newest data")
        self.assertFalse(any("migrate" in call for call in repeated.controller.calls))

    def test_foreign_running_operation_is_not_stopped(self):
        self.c.operation = {"id": "2" * 32, "status": "running", "kind": "activate"}
        self.c.persist()
        worker = self.worker()
        worker.run()
        self.assertEqual(worker.state["status"], "failed")
        self.assertEqual(worker.calls, [])
        self.assertEqual(read_json(self.c.operation_path)["id"], "2" * 32)

    def test_racing_operation_is_preserved_and_controller_resumes(self):
        worker = self.worker()
        worker.on_stop = lambda: write_json(self.c.operation_path, {"id": "3" * 32, "kind": "backup", "status": "queued"})
        worker.run()
        self.assertEqual(worker.state["status"], "failed")
        self.assertEqual(worker.active, "active")
        self.assertEqual(read_json(self.c.operation_path)["id"], "3" * 32)
        self.assertFalse((self.directory / "config/maintenance.json").exists())

    def test_storage_cleanup_is_not_interrupted_by_update(self):
        filename = self.directory / "config/storage-job.json"
        write_json(filename, {"id": "4" * 32, "status": "running"})
        worker = self.worker()
        worker.run()
        self.assertEqual(worker.state["status"], "failed")
        self.assertEqual(worker.calls, [])
        self.assertEqual(read_json(filename)["status"], "running")

    def test_racing_cleanup_abandons_update_and_restarts_controller(self):
        filename = self.directory / "config/storage-job.json"
        worker = self.worker()
        worker.on_stop = lambda: write_json(filename, {"id": "5" * 32, "status": "queued"})
        worker.run()
        self.assertEqual(worker.state["status"], "failed")
        self.assertEqual(worker.active, "active")
        self.assertEqual(read_json(filename)["status"], "queued")
        self.assertFalse((self.directory / "config/maintenance.json").exists())

    def test_bad_revision_fails_before_creating_instance_operation(self):
        self.request["revision"] = 10
        write_json(self.request_path, {"schema": 1, "request": self.request})
        worker = self.worker()
        worker.run()
        self.assertEqual(worker.state["status"], "failed")
        self.assertEqual(worker.active, "active")
        self.assertFalse(self.c.operation_path.exists())

    def test_only_fixed_update_fields_are_accepted(self):
        for change in ({"command": "rm -rf /"}, {"target_checksum": "../outside"}, {"job": "anything"},
                {"revision": True}, {"kind": "rollback", "rollback_id": "2" * 32}):
            with self.subTest(change=change), self.assertRaises((ValueError, SetupError)):
                validate_request({**self.request, **change})

    def test_rollback_keeps_management_protocol_current(self):
        self.worker().run()
        request = {**self.request, "id": "2" * 32, "kind": "rollback", "revision": 3,
            "expected_checksum": self.new.checksum, "target_checksum": self.old.checksum,
            "rollback_id": "1" * 32, "confirm_data_replacement": True}
        write_json(self.root / "updates" / (request["id"] + ".json"),
            {"schema": 1, "request": request, "runner_checksum": self.new.checksum})
        worker = LocalWorker(request["id"], self.root)
        worker.run()
        self.assertEqual(worker.state["status"], "completed", worker.state)
        deployed = read_json(self.root / "jobs" / (self.job + ".json"))
        self.assertEqual(deployed["checksum"], self.old.checksum)
        self.assertEqual(deployed["controller_checksum"], self.new.checksum)
        for unit in (self.root / "test-unit-registry").glob("*.service"):
            self.assertIn(self.new.checksum, unit.read_text())
        public = worker.controller.versions()
        self.assertNotIn("private-test-model-key", str(public))
        self.assertNotIn("private-qq-session", str(public))


class DispatchTests(WorkerFixture):
    def setUp(self):
        super().setUp()
        self.request_path.unlink()
        (self.root / "updates").chmod(0o700)
        self.source = self.root / "bootstrap" / self.job / self.new.checksum / "requests" / (self.request["id"] + ".json")
        self.source.parent.mkdir(parents=True)
        write_json(self.source, self.request)
        self.calls = []

    def dispatch(self):
        return dispatch(self.root, self.job, self.request["id"], self.new.checksum, sys.executable,
            register=lambda name, body: write_unit(name, body, self.root / "test-unit-registry"),
            systemctl=lambda *args: self.calls.append(args))

    def test_persisted_request_precedes_namespaced_dispatch_and_reuses_id(self):
        record = self.dispatch()
        self.assertEqual(record["status"], "queued")
        self.assertEqual(read_json(self.request_path)["request"], self.request)
        self.assertEqual(self.calls[-1], ("start", "--no-block", "gaoji-app-update-" + self.request["id"] + ".service"))
        unit = self.root / "test-unit-registry" / self.calls[-1][-1]
        self.assertIn("Restart=on-failure", unit.read_text())
        self.assertNotIn("nixos-rebuild", unit.read_text())
        public = read_json(self.root / "status/updates" / (self.request["id"] + ".json"))
        self.assertEqual(public["status"], "queued")
        self.dispatch()
        self.assertEqual(len(list((self.root / "updates").glob("*.json"))), 1)

    def test_private_dispatch_umask_does_not_hide_public_progress(self):
        mask = os.umask(0o077)
        try:
            self.dispatch()
        finally:
            os.umask(mask)
        self.assertEqual((self.root / "status").stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.root / "status/updates").stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.root / "updates").stat().st_mode & 0o777, 0o700)

    def test_same_id_cannot_be_retargeted_and_finished_request_does_not_restart(self):
        self.dispatch()
        write_json(self.source, {**self.request, "revision": 3})
        with self.assertRaises(SetupError):
            self.dispatch()
        write_json(self.source, self.request)
        record = read_json(self.request_path)
        record.update(finished=True, status="failed")
        write_json(self.request_path, record)
        self.calls.clear()
        self.dispatch()
        self.assertEqual(self.calls, [])

    def test_stale_config_and_mismatched_bundle_refused_before_dispatch(self):
        write_json(self.source, {**self.request, "revision": 20})
        with self.assertRaises(SetupError):
            self.dispatch()
        self.assertFalse(self.request_path.exists())
        write_json(self.source, {**self.request, "target_checksum": self.old.checksum})
        with self.assertRaises(SetupError):
            self.dispatch()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
