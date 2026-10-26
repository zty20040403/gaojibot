from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_desktop_backups import Fixture, Crash, CHECKSUM, IMAGE
from test_desktop_lifecycle import LocalLifecycle
from src.local_runtime.files import read_json, write_json
from src.local_runtime.purge import Purge
from src.local_runtime.purge_inventory import PurgeInventory
from src.local_runtime.lifecycle_worker import dispatch, operation_task, retire_completed_purge, worker_unit
from src.local_runtime.services import write_unit
from src.local_runtime.setup_clients import SetupError


class LocalInventory(PurgeInventory):
    def __init__(self, *args, engine):
        super().__init__(*args)
        self.engine = engine

    def objects(self, kind):
        return copy.deepcopy(self.engine[kind])

    def command(self, *args):
        self.engine["calls"].append(args)
        kind = {"rm": "container", "volume": "volume", "network": "network"}[args[0]]
        key = "Name" if kind == "volume" else "Id"
        self.engine[kind] = [row for row in self.engine[kind] if row[key] != args[-1]]
        callback = self.engine.pop("after_remove", None)
        if callback:
            callback()
        return ""


class LocalPurge(Purge):
    def __init__(self, job, root, registry, backend, engine):
        self.backend, self.engine = backend, engine
        super().__init__(job, root, unit_root=registry)

    def lifecycle_class(self, job, root, *, unit_root):
        return LocalLifecycle(job, root, unit_root, self.backend)

    def inventory_class(self, *args):
        return LocalInventory(*args, engine=self.engine)


class PurgeTests(Fixture):
    def setUp(self):
        super().setUp()
        (self.root / "jobs").mkdir(mode=0o700)
        os.chmod(self.root / "data", 0o700)
        self.job = "9" * 32
        write_json(self.root / "jobs" / (self.job + ".json"), {"instance_id": self.identifier, "checksum": CHECKSUM,
            "runtime_image": IMAGE, "docker": "/test/docker", "docker_host": "unix:///test/engine", "engine_unit": "docker.service"})
        write_json(self.directory / "config/control.json", {"port": 12345, "token": "fixture-token"})
        self.registry = self.root / "units"
        self.backend = {"calls": [], "components": {"bot", "napcat", "database"}, "active": {}}
        self.life = LocalLifecycle(self.job, self.root, self.registry, self.backend)
        for name in self.life.names:
            write_unit(name, "[Service]\nExecStart=/fixture\n", self.registry)
            if name == self.life.names[0]:
                (self.registry / name).write_text(f"# Gaoji installer {self.job} {CHECKSUM}\n[Service]\nExecStart=/fixture\n")
            self.backend["active"][name] = True
        original = self.life.prepare("uninstall")
        self.life.submit({"id": original["id"], "confirm": True})
        self.assertEqual(self.life.run(original["id"])["status"], "completed")
        self.old_worker = "gaoji-app-lifecycle-" + original["id"] + ".service"
        write_unit(self.old_worker, f"[Service]\nExecStart=/fixture/python -m src.local_runtime.lifecycle_worker --job {self.job} --operation {original['id']}\n", self.registry)
        write_unit("gaoji-app-somebody-else.service", "[Service]\nExecStart=/other\n", self.registry)
        (self.root / "control-access").mkdir(mode=0o711)
        write_json(self.root / "control-access" / (self.job + ".json"), {"token": "private-access"})
        self.other = self.store.create("other instance")
        self.engine = {"calls": [], "container": [{"Id": "a" * 64, "Name": "/gaoji-" + self.identifier + "-bot-1", "Created": "fixture",
            "Config": {"Labels": {"com.docker.compose.project": "gaoji-" + self.identifier, "com.docker.compose.service": "bot",
                "com.docker.compose.project.working_dir": str(self.directory), "com.docker.compose.project.config_files": str(self.directory / "compose.json")}},
            "State": {"Running": False}, "Mounts": [{"Type": "bind", "Source": str(self.directory / "files")}]}], "volume": [], "network": []}
        self.purge = self.new_purge()

    def new_purge(self):
        return LocalPurge(self.job, self.root, self.registry, self.backend, self.engine)

    def confirm(self):
        preview = self.purge.prepare()
        self.purge.submit({"id": preview["id"], "confirm": True, "erase_instance_id": self.identifier})
        return preview

    def test_purge_removes_only_instance_data_and_old_access_not_shared_resources(self):
        self.publish()
        outside = self.root / "outside-user-file"
        outside.write_bytes(b"keep me")
        (self.directory / "files/link").symlink_to(outside)
        preview = self.confirm()
        self.assertFalse(preview["data_retained"])
        self.assertTrue(self.directory.exists())
        result = self.purge.run(preview["id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["service_state"], "removed")
        self.assertFalse(self.directory.exists())
        self.assertFalse((self.root / "control-access" / (self.job + ".json")).exists())
        self.assertFalse((self.registry / self.old_worker).exists())
        self.assertTrue((self.registry / "gaoji-app-somebody-else.service").exists())
        self.assertTrue(self.store.directory(self.other["id"]).exists())
        self.assertEqual(outside.read_bytes(), b"keep me")
        self.assertEqual(self.engine["calls"], [("rm", "a" * 64)])
        self.assertNotIn("private-test-model-key", json.dumps(result))
        self.assertNotIn("private-access", json.dumps(result))
        self.assertEqual(self.new_purge().run(preview["id"])["status"], "completed")
        recovered = operation_task(self.job, self.root, preview["id"])
        self.assertIsInstance(recovered, Purge)
        self.assertEqual(recovered.run(preview["id"])["status"], "completed")
        self.assertEqual(len(self.engine["calls"]), 1)

    def test_completed_purge_retires_only_its_own_executor_without_data_directory(self):
        preview = self.confirm()
        name = "gaoji-app-lifecycle-" + preview["id"] + ".service"
        write_unit(name, worker_unit(self.root, self.purge, preview, "/fixture/python"), self.registry)
        self.assertEqual(self.purge.run(preview["id"])["status"], "completed")
        recovered = self.new_purge()
        for _ in range(2):
            retire_completed_purge(recovered, preview["id"], python="/fixture/python", systemctl=self.life.systemctl)
        self.assertFalse((self.registry / name).exists())
        self.assertFalse((self.registry / "multi-user.target.wants" / name).is_symlink())
        self.assertTrue((self.registry / "gaoji-app-somebody-else.service").exists())
        self.assertFalse(self.directory.exists())

    def test_unfinished_worker_or_foreign_override_is_not_retired(self):
        preview = self.confirm()
        name = "gaoji-app-lifecycle-" + preview["id"] + ".service"
        write_unit(name, worker_unit(self.root, self.purge, preview, "/fixture/python"), self.registry)
        retire_completed_purge(self.purge, preview["id"], python="/fixture/python", systemctl=self.life.systemctl)
        self.assertTrue((self.registry / name).exists())
        self.assertEqual(self.purge.run(preview["id"])["status"], "completed")
        self.backend["dropins"] = {name: "/etc/systemd/foreign.conf"}
        with self.assertRaisesRegex(SetupError, "外部覆盖"):
            retire_completed_purge(self.purge, preview["id"], python="/fixture/python", systemctl=self.life.systemctl)
        self.assertTrue((self.registry / name).exists())
        self.assertTrue((self.registry / "multi-user.target.wants" / name).is_symlink())

    def test_separate_erase_confirmation_is_required_and_expiry_is_terminal(self):
        preview = self.purge.prepare()
        for payload in ({"id": preview["id"], "confirm": True}, {"id": preview["id"], "confirm": True, "erase_instance_id": self.other["id"]}):
            with self.assertRaises(SetupError):
                self.purge.submit(payload)
        with patch("src.local_runtime.purge.time.time", return_value=preview["created_at"] + 901):
            with self.assertRaisesRegex(SetupError, "过期"):
                self.purge.submit({"id": preview["id"], "confirm": True, "erase_instance_id": self.identifier})
        self.assertEqual(self.purge.load(preview["id"])["status"], "failed")
        self.assertFalse(self.purge.load(preview["id"])["resumable"])
        self.assertTrue(self.directory.exists())
        self.assertEqual(self.engine["calls"], [])

    def test_changed_data_or_foreign_consumer_blocks_before_any_deletion(self):
        preview = self.confirm()
        (self.directory / "files/new").write_text("new data")
        result = self.purge.run(preview["id"])
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["resumable"])
        self.assertEqual(self.engine["calls"], [])
        (self.directory / "files/new").unlink()
        preview = self.confirm()
        self.engine["container"].append({"Id": "f" * 64, "Name": "/other", "Config": {"Labels": {}},
            "State": {"Running": False}, "Mounts": [{"Type": "bind", "Source": str(self.directory)}]})
        self.assertEqual(self.purge.run(preview["id"])["status"], "failed")
        self.assertTrue(self.directory.exists())
        self.assertEqual(self.engine["calls"], [])

    def test_controller_capacity_initialization_does_not_invalidate_preview(self):
        from src.local_runtime.capacity import prepare_directory
        create_lifecycle = self.purge.lifecycle_class

        def initialize_controller(*args, **kwargs):
            life = create_lifecycle(*args, **kwargs)
            prepare_directory(life.directory)
            return life

        with patch.object(self.purge, "lifecycle_class", side_effect=initialize_controller):
            preview = self.confirm()
            result = self.purge.run(preview["id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertFalse(self.directory.exists())

    def test_worker_removal_must_not_touch_foreign_overrides(self):
        preview = self.confirm()
        self.backend["dropins"] = {self.old_worker: "/etc/systemd/foreign.conf"}
        self.assertEqual(self.purge.run(preview["id"])["status"], "failed")
        self.assertTrue((self.registry / self.old_worker).exists())
        self.assertTrue(self.directory.exists())

    def test_crash_after_container_removal_resumes_without_repeating_removal(self):
        preview = self.confirm()
        def crash():
            raise Crash()
        self.engine["after_remove"] = crash
        with self.assertRaises(Crash):
            self.purge.run(preview["id"])
        self.assertTrue(self.directory.exists())
        result = self.new_purge().run(preview["id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.engine["calls"], [("rm", "a" * 64)])

    def test_crash_after_atomic_retirement_resumes_without_controller_data(self):
        preview = self.confirm()
        rename = os.rename
        def crash(source, destination):
            rename(source, destination)
            raise Crash()
        with patch("src.local_runtime.purge.os.rename", side_effect=crash), self.assertRaises(Crash):
            self.purge.run(preview["id"])
        self.assertFalse(self.directory.exists())
        self.assertTrue(self.purge.quarantine(preview).exists())
        result = self.new_purge().run(preview["id"])
        self.assertEqual(result["status"], "completed", result)

    def test_partial_tree_deletion_resumes_with_identity_preserved(self):
        preview = self.confirm()
        def partial(path):
            (path / "files/report.pdf").unlink()
            raise Crash()
        with patch("src.local_runtime.purge.shutil.rmtree", side_effect=partial) as remover:
            remover.avoids_symlink_attacks = True
            with self.assertRaises(Crash):
                self.purge.run(preview["id"])
        self.assertFalse(self.directory.exists())
        result = self.new_purge().run(preview["id"])
        self.assertEqual(result["status"], "completed", result)

    def test_reused_original_path_or_new_mount_does_not_get_deleted_on_resume(self):
        preview = self.confirm()
        rename = os.rename
        def crash(source, destination):
            rename(source, destination)
            raise Crash()
        with patch("src.local_runtime.purge.os.rename", side_effect=crash), self.assertRaises(Crash):
            self.purge.run(preview["id"])
        self.directory.mkdir(mode=0o700)
        (self.directory / "new-file").write_text("not approved")
        result = self.new_purge().run(preview["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual((self.directory / "new-file").read_text(), "not approved")
        self.assertEqual([path.name for path in self.directory.iterdir()], ["new-file"])
        (self.directory / "new-file").unlink()
        self.directory.rmdir()
        target = self.purge.quarantine(preview)
        with patch("src.local_runtime.purge.Storage.kernel_mounts", return_value=[target.resolve() / "database"]):
            self.assertEqual(self.new_purge().run(preview["id"])["status"], "failed")
        self.assertTrue(target.exists())

    def test_failed_access_revocation_can_resume_after_data_is_gone(self):
        preview = self.confirm()
        with patch.object(self.purge, "retire_access", side_effect=SetupError("access fixture failed")):
            self.assertEqual(self.purge.run(preview["id"])["status"], "failed")
        self.assertFalse(self.directory.exists())
        result = self.new_purge().run(preview["id"])
        self.assertEqual(result["status"], "completed", result)

    def test_deployment_change_is_rejected_before_delete_even_on_same_object(self):
        preview = self.confirm()
        current = read_json(self.purge.job_path)
        current["docker_host"] = "unix:///different-engine"
        write_json(self.purge.job_path, current)
        self.assertEqual(self.purge.run(preview["id"])["status"], "failed")
        self.assertEqual(self.engine["calls"], [])
        self.assertTrue(self.directory.exists())

    def test_dispatch_requires_erase_identity_and_persists_before_fixed_worker_start(self):
        preview = self.purge.prepare()
        release = self.root / "releases" / CHECKSUM
        source = release / "src/local_runtime"
        source.mkdir(parents=True)
        for name in ("lifecycle.py", "lifecycle_state.py", "lifecycle_worker.py", "purge.py", "purge_inventory.py"):
            (source / name).write_text("# fixture\n")
        factory = lambda job, root: LocalPurge(job, root, self.registry, self.backend, self.engine)
        calls, units = [], {}
        with patch("src.local_runtime.lifecycle_worker.load_release", return_value=SimpleNamespace(directory=release)):
            with self.assertRaises(SetupError):
                dispatch(self.root, self.job, preview["id"], confirm=True, lifecycle_class=factory)
            result = dispatch(self.root, self.job, preview["id"], confirm=True, erase_instance_id=self.identifier,
                lifecycle_class=factory, python="/fixture/python", register=lambda name, body: units.update({name: body}),
                systemctl=lambda *args: calls.append(args))
        self.assertEqual(result["status"], "queued")
        name = "gaoji-app-lifecycle-" + preview["id"] + ".service"
        self.assertEqual(set(units), {name})
        self.assertIn("--job " + self.job + " --operation " + preview["id"], units[name])
        self.assertEqual(calls, [("daemon-reload",), ("start", "--no-block", name)])
        self.assertEqual(self.engine["calls"], [])
        self.assertTrue(self.directory.exists())
