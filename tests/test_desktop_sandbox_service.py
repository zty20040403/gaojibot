"""Lifecycle and stop-hook tests; no real engine, systemd or server is touched."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from src.local_runtime.files import read_json, write_json
from src.local_runtime.sandbox_service import SandboxService, maintenance_allows_start, service_unit
from src.local_runtime.setup_clients import SetupError
from src.sandbox_runtime.docker import SandboxResult, SandboxError
from src.sandbox_runtime.quiesce import stop_instance
from src.sandbox_runtime.server import manager_from_config


INSTANCE = "a" * 32
IMAGE = "gaoji-app-sandbox:" + "b" * 64
IMAGE_ID = "sha256:" + "c" * 64


class ServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gj-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / INSTANCE
        (self.directory / "config").mkdir(parents=True)
        self.deployment = {"instance_id": INSTANCE, "docker": "/test/docker", "docker_host": "unix:///test/engine",
            "sandbox_image": IMAGE, "sandbox_image_id": IMAGE_ID}
        self.service = SandboxService(self.directory, self.deployment)

    def test_no_image_evidence_keeps_component_disabled_and_does_not_touch_systemd(self):
        for changed in ({"sandbox_image": "gaoji-sandbox:latest"}, {"sandbox_image_id": ""}):
            service = SandboxService(self.directory, {**self.deployment, **changed})
            with patch.object(service, "command") as command:
                self.assertFalse(service.available)
                self.assertIsNone(service.prepare())
                self.assertFalse(service.pause())
                with self.assertRaises(SetupError):
                    service.start()
                command.assert_not_called()
        self.assertFalse(self.service.config_path.exists())

    def test_prepare_keeps_private_credential_stable_and_never_enables_service(self):
        first = self.service.prepare()
        second = self.service.prepare()
        self.assertEqual(first, second)
        self.assertEqual(first["socket"], "/run/gaoji-sandbox/runner.sock")
        self.assertEqual(self.service.config_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.service.state_directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.service.socket_path.parent.stat().st_mode & 0o777, 0o750)
        self.assertFalse(self.service.enabled_path.exists())
        config = read_json(self.service.config_path)
        self.assertEqual(manager_from_config(config).namespace, INSTANCE)
        self.assertEqual(config["limits"]["max_total"], 2)
        self.assertNotIn("database_password", config)

    def test_public_config_or_symlink_cannot_be_adopted(self):
        self.service.prepare()
        os.chmod(self.service.config_path, 0o644)
        with self.assertRaisesRegex(SetupError, "权限"):
            self.service.prepare()
        self.service.config_path.unlink()
        outside = self.directory / "other.json"
        write_json(outside, {})
        self.service.config_path.symlink_to(outside)
        with self.assertRaises(SetupError):
            self.service.prepare()
        self.assertEqual(read_json(outside), {})

    def test_pause_removes_boot_marker_before_stop_and_checks_real_workloads(self):
        self.service.prepare()
        write_json(self.service.enabled_path, {})
        async def stopped(manager):
            self.assertEqual(manager.namespace, INSTANCE)
            self.assertFalse(self.service.enabled_path.exists())
            return 0
        def command(*args, **_kwargs):
            self.assertEqual(args, ("stop",))
            self.assertFalse(self.service.enabled_path.exists())
        with patch.object(self.service, "command", side_effect=command), \
                patch("src.local_runtime.sandbox_service.stop_instance", side_effect=stopped) as quiesce:
            self.assertTrue(self.service.pause())
            self.assertFalse(self.service.pause())
            self.assertEqual(quiesce.call_count, 2)
        self.assertTrue(self.service.config_path.exists())

    def test_failed_quiesce_is_not_reported_as_success(self):
        self.service.prepare()
        write_json(self.service.enabled_path, {})
        with patch.object(self.service, "command"), \
                patch("src.local_runtime.sandbox_service.stop_instance", new=AsyncMock(side_effect=SandboxError("still running"))):
            with self.assertRaisesRegex(SetupError, "复查失败"):
                self.service.pause()
        self.assertFalse(self.service.enabled_path.exists())

    def test_maintenance_blocks_reboot_start_until_data_is_restored(self):
        self.assertTrue(maintenance_allows_start(self.directory))
        marker = self.directory / "config/maintenance.json"
        for stage in ("preparing", "committing", "rolling_back", "committed"):
            write_json(marker, {"stage": stage})
            self.assertFalse(maintenance_allows_start(self.directory))
            with patch.object(self.service, "command") as command, self.assertRaisesRegex(SetupError, "维护"):
                self.service.start()
            command.assert_not_called()
        write_json(marker, {"stage": "resuming"})
        self.assertTrue(maintenance_allows_start(self.directory))

    def test_missing_or_retagged_image_is_not_pulled_and_does_not_start_runner(self):
        for result in (SandboxResult("", "missing", 1), SandboxResult("sha256:" + "d" * 64, "", 0)):
            with patch("src.sandbox_runtime.docker.DockerSandboxManager._run", new=AsyncMock(return_value=result)) as docker, \
                    patch.object(self.service, "command") as command, self.assertRaisesRegex(SetupError, "镜像"):
                self.service.start()
            self.assertIn("inspect", docker.call_args.args)
            command.assert_not_called()
            self.assertFalse(self.service.enabled_path.exists())

    def test_health_gates_start_and_broken_start_removes_enabled_marker(self):
        with patch.object(self.service, "verify_image"), patch.object(self.service, "command") as command, \
                patch("src.local_runtime.sandbox_service.SandboxClient.health", new=AsyncMock(return_value={
                    "channel_ready": True, "instance_id": INSTANCE, "image": IMAGE})):
            self.service.start()
            command.assert_called_once_with("start", timeout=30)
        self.assertTrue(self.service.enabled_path.exists())
        with patch.object(self.service, "verify_image"), patch.object(self.service, "command", side_effect=[SetupError("failed"), ""]), \
                patch("src.local_runtime.sandbox_service.stop_instance", new=AsyncMock(return_value=0)), self.assertRaises(SetupError):
            self.service.start()
        self.assertFalse(self.service.enabled_path.exists())

    def test_unit_has_fixed_operations_and_cannot_execute_arbitrary_shell(self):
        body = service_unit(self.directory, Path("/release"), "/usr/bin/python3", "docker.service")
        self.assertIn("ExecCondition=/usr/bin/python3 -m src.local_runtime.sandbox_service", body)
        self.assertIn("ExecStopPost=/usr/bin/python3 -m src.sandbox_runtime.quiesce", body)
        self.assertIn("RestrictAddressFamilies=AF_UNIX", body)
        self.assertNotIn("/bin/sh", body)
        self.assertIn("ConditionPathExists=" + str(self.service.enabled_path), body)
        for release, python, unit in (("/bad%path", "/python", "docker.service"),
                ("/release", "/python\nExecStart=/bin/sh", "docker.service"), ("/release", "/python", "other.service")):
            with self.assertRaises(ValueError):
                service_unit(self.directory, Path(release), python, unit)


class QuiesceTests(unittest.IsolatedAsyncioTestCase):
    async def test_stops_only_verified_immutable_ids_and_never_removes_volumes(self):
        from src.sandbox_runtime.docker import DockerSandboxManager
        manager = DockerSandboxManager(namespace=INSTANCE, image=IMAGE)
        identifier = "d" * 64
        labels = {"gaoji.app.instance": INSTANCE, "gaoji.app.resource": "sandbox", "gaoji.app.sandbox": "true", "qqbot.id": "s123abc"}
        manager._run = AsyncMock(side_effect=[SandboxResult(identifier, "", 0),
            SandboxResult(json.dumps(["/" + manager._container_name("s123abc"), labels, True]), "", 0),
            SandboxResult(identifier, "", 0), SandboxResult("", "", 0)])
        self.assertEqual(await stop_instance(manager), 1)
        calls = [call.args for call in manager._run.call_args_list]
        self.assertEqual(calls[2], ("docker", "stop", "--time", "5", identifier))
        self.assertFalse(any("rm" in call or "prune" in call or "volume" in call for call in calls))
        self.assertIn(f"label=gaoji.app.instance={INSTANCE}", calls[-1])

    async def test_foreign_or_unknown_containers_are_not_stopped(self):
        from src.sandbox_runtime.docker import DockerSandboxManager
        manager = DockerSandboxManager(namespace=INSTANCE, image=IMAGE)
        for labels in ({"gaoji.app.instance": "f" * 32}, {"gaoji.app.instance": INSTANCE, "gaoji.app.resource": "other"}):
            manager._run = AsyncMock(side_effect=[SandboxResult("d" * 64, "", 0),
                SandboxResult(json.dumps(["/unrelated", labels, True]), "", 0)])
            with self.assertRaises(SandboxError):
                await stop_instance(manager)
            self.assertFalse(any("stop" in call.args for call in manager._run.call_args_list))

    async def test_final_live_process_check_must_be_empty(self):
        from src.sandbox_runtime.docker import DockerSandboxManager
        manager = DockerSandboxManager(namespace=INSTANCE, image=IMAGE)
        manager._run = AsyncMock(side_effect=[SandboxResult("", "", 0), SandboxResult("d" * 64, "", 0)])
        with self.assertRaisesRegex(SandboxError, "仍有"):
            await stop_instance(manager)


if __name__ == "__main__":
    unittest.main()
