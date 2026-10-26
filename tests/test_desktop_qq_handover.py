from __future__ import annotations

from itertools import count
from unittest.mock import patch

from test_desktop_backups import Fixture, FakeController, CHECKSUM, IMAGE, DEPENDENCY, Crash
from src.local_runtime.files import read_json, write_json
from src.local_runtime.instances import ConfigurationConflict
from src.local_runtime.qq_handover import read_handover, require_handover, start_allowed
from src.local_runtime.setup_clients import SetupError


class HandoverTests(Fixture):
    def setUp(self):
        super().setUp()
        (self.root / "jobs").mkdir()
        self.job = "9" * 32
        write_json(self.root / "jobs" / (self.job + ".json"), {"instance_id": self.identifier,
            "checksum": CHECKSUM, "runtime_image": IMAGE, "docker": "/test/docker",
            "docker_host": "unix:///test/engine"})
        release = self.root / "releases" / CHECKSUM
        release.mkdir(parents=True)
        write_json(release / "images.lock.json", {"postgres": DEPENDENCY, "napcat": DEPENDENCY})
        write_json(self.directory / "config/control.json", {"port": 12345, "token": "fixture-token"})
        self.c = FakeController(self.job, self.root)
        self.c.running = {"database"}
        require_handover(self.directory, "3" * 32, "4" * 32)
        self.request = {"id": "5" * 32, "kind": "resume_qq", "revision": 2, "confirm": True}

    def queue(self):
        with patch.object(self.c, "resume"):
            return self.c.submit(self.request)

    def test_pending_gate_survives_render_and_manager_reconstruction(self):
        self.c.render(False)
        restarted = FakeController(self.job, self.root)
        restarted.render(False)
        napcat = read_json(self.directory / "compose.json")["services"]["napcat"]
        self.assertEqual(napcat["profiles"], ["qq-handover"])
        self.assertEqual(napcat["restart"], "no")
        self.assertFalse(start_allowed(self.directory))
        with patch.object(restarted.napcat, "status") as status, patch.object(restarted.napcat, "refresh") as refresh:
            self.assertEqual(restarted.qq_status()["phase"], "handover_required")
            with self.assertRaisesRegex(SetupError, "接管"):
                restarted.qq_refresh()
            status.assert_not_called()
            refresh.assert_not_called()
        with self.assertRaisesRegex(SetupError, "接管"), patch.object(restarted, "resume") as resume:
            restarted.submit({"id": "6" * 32, "kind": "activate", "revision": 2})
        resume.assert_not_called()
        self.assertEqual(restarted.calls, [])

    def test_confirmation_revision_and_stable_operation_id(self):
        for confirm in (False, None, 1, "true"):
            with self.subTest(confirm=confirm), self.assertRaises(SetupError):
                self.c.submit(self.request | {"confirm": confirm})
        with self.assertRaises(ConfigurationConflict):
            self.c.submit(self.request | {"revision": 1})
        self.assertFalse(self.c.operation_path.exists())
        first = self.queue()
        self.assertEqual(self.queue(), first)
        self.assertEqual(self.c.calls, [])
        self.assertEqual(read_handover(self.directory)["state"], "pending")

    def test_verified_handover_starts_qq_but_never_bot(self):
        self.queue()
        with patch.object(self.c.napcat, "status", side_effect=[SetupError("warming"), {}, {"online": False, "qrcode": "fixture-qr"}]), \
                patch("src.local_runtime.manager.time.sleep") as sleep:
            self.c.execute()
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(self.c.operation["status"], "completed", self.c.operation)
        self.assertIsNone(read_handover(self.directory))
        self.assertEqual(self.c.running, {"database", "napcat"})
        self.assertFalse(read_json(self.c.desired_path)["bot_enabled"])
        self.assertNotIn("profiles", read_json(self.directory / "compose.json")["services"]["napcat"])
        self.assertEqual(self.c.submit(self.request), self.c.public_operation())
        self.assertEqual(len(self.c.calls), 1)

    def test_startup_timeout_keeps_consent_and_explicit_retry_available(self):
        self.queue()
        with patch.object(self.c, "render"), patch.object(self.c.napcat, "status", return_value={}), \
                patch("src.local_runtime.manager.time.monotonic", side_effect=count(0, 200)):
            self.c.execute()
        self.assertEqual(self.c.operation["status"], "failed")
        self.assertIn("登录入口尚未确认", self.c.operation["message"])
        self.assertEqual(read_handover(self.directory)["state"], "approved")
        self.assertFalse(read_json(self.c.desired_path)["bot_enabled"])
        self.request["id"] = "6" * 32
        self.queue()
        with patch.object(self.c.napcat, "status", return_value={"online": True}):
            self.c.execute()
        self.assertEqual(self.c.operation["status"], "completed")

    def test_worker_restart_after_gate_removed_finishes_without_new_start(self):
        self.queue()
        original = self.c.persist
        def crash(**changes):
            if changes.get("status") == "completed":
                raise Crash()
            original(**changes)
        with patch.object(self.c, "persist", side_effect=crash), \
                patch.object(self.c.napcat, "status", return_value={"online": True}), self.assertRaises(Crash):
            self.c.execute()
        self.assertIsNone(read_handover(self.directory))
        restarted = FakeController(self.job, self.root)
        restarted.execute()
        self.assertEqual(restarted.operation["id"], self.request["id"])
        self.assertEqual(restarted.operation["status"], "completed")
        self.assertEqual(restarted.calls, [])

    def test_bad_marker_cannot_enable_qq(self):
        path = self.directory / "config/qq-handover.json"
        for value in ({}, {**read_handover(self.directory), "target_instance_id": "f" * 32}):
            write_json(path, value)
            with self.assertRaises(SetupError):
                self.c.render(False)
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(SetupError):
            self.c.qq_status()
        path.unlink()
        path.symlink_to(self.directory / "instance.json")
        with self.assertRaises(SetupError):
            self.c.render(False)
