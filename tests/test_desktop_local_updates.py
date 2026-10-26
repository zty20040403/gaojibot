import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.local_runtime.desktop_updates import DesktopRuntimeUpdate, DesktopUpdateWorker, prepare_update
from src.local_runtime.files import read_json, write_json
from src.local_runtime.setup_clients import SetupError
from test_desktop_local_backups import DesktopBackupTests, LocalController
from test_desktop_backups import Fixture, FakeMaintenance, Crash, CHECKSUM, DEPENDENCY

NEW = "d" * 64


class LocalTransaction(DesktopRuntimeUpdate):
    active_tasks = 0

    def command(self, arguments, **kwargs):
        if "SELECT to_regclass('qq_bot.subagent_tasks') IS NOT NULL" in arguments:
            return b"t"
        if any("SELECT count(*) FROM qq_bot.subagent_tasks" in a for a in arguments):
            return str(self.active_tasks).encode()
        return FakeMaintenance.command(self, arguments, **kwargs)

    def prepare_release(self):
        target = self.c.operation["target_checksum"]
        return SimpleNamespace(checksum=target, image="gaoji-app-runtime:" + target,
            images={"postgres": DEPENDENCY, "napcat": DEPENDENCY})

    def verify_online(self, enabled):
        assert self.state["committed"]


class LocalUpgradeTests(DesktopBackupTests):

    def request(self, operation="1" * 32):
        return {"id": operation, "job": self.job, "kind": "update", "revision": 2,
            "expected_checksum": CHECKSUM, "target_checksum": NEW}

    def transaction(self):
        target = self.root / "releases" / NEW
        target.mkdir(parents=True, exist_ok=True)
        write_json(target / "images.lock.json", {"postgres": DEPENDENCY, "napcat": DEPENDENCY})
        self.c.operation = {**self.request(), "status": "queued"}
        self.c.controller_checksum = NEW
        self.c.persist()
        return LocalTransaction(self.c, lambda deployment: None)

    def test_upgrade_named_volume_candidate_and_rollback_keep_credentials(self):
        update = self.transaction()
        original = update.candidate_document
        documents = []
        def capture(*args, **kwargs):
            document = original(*args, **kwargs)
            documents.append(document)
            return document
        with patch.object(update, "candidate_document", capture):
            update.run_update()
        self.assertEqual(self.c.operation["status"], "completed", self.c.operation)
        self.assertEqual(self.c.deployment["checksum"], NEW)
        self.assertEqual(documents[0]["networks"], {"instance": {"internal": True}})
        self.assertEqual(self.c.napcat.base, "http://napcat:6099")
        self.assertEqual((self.directory / "runtime-config/admin.secret").read_bytes(), self.original_secret)
        self.c.operation = {**self.request("2" * 32), "status": "queued", "kind": "rollback", "expected_checksum": NEW,
            "target_checksum": CHECKSUM, "rollback_id": "1" * 32, "confirm_data_replacement": True}
        self.c.persist()
        LocalTransaction(self.c, lambda deployment: None).run_update()
        self.assertEqual(self.c.operation["status"], "completed", self.c.operation)
        self.assertEqual(self.c.deployment["checksum"], CHECKSUM)
        self.assertEqual(self.c.deployment["controller_checksum"], NEW)

    def test_active_task_refuses_upgrade_and_restores_running_state(self):
        transaction = self.transaction()
        transaction.active_tasks = 1
        transaction.run_update()
        self.assertEqual(self.c.deployment["checksum"], CHECKSUM)
        self.assertEqual(self.c.running, {"bot", "database", "napcat"})
        self.assertIn("Agent", self.c.operation["message"])
        self.assertFalse(transaction.marker.exists())

    def test_postcommit_crash_finishes_new_version_without_migrating_twice(self):
        transaction = self.transaction()
        with patch.object(transaction, "finish", side_effect=Crash()), self.assertRaises(Crash):
            transaction.run_update()
        (self.directory / "state/new-message").write_text("keep this")
        self.c = LocalController(self.job, self.root)
        self.c.controller_checksum = NEW
        recovered = LocalTransaction(self.c, lambda deployment: None)
        with patch.object(recovered, "stage_restore", side_effect=AssertionError("must not migrate again")):
            recovered.run_update()
        self.assertEqual(self.c.operation["status"], "completed", self.c.operation)
        self.assertEqual((self.directory / "state/new-message").read_text(), "keep this")

    def test_precommit_failure_restores_original(self):
        transaction = self.transaction()
        with patch.object(transaction, "stage_restore", side_effect=SetupError("candidate failed")):
            transaction.run_update()
        self.assertEqual(self.c.deployment["checksum"], CHECKSUM)
        self.assertEqual(self.c.running, {"bot", "database", "napcat"})
        self.assertEqual(self.c.operation["status"], "failed")

    def test_persistent_request_is_idempotent_and_cannot_change_target(self):
        write_json(self.root / "desktop.json", {"job": self.job})
        payload = {"request": self.request(), "runner_checksum": NEW, "port": 18080}
        prepare_update(payload, self.root)
        prepare_update(payload, self.root)
        self.assertEqual(len(list((self.root / "updates").glob("*.json"))), 1)
        with self.assertRaises(SetupError):
            prepare_update({**payload, "request": {**payload["request"], "target_checksum": "e" * 64}}, self.root)
        with self.assertRaises(SetupError):
            prepare_update({**payload, "request": self.request("2" * 32)}, self.root)

    def test_manager_does_not_reopen_writers_while_worker_owns_update(self):
        write_json(self.directory / "config/update-worker.json", {"id": "1" * 32})
        self.c.initialize()
        self.assertEqual(self.c.calls, [])

    def test_changed_configuration_has_terminal_receipt_without_stopping_manager(self):
        write_json(self.root / "desktop.json", {"job": self.job})
        request = {**self.request(), "revision": 999}
        prepare_update({"request": request, "runner_checksum": NEW, "port": 18080}, self.root)
        worker = DesktopUpdateWorker(request["id"], self.root)
        self.assertTrue(worker.state["finished"])
        self.assertEqual(worker.state["stage"], "refused")
        with patch.object(worker, "stop_manager") as stop:
            worker.run()
            stop.assert_not_called()
        self.assertFalse((self.directory / "config/update-worker.json").exists())

    def test_progress_is_public_without_credentials_and_records_actual_runtime(self):
        write_json(self.root / "desktop.json", {"job": self.job})
        prepare_update({"request": self.request(), "runner_checksum": NEW, "port": 18080}, self.root)
        worker = DesktopUpdateWorker("1" * 32, self.root)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            worker.save(status="running", stage="preparing", message="preparing")
        self.assertIn(CHECKSUM, output.getvalue())
        self.assertNotIn("private-test", output.getvalue())
        self.assertNotIn("token", output.getvalue())


if __name__ == "__main__":
    unittest.main()
