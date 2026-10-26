from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_desktop_backups import Fixture, FakeController, Crash, CHECKSUM, IMAGE
from src.local_runtime.files import read_json, write_json
from src.local_runtime.lifecycle import Lifecycle
from src.local_runtime.lifecycle_worker import dispatch, worker_unit
from src.local_runtime.services import write_unit
from src.local_runtime.setup_clients import SetupError
from src.local_runtime.sandbox_service import maintenance_allows_start
from src.local_runtime.manager import main as manager_main


class Sandbox:
    def __init__(self, directory, backend):
        self.enabled_path = directory / "config/sandbox.enabled"
        self.backend = backend

    def pause(self, *, service_stopped=False):
        self.backend["calls"].append(("sandbox.pause", service_stopped))
        was_enabled = self.enabled_path.exists()
        self.enabled_path.unlink(missing_ok=True)
        return was_enabled

    def start(self):
        self.backend["calls"].append(("sandbox.start",))
        self.enabled_path.write_text("{}")


class LocalController(FakeController):
    def __init__(self, job, root, backend):
        super().__init__(job, root)
        self.running = backend["components"]
        self.sandbox = Sandbox(self.directory, backend)
        self.backend = backend

    def compose(self, arguments, **kwargs):
        self.backend["calls"].append(("compose", *arguments))
        if arguments == ["stop"]:
            if not self.backend.get("stop_unverified"):
                self.running.clear()
        else:
            super().compose(arguments, **kwargs)

    def services(self):
        return {"services": [{"name": name, "state": "running", "health": self.backend.get("database_health", "healthy") if name == "database" else ""} for name in self.running]}


class LocalLifecycle(Lifecycle):
    verification_timeout = 0
    def __init__(self, job, root, registry, backend):
        self.backend = backend
        super().__init__(job, root, unit_root=registry)

    def controller_class(self, job, root):
        return LocalController(job, root, self.backend)

    def systemctl(self, *arguments):
        self.backend["calls"].append(arguments)
        name = arguments[1] if len(arguments) > 1 else None
        if arguments[0] == "show":
            filename = self.unit_root / name
            override = self.backend.get("override", {}).get(name)
            fragment = override or (str(filename) if filename.exists() else "")
            return "\n".join(["LoadState=" + ("loaded" if fragment else "not-found"),
                "ActiveState=" + ("active" if self.backend["active"].get(name) else "inactive"),
                "FragmentPath=" + fragment, "DropInPaths=" + self.backend.get("dropins", {}).get(name, "")])
        if arguments[0] == "stop":
            self.backend["active"][name] = False
            if self.backend.get("on_stop"):
                self.backend["on_stop"](name)
        if arguments[0] == "start":
            if self.backend.get("fail_start") == name:
                raise SetupError("simulated start failure")
            self.backend["active"][name] = True
            if name == self.names[1]:
                self.backend["components"].update({"database", "bot", "napcat"})
        return ""


class LifecycleTests(Fixture):
    def setUp(self):
        super().setUp()
        (self.root / "jobs").mkdir()
        self.job = "9" * 32
        write_json(self.root / "jobs" / (self.job + ".json"), {"instance_id": self.identifier, "checksum": CHECKSUM,
            "runtime_image": IMAGE, "docker": "/test/docker", "docker_host": "unix:///test/engine", "engine_unit": "docker.service"})
        write_json(self.directory / "config/control.json", {"port": 12345, "token": "fixture-token"})
        self.registry = self.root / "units"
        self.backend = {"calls": [], "components": {"bot", "napcat", "database"}, "active": {}}
        self.life = self.new_lifecycle()
        for name in self.life.names:
            write_unit(name, "[Service]\nExecStart=/test/does-not-run\n", self.registry)
            if name == self.life.names[0]:
                (self.registry / name).write_text(f"# Gaoji installer {self.job} {CHECKSUM}\n[Service]\nExecStart=/test/does-not-run\n")
            self.backend["active"][name] = True
        write_unit("gaoji-app-other-instance.service", "[Service]\nExecStart=/test/other\n", self.registry)
        self.other = (self.registry / "gaoji-app-other-instance.service").read_bytes()

    def new_lifecycle(self):
        return LocalLifecycle(self.job, self.root, self.registry, self.backend)

    def execute(self, kind="uninstall"):
        preview = self.life.prepare(kind)
        self.life.submit({"id": preview["id"], "confirm": True})
        return self.life.run(preview["id"])

    def test_uninstall_removes_only_owned_services_preserving_all_data(self):
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        for name in self.life.names:
            self.assertFalse((self.registry / name).exists())
            self.assertFalse((self.registry / "multi-user.target.wants" / name).is_symlink())
        self.assertEqual((self.registry / "gaoji-app-other-instance.service").read_bytes(), self.other)
        self.assertEqual((self.directory / "database/original").read_bytes(), b"original database")
        self.assertEqual((self.directory / "files/report.pdf").read_bytes(), b"fixture-pdf")
        self.assertEqual((self.directory / "napcat/QQ/session").read_bytes(), b"private-qq-session")
        self.assertEqual(read_json(self.life.marker)["state"], "retained")
        self.assertFalse(maintenance_allows_start(self.directory))
        self.assertNotIn("private-test-model-key", str(result))
        with self.assertRaises(SetupError):
            self.life.c.mutable()
        with patch("sys.argv", ["manager", "--job", self.job]), patch("src.local_runtime.manager.Controller", return_value=self.life.c), patch("src.local_runtime.manager.ManagerServer") as server:
            manager_main()
            server.assert_not_called()
        for call in self.backend["calls"]:
            if call[0] in {"start", "stop", "show"}:
                self.assertIn(call[1], self.life.names)
        self.backend["calls"].clear()
        self.assertEqual(self.life.run(result["id"])["status"], "completed")
        self.assertEqual(self.backend["calls"], [])

    def test_explicit_reinstall_restores_original_units_without_new_database(self):
        original = {name: (self.registry / name).read_bytes() for name in self.life.names}
        self.assertEqual(self.execute()["status"], "completed")
        self.life = self.new_lifecycle()
        restored = self.execute("reinstall")
        self.assertEqual(restored["status"], "completed", restored)
        self.assertFalse(self.life.marker.exists())
        for name, content in original.items():
            self.assertEqual((self.registry / name).read_bytes(), content)
            link = self.registry / "multi-user.target.wants" / name
            if name == self.life.names[0]:
                self.assertFalse(link.is_symlink())
            else:
                self.assertEqual(os.readlink(link), "../" + name)
            self.assertEqual(self.backend["active"][name], name != self.life.names[0])
        self.assertEqual((self.directory / "database/original").read_bytes(), b"original database")
        self.assertEqual(self.backend["components"], {"database", "bot", "napcat"})

    def test_reinstall_does_not_wait_for_installer_while_holding_its_lock(self):
        self.assertEqual(self.execute()["status"], "completed")
        self.life = self.new_lifecycle()
        self.backend["calls"].clear()
        self.backend["fail_start"] = self.life.names[0]
        restored = self.execute("reinstall")
        self.assertEqual(restored["status"], "completed", restored)
        self.assertNotIn(("start", self.life.names[0]), self.backend["calls"])
        self.assertTrue((self.registry / self.life.names[0]).is_file())
        self.assertFalse((self.registry / "multi-user.target.wants" / self.life.names[0]).is_symlink())

    def test_reinstall_resume_removes_boot_link_left_by_older_worker(self):
        self.assertEqual(self.execute()["status"], "completed")
        self.life = self.new_lifecycle()
        self.backend["fail_start"] = self.life.names[-1]
        result = self.execute("reinstall")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["resumable"])
        link = self.registry / "multi-user.target.wants" / self.life.names[0]
        link.symlink_to("../" + self.life.names[0])
        self.backend.pop("fail_start")
        self.life = self.new_lifecycle()
        resumed = self.life.run(result["id"])
        self.assertEqual(resumed["status"], "completed", resumed)
        self.assertFalse(link.is_symlink())
        self.assertNotIn(("start", self.life.names[0]), self.backend["calls"])

    def test_unconfirmed_stale_and_changed_configuration_do_nothing(self):
        preview = self.life.prepare("uninstall")
        with self.assertRaises(SetupError):
            self.life.run(preview["id"])
        with self.assertRaises(SetupError):
            self.life.submit({"id": preview["id"], "confirm": False})
        with self.assertRaises(SetupError):
            self.life.submit({"id": preview["id"], "confirm": True, "path": "/"})
        self.store.update(self.identifier, self.config["settings"], expected_revision=2)
        with self.assertRaises(SetupError):
            self.life.submit({"id": preview["id"], "confirm": True})
        failed = self.life.load(preview["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertFalse(failed["resumable"])
        self.assertFalse(self.life.marker.exists())
        self.assertFalse(any(call[0] == "stop" for call in self.backend["calls"]))

    def test_expired_preview_is_terminal_and_can_be_replaced_without_stopping_services(self):
        preview = self.life.prepare("uninstall")
        with patch("src.local_runtime.lifecycle.time.time", return_value=preview["created_at"] + 901):
            with self.assertRaisesRegex(SetupError, "过期"):
                self.life.submit({"id": preview["id"], "confirm": True})
        current = read_json(self.root / "status/lifecycles" / (preview["id"] + ".json"))
        self.assertEqual(current["schema"], 1)
        self.assertEqual(current["status"], "failed")
        self.assertEqual(current["service_state"], "installed")
        self.assertFalse(current["resumable"])
        self.assertNotEqual(self.life.prepare("uninstall")["id"], preview["id"])
        self.assertFalse(self.life.marker.exists())
        self.assertFalse(any(call[0] == "stop" for call in self.backend["calls"]))

    def test_foreign_unit_dropin_or_boot_link_is_not_modified(self):
        name = self.life.names[1]
        self.backend["dropins"] = {name: "/etc/systemd/system/custom.conf"}
        with self.assertRaisesRegex(SetupError, "外部覆盖"):
            self.life.prepare("uninstall")
        self.backend.pop("dropins")
        self.backend["override"] = {name: "/etc/systemd/system/other.service"}
        with self.assertRaises(SetupError):
            self.life.prepare("uninstall")
        self.backend.pop("override")
        link = self.registry / "multi-user.target.wants" / name
        link.unlink(); link.symlink_to("/another/service")
        with self.assertRaises(SetupError):
            self.life.prepare("uninstall")
        self.assertFalse(any(call[0] == "stop" for call in self.backend["calls"]))

    def test_changed_unit_after_preview_prevents_all_stops(self):
        preview = self.life.prepare("uninstall")
        self.life.submit({"id": preview["id"], "confirm": True})
        write_unit(self.life.names[1], "[Service]\nExecStart=/test/changed\n", self.registry)
        result = self.life.run(preview["id"])
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["resumable"])
        self.assertFalse(any(call[0] == "stop" for call in self.backend["calls"]))

    def test_still_running_component_does_not_lose_unit_definitions(self):
        self.backend["stop_unverified"] = True
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["resumable"])
        self.assertTrue(all((self.registry / name).exists() for name in self.life.names))
        self.backend["stop_unverified"] = False
        self.life = self.new_lifecycle()
        self.assertEqual(self.life.run(result["id"])["status"], "completed")

    def test_restart_after_first_definition_removal_continues_original_task(self):
        preview = self.life.prepare("uninstall")
        self.life.submit({"id": preview["id"], "confirm": True})
        original = self.life.remove_definition
        def interrupt(unit):
            original(unit)
            raise Crash()
        with patch.object(self.life, "remove_definition", side_effect=interrupt):
            with self.assertRaises(Crash):
                self.life.run(preview["id"])
        self.life = self.new_lifecycle()
        result = self.life.run(preview["id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual((self.directory / "files/report.pdf").read_bytes(), b"fixture-pdf")

    def test_restart_after_sandbox_unit_removal_still_quiesces_without_stopping_missing_unit(self):
        preview = self.life.prepare("uninstall")
        self.life.submit({"id": preview["id"], "confirm": True})
        original = self.life.remove_definition
        def interrupt(unit):
            original(unit)
            if unit["name"] == self.life.names[2]:
                raise Crash()
        with patch.object(self.life, "remove_definition", side_effect=interrupt):
            with self.assertRaises(Crash):
                self.life.run(preview["id"])
        self.life = self.new_lifecycle()
        result = self.life.run(preview["id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertIn(("sandbox.pause", True), self.backend["calls"])

    def test_reinstall_retries_failed_service_start_without_replacing_data(self):
        self.assertEqual(self.execute()["status"], "completed")
        self.life = self.new_lifecycle()
        self.backend["fail_start"] = self.life.names[-1]
        result = self.execute("reinstall")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["resumable"])
        self.backend.pop("fail_start")
        self.life = self.new_lifecycle()
        result = self.life.run(result["id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual((self.directory / "database/original").read_bytes(), b"original database")

    def test_active_service_without_healthy_database_is_not_reported_complete(self):
        self.assertEqual(self.execute()["status"], "completed")
        self.life = self.new_lifecycle()
        self.backend["database_health"] = "unhealthy"
        result = self.execute("reinstall")
        self.assertEqual(result["status"], "failed")
        self.assertIn("数据库健康", result["message"])
        self.backend["database_health"] = "healthy"
        self.life = self.new_lifecycle()
        self.assertEqual(self.life.run(result["id"])["status"], "completed")

    def test_foreign_compose_project_is_never_stopped(self):
        path = self.directory / "compose.json"
        value = read_json(path)
        value["name"] = "another-person"
        write_json(path, value)
        with self.assertRaises(SetupError):
            self.life.prepare("uninstall")
        self.assertFalse(any(call[0] == "stop" for call in self.backend["calls"]))

    def test_racing_configuration_task_restores_manager_and_original_boot_links(self):
        def on_stop(name):
            if name == self.life.names[-1]:
                write_json(self.directory / "config/operation.json", {"id": "b" * 32, "status": "queued"})
        self.backend["on_stop"] = on_stop
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["resumable"])
        self.assertFalse(self.life.marker.exists())
        self.assertTrue(self.backend["active"][self.life.names[-1]])
        self.assertTrue(all((self.registry / "multi-user.target.wants" / name).is_symlink() for name in self.life.names))
        self.assertEqual(self.backend["components"], {"database", "bot", "napcat"})

    def test_pending_update_and_cleanup_block_uninstall(self):
        (self.root / "updates").mkdir()
        path = self.root / "updates" / ("1" * 32 + ".json")
        write_json(path, {"request": {"job": self.job}, "status": "queued"})
        with self.assertRaises(SetupError):
            self.life.prepare("uninstall")
        path.unlink()
        write_json(self.directory / "config/storage-job.json", {"id": "2" * 32, "status": "running"})
        with self.assertRaises(SetupError):
            self.life.prepare("uninstall")

    def test_conflict_compensation_can_resume_when_manager_restart_fails(self):
        def on_stop(name):
            if name == self.life.names[-1]:
                write_json(self.directory / "config/operation.json", {"id": "b" * 32, "status": "queued"})
        self.backend.update(on_stop=on_stop, fail_start=self.life.names[-1])
        result = self.execute()
        self.assertEqual(result["stage"], "compensating")
        self.assertTrue(result["resumable"])
        self.backend.pop("fail_start")
        self.life = self.new_lifecycle()
        result = self.life.run(result["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stage"], "conflict")
        self.assertFalse(result["resumable"])
        self.assertTrue(self.backend["active"][self.life.names[-1]])
        self.assertFalse(self.life.marker.exists())

    def test_second_task_cannot_overwrite_queued_lifecycle(self):
        preview = self.life.prepare("uninstall")
        self.life.submit({"id": preview["id"], "confirm": True})
        with self.assertRaises(SetupError):
            self.life.prepare("uninstall")

    def test_reinstall_retry_does_not_overwrite_new_foreign_definition(self):
        self.assertEqual(self.execute()["status"], "completed")
        self.life = self.new_lifecycle()
        preview = self.life.prepare("reinstall")
        self.life.submit({"id": preview["id"], "confirm": True})
        (self.registry / self.life.names[1]).write_text("# manually managed\n")
        result = self.life.run(preview["id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("manually managed", (self.registry / self.life.names[1]).read_text())

    def test_dispatch_is_fixed_durable_and_requires_confirmation(self):
        preview = self.life.prepare("uninstall")
        release = self.root / "releases" / CHECKSUM
        source = release / "src/local_runtime"
        source.mkdir(parents=True)
        for name in ("lifecycle.py", "lifecycle_state.py", "lifecycle_worker.py"):
            (source / name).write_text("# fixture, never executed\n")
        calls, units = [], {}
        factory = lambda job, root: LocalLifecycle(job, root, self.registry, self.backend)
        with self.assertRaises(SetupError):
            dispatch(self.root, self.job, preview["id"], lifecycle_class=factory)
        with patch("src.local_runtime.lifecycle_worker.load_release", return_value=SimpleNamespace(directory=release)):
            result = dispatch(self.root, self.job, preview["id"], confirm=True, lifecycle_class=factory,
                python="/test/python", register=lambda name, body: units.update({name: body}), systemctl=lambda *args: calls.append(args))
        self.assertEqual(result["status"], "queued")
        name = "gaoji-app-lifecycle-" + preview["id"] + ".service"
        self.assertEqual(set(units), {name})
        self.assertIn("--job " + self.job + " --operation " + preview["id"], units[name])
        self.assertIn("Restart=on-failure", units[name])
        self.assertIn("WantedBy=multi-user.target", units[name])
        self.assertNotIn("Requires=docker.service", units[name])
        self.assertEqual(calls, [("daemon-reload",), ("start", "--no-block", name)])
        self.assertFalse(any(call[0] == "stop" for call in self.backend["calls"]))
        status = read_json(self.root / "status/lifecycles" / (preview["id"] + ".json"))
        self.assertEqual(status["status"], "queued")
        self.assertNotIn("ExecStart", str(status))
        self.assertNotIn("fixture-token", str(status))
        with self.assertRaises(ValueError):
            worker_unit(self.root, self.life, self.life.load(preview["id"]), "/test/python --bad")
