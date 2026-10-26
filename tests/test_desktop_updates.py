from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import test_desktop_backups as backups
from src.local_runtime.backups import BackupError
from src.local_runtime.files import read_json, write_json
from src.local_runtime.maintenance import CONFIG, Maintenance, write_bytes
from src.local_runtime.sandbox_service import maintenance_allows_start
from src.local_runtime.releases import RUNTIME_CHECK_TIMEOUT, REQUIRED_FILES, ReleaseError, load_release, runtime_check_arguments
from src.local_runtime.setup_clients import SetupError
from src.local_runtime.updates import RuntimeUpdate


def release(root, checksum, version="0.20.1"):
    folder = root / "releases" / checksum
    folder.mkdir(parents=True, mode=0o700)
    files = {name: b"# verified test fixture\n" for name in REQUIRED_FILES}
    files["images.lock.json"] = json.dumps({"schema": 1, **{name: backups.DEPENDENCY
        for name in ("python", "postgres", "napcat")}}).encode()
    entries = []
    for name, content in sorted(files.items()):
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o600)
        entries.append({"path": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})
    write_json(folder / "manifest.json", {"schema": 1, "version": version, "files": entries})
    write_json(folder / ".bundle.json", {"sha256": checksum})
    return load_release(root / "releases", checksum)


class ReleaseTests(unittest.TestCase):
    def test_runtime_timeout_reports_the_failing_step_without_command_details(self):
        from types import SimpleNamespace
        update = RuntimeUpdate.__new__(RuntimeUpdate)
        update.c = SimpleNamespace(process_environment=lambda: {})
        args = runtime_check_arguments("/test/docker", "gaoji-app-runtime:" + "a" * 64)
        with patch("src.local_runtime.updates.subprocess.run", side_effect=subprocess.TimeoutExpired(args, RUNTIME_CHECK_TIMEOUT)):
            with self.assertRaisesRegex(SetupError, "运行镜像离线自检超过 300 秒"):
                update.runtime_command(args, timeout=RUNTIME_CHECK_TIMEOUT)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.release = release(self.root, "a" * 64)

    def test_verified_image_matches_installer_identity_and_offline_check(self):
        identity = hashlib.sha256(json.dumps(self.release.manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(self.release.image, "gaoji-app-runtime:" + identity)
        args = runtime_check_arguments("/test/docker", self.release.image)
        for option in ("--read-only", "--pull=never", "--network", "none", "10001:10001", "check-runtime"):
            self.assertIn(option, args)
        self.assertNotIn("--volume", args)

    def test_rejects_changed_source_and_group_writable_files(self):
        path = self.release.directory / "Dockerfile"
        original = path.read_bytes()
        path.write_bytes(b"tampered")
        with self.assertRaisesRegex(ReleaseError, "摘要"):
            load_release(self.root / "releases", self.release.checksum)
        path.write_bytes(original)
        path.chmod(0o660)
        with self.assertRaisesRegex(ReleaseError, "权限"):
            load_release(self.root / "releases", self.release.checksum)

    def test_rejects_linked_files_directories_and_hardlinks(self):
        path = self.release.directory / "Dockerfile"
        original = path.read_bytes()
        outside = self.root / "outside"
        outside.write_bytes(original)
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(ReleaseError):
            load_release(self.root / "releases", self.release.checksum)
        path.unlink()
        os.link(outside, path)
        with self.assertRaises(ReleaseError):
            load_release(self.root / "releases", self.release.checksum)
        path.unlink()
        path.write_bytes(original)
        parent = self.release.directory / "src"
        parent.rename(self.root / "other-src")
        parent.symlink_to(self.root / "other-src", target_is_directory=True)
        with self.assertRaises(ReleaseError):
            load_release(self.root / "releases", self.release.checksum)

    def test_bad_markers_paths_duplicates_and_missing_dependencies(self):
        for change in (lambda value: value["files"].append(value["files"][0]),
                lambda value: value["files"][0].update(path="../escape"),
                lambda value: value["files"].pop(), lambda value: value.update(version="latest")):
            with self.subTest(change=change):
                value = json.loads(json.dumps(self.release.manifest))
                change(value)
                write_json(self.release.directory / "manifest.json", value)
                with self.assertRaises(ReleaseError):
                    load_release(self.root / "releases", self.release.checksum)
        for value in (None, "../outside", "a" * 63):
            with self.assertRaises(ReleaseError):
                load_release(self.root / "releases", value)


class SimulatedUpdate(RuntimeUpdate):
    command = backups.FakeMaintenance.command

    def runtime_command(self, arguments, *, timeout):
        self.c.calls.append(arguments)
        return b"cached image"

    def verify_online(self, enabled):
        assert read_json(self.marker)["committed"] is True
        assert (self.history / (self.state["id"] + ".json")).exists()
        self.c.calls.append(["verified", enabled])


class UpdateFixture(backups.Fixture):
    def setUp(self):
        super().setUp()
        self.old = release(self.root, backups.CHECKSUM, "0.20.0")
        self.new = release(self.root, "d" * 64)
        (self.root / "jobs").mkdir()
        self.job = "9" * 32
        write_json(self.root / "jobs" / (self.job + ".json"), {"instance_id": self.identifier,
            "checksum": self.old.checksum, "runtime_image": self.old.image,
            "docker": "/test/docker", "docker_host": "unix:///test/engine"})
        write_json(self.directory / "config/control.json", {"port": 12345, "token": secrets.token_urlsafe(32)})
        self.c = backups.FakeController(self.job, self.root)
        write_json(self.c.desired_path, {"bot_enabled": True, "revision": 2})
        self.installed = []

    def transaction(self, identifier="1" * 32, kind="update", **extra):
        self.c.operation = {"id": identifier, "kind": kind, "status": "queued",
            "expected_checksum": self.c.deployment["checksum"], "target_checksum": self.new.checksum, **extra}
        self.c.persist()
        return SimulatedUpdate(self.c, lambda value: self.installed.append(value["checksum"]))

    def recover(self):
        self.c = backups.FakeController(self.job, self.root)
        return SimulatedUpdate(self.c, lambda value: self.installed.append(value["checksum"]))

    def assert_original(self):
        self.assertEqual(self.c.deployment["checksum"], self.old.checksum)
        self.assertTrue((self.directory / "database/original").exists())
        self.assertEqual(self.store.read(self.identifier)["revision"], 2)
        self.assertEqual((self.directory / "config/admin.secret").read_bytes(), self.original_secret)
        self.assertFalse((self.directory / "config/maintenance.json").exists())


class UpdateTests(UpdateFixture):
    def test_update_migrates_only_copy_then_preserves_keys_and_running_state(self):
        transaction = self.transaction()
        command = transaction.command
        def verify_migration(arguments, **kwargs):
            if "migrate" in arguments:
                self.assertEqual(self.c.deployment["checksum"], self.old.checksum)
                self.assertTrue((self.directory / "database/original").exists())
                self.assertNotIn("bot", self.c.running)
                document = read_json(kwargs["compose"])
                self.assertEqual(document["services"]["bot"]["image"], self.new.image)
                self.assertNotIn("napcat", document["services"])
                self.assertNotIn("ports", document["services"]["bot"])
            return command(arguments, **kwargs)
        with patch.object(transaction, "command", side_effect=verify_migration):
            transaction.run_update()
        self.assertEqual(self.c.operation["status"], "completed", self.c.operation)
        self.assertEqual(self.c.deployment["checksum"], self.new.checksum)
        self.assertEqual(self.c.running, {"bot", "database", "napcat"})
        self.assertTrue(read_json(self.c.desired_path)["bot_enabled"])
        self.assertEqual(self.store.snapshot(self.identifier)[1]["model_keys"], {"mine": "private-test-model-key"})
        self.assertEqual((self.directory / "napcat/QQ/session").read_bytes(), b"private-qq-session")
        self.assertEqual((self.directory / "config/admin.secret").read_bytes(), self.original_secret)
        count = len(self.c.calls)
        self.recover().run_update()
        self.assertEqual(len(self.c.calls), 0)  # Completed receipt does not restart or migrate.
        self.assertGreater(count, 0)

    def test_bad_release_or_migration_failure_preserves_original(self):
        for step in ("prepare_release", "stage_restore"):
            with self.subTest(step=step):
                transaction = self.transaction(identifier=("2" if step == "prepare_release" else "3") * 32)
                with patch.object(transaction, step, side_effect=SetupError("simulated verification failure")):
                    transaction.run_update()
                self.assertEqual(self.c.operation["status"], "failed")
                self.assert_original()
                self.assertEqual(self.c.running, {"bot", "database", "napcat"})

    def test_failed_operation_receipt_cannot_reuse_a_stale_snapshot(self):
        transaction = self.transaction()
        with patch.object(transaction, "stage_restore", side_effect=SetupError("migration failed")):
            transaction.run_update()
        (self.directory / "state/notes.txt").write_text("received after automatic rollback")
        repeated = self.recover()
        with patch.object(repeated, "snapshot", side_effect=AssertionError("must use a new operation ID")):
            repeated.run_update()
        self.assertEqual(self.c.operation["status"], "failed")
        self.assertEqual((self.directory / "state/notes.txt").read_text(), "received after automatic rollback")

    def test_all_eight_interrupted_directory_moves_restore_original_runtime_and_data(self):
        rename = os.rename
        for number in range(1, 9):
            with self.subTest(move=number):
                transaction = self.transaction(identifier=f"{number + 30:032x}")
                count = 0
                def die(source, target):
                    nonlocal count
                    rename(source, target)
                    count += 1
                    if count == number:
                        raise backups.Crash("power loss")
                with patch("src.local_runtime.maintenance.os.rename", side_effect=die), self.assertRaises(backups.Crash):
                    transaction.run_update()
                self.recover().run_update()
                self.assert_original()

    def test_crash_after_release_pointer_write_restores_original(self):
        transaction = self.transaction()
        switch = transaction.set_deployment
        def die(value):
            switch(value)
            raise backups.Crash("after release pointer write")
        with patch.object(transaction, "set_deployment", side_effect=die), self.assertRaises(backups.Crash):
            transaction.run_update()
        self.assertEqual(read_json(transaction.record_path)["checksum"], self.new.checksum)
        self.recover().run_update()
        self.assert_original()

    def test_all_config_write_interruptions_restore_matching_credentials(self):
        for number in range(1, len(CONFIG) + 1):
            with self.subTest(write=number):
                transaction = self.transaction(identifier=f"{number + 50:032x}")
                count = 0
                def die(filename, data, **kwargs):
                    nonlocal count
                    write_bytes(filename, data, **kwargs)
                    if filename in [self.directory / name for name in CONFIG]:
                        count += 1
                        if count == number:
                            raise backups.Crash("after config write")
                with patch("src.local_runtime.maintenance.write_bytes", side_effect=die), self.assertRaises(backups.Crash):
                    transaction.run_update()
                self.recover().run_update()
                self.assert_original()
                self.assertEqual(self.store.snapshot(self.identifier)[1]["model_keys"], {"mine": "private-test-model-key"})

    def test_crash_at_durable_commit_finishes_new_version_without_rollback(self):
        transaction = self.transaction()
        save = transaction.save
        def die(**values):
            save(**values)
            if values.get("committed") is True:
                raise backups.Crash("commit saved, no traffic yet")
        with patch.object(transaction, "save", side_effect=die), self.assertRaises(backups.Crash):
            transaction.run_update()
        self.assertTrue(read_json(transaction.marker)["committed"])
        self.assertNotIn("bot", self.c.running)
        self.recover().run_update()
        self.assertEqual(self.c.operation["status"], "completed", self.c.operation)
        self.assertEqual(self.c.deployment["checksum"], self.new.checksum)

    def test_sandbox_gate_only_opens_after_durable_update_commit(self):
        write_json(self.c.sandbox.enabled_path, {})
        transaction = self.transaction()
        def pause():
            self.assertFalse(maintenance_allows_start(self.directory))
            self.c.sandbox.enabled_path.unlink(missing_ok=True)
        def start():
            self.assertTrue(maintenance_allows_start(self.directory))
            self.assertTrue(read_json(transaction.marker)["committed"])
            write_json(self.c.sandbox.enabled_path, {})
        with patch.object(self.c.sandbox, "pause", side_effect=pause), patch.object(self.c.sandbox, "start", side_effect=start) as started:
            transaction.run_update()
        self.assertEqual(self.c.operation["status"], "completed", self.c.operation)
        started.assert_called_once()

    def test_unverified_new_runtime_never_discards_new_messages_or_replays_migration(self):
        transaction = self.transaction()
        with patch.object(transaction, "verify_online", side_effect=SetupError("QQ offline")):
            transaction.run_update()
        self.assertEqual(self.c.operation["stage"], "update_unverified")
        (self.directory / "state/notes.txt").write_text("messages received after update")
        recovered = self.recover()
        with patch.object(recovered, "stage_restore", side_effect=AssertionError("must not migrate again")):
            recovered.run_update()
        self.assertEqual(self.c.operation["status"], "completed", self.c.operation)
        self.assertEqual(self.c.deployment["checksum"], self.new.checksum)
        self.assertEqual((self.directory / "state/notes.txt").read_text(), "messages received after update")

    def test_explicit_rollback_restores_old_pair_and_preserves_new_data(self):
        self.transaction().run_update()
        (self.directory / "state/notes.txt").write_text("new-version data")
        rollback = self.transaction("2" * 32, "rollback", target_checksum=self.old.checksum,
            rollback_id="1" * 32, confirm_data_replacement=True)
        before = len(self.c.calls)
        rollback.run_update()
        self.assertEqual(self.c.operation["stage"], "rolled_back", self.c.operation)
        self.assertEqual(self.c.deployment["checksum"], self.old.checksum)
        self.assertEqual((self.directory / "state/notes.txt").read_text(), "群聊记忆")
        self.assertEqual((self.directory / "recovery" / ("2" * 32) / "rollback/state/notes.txt").read_text(), "new-version data")
        self.assertFalse(any("migrate" in call for call in self.c.calls[before:]))
        self.backups.check("2" * 32, self.new.checksum)

    def test_rollback_without_confirmation_and_stale_versions_never_pause(self):
        self.transaction().run_update()
        self.c.calls.clear()
        transaction = self.transaction("2" * 32, "rollback", target_checksum=self.old.checksum, rollback_id="1" * 32)
        transaction.run_update()
        self.assertEqual(self.c.operation["status"], "failed")
        self.assertEqual(self.c.calls, [])
        self.assertEqual(self.c.deployment["checksum"], self.new.checksum)
        transaction = self.transaction("3" * 32, target_checksum=self.old.checksum, expected_checksum="f" * 64)
        transaction.run_update()
        self.assertEqual(self.c.calls, [])

    def test_generic_recovery_and_controller_do_not_take_over_update(self):
        transaction = self.transaction()
        transaction.begin("update", metadata={"original_deployment": dict(self.c.deployment), "committed": False,
            "target_checksum": self.new.checksum})
        with self.assertRaisesRegex(BackupError, "独立更新"):
            Maintenance(self.c).run(recovery_only=True)
        with patch("src.local_runtime.manager.threading.Thread") as thread:
            self.c.resume()
            thread.assert_not_called()
        with self.assertRaisesRegex(SetupError, "版本更新"):
            self.c.submit({"id": "e" * 32, "kind": "recover", "revision": 2})
        transaction.original()


if __name__ == "__main__":
    unittest.main()
