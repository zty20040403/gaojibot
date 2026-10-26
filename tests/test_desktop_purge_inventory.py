from __future__ import annotations

import copy
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from src.local_runtime.purge_inventory import PurgeInventory, tree_snapshot
from src.local_runtime.setup_clients import SetupError


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="gaoji-purge-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / ("c" * 32)
        self.directory.mkdir(mode=0o700)
        self.inventory = PurgeInventory(self.directory.name, self.directory,
            {"docker": "/fixture/docker", "docker_host": "unix:///fixture/engine"})
        self.container = {"Id": "a" * 64, "Name": "/" + self.inventory.project + "-bot-1", "Created": "2026-09-15T00:00:00Z",
            "Config": {"Labels": {"com.docker.compose.project": self.inventory.project, "com.docker.compose.service": "bot",
                "com.docker.compose.project.working_dir": str(self.directory), "com.docker.compose.project.config_files": str(self.directory / "compose.json")},
                "Env": ["SECRET=never-persist"]}, "State": {"Running": False},
            "Mounts": [{"Type": "bind", "Source": str(self.directory / "state"), "Destination": "/data/state"}]}
        name = self.inventory.prefix + "-work-s123abc"
        self.volume = {"Name": name, "CreatedAt": "2026-09-15T00:00:00Z", "Driver": "local", "Options": {},
            "Mountpoint": "/fixture/docker/volumes/" + name + "/_data",
            "Labels": {"gaoji.app.instance": self.directory.name, "gaoji.app.resource": "workspace", "qqbot.id": "s123abc", "qqbot.owner": "d" * 20}}
        self.network = {"Id": "b" * 64, "Name": self.inventory.project + "_default", "Created": "2026-09-15T00:00:00Z",
            "Driver": "bridge", "Labels": {"com.docker.compose.project": self.inventory.project, "com.docker.compose.network": "default"},
            "Containers": {self.container["Id"]: {}}, "Options": {}, "IPAM": {"Driver": "default"}}
        self.state = {"container": [self.container], "volume": [self.volume], "network": [self.network]}
        self.calls = []
        self.inventory.objects = lambda kind: copy.deepcopy(self.state[kind])
        self.inventory.command = self.command

    def command(self, *args):
        self.calls.append(args)
        kind = {"rm": "container", "volume": "volume", "network": "network"}[args[0]]
        key = "Name" if kind == "volume" else "Id"
        self.state[kind] = [row for row in self.state[kind] if row[key] != args[-1]]
        if kind == "container":
            self.network["Containers"].pop(args[-1], None)
        return ""

    def test_exact_resources_only_and_secret_free_snapshot(self):
        result = self.inventory.snapshot()
        self.assertEqual(result["containers"][0]["id"], self.container["Id"])
        self.assertEqual(result["volumes"][0]["id"], self.volume["Name"])
        self.assertEqual(result["networks"][0]["id"], self.network["Id"])
        self.assertNotIn("never-persist", json.dumps(result))
        self.assertNotIn("Env", json.dumps(result))
        self.assertEqual(self.calls, [])

    def test_delete_exact_container_id_then_unreferenced_volume_and_network(self):
        approved = self.inventory.snapshot()
        with self.assertRaises(SetupError):
            self.inventory.remove(approved, "volumes", self.volume["Name"])
        for kind in ("containers", "volumes", "networks"):
            self.inventory.remove(approved, kind, approved[kind][0]["id"])
        self.assertEqual(self.inventory.snapshot(), {"containers": [], "volumes": [], "networks": []})
        self.assertEqual(self.calls, [("rm", self.container["Id"]), ("volume", "rm", self.volume["Name"]), ("network", "rm", self.network["Id"])])
        self.inventory.remove(approved, "containers", self.container["Id"])
        self.assertEqual(len(self.calls), 3)

    def test_mount_order_does_not_invalidate_plan_but_mount_changes_do(self):
        self.container["Mounts"].append({"Type": "bind", "Source": str(self.directory / "files"),
            "Destination": "/data/files", "RW": False})
        approved = self.inventory.snapshot()
        self.container["Mounts"].reverse()
        self.assertEqual(self.inventory.remaining(approved), approved)
        self.container["Mounts"][0]["RW"] = True
        with self.assertRaises(SetupError):
            self.inventory.remaining(approved)
        self.assertEqual(self.calls, [])

    def test_running_and_restarting_containers_block_whole_plan(self):
        for state in ({"Running": True}, {"Running": False, "Restarting": True}, {"Running": False, "Paused": True}):
            self.container["State"] = state
            with self.assertRaises(SetupError):
                self.inventory.snapshot()
        self.assertEqual(self.calls, [])

    def test_same_name_or_project_without_full_ownership_is_rejected(self):
        for key in ("com.docker.compose.project", "com.docker.compose.project.working_dir", "com.docker.compose.project.config_files"):
            original = self.container["Config"]["Labels"].pop(key)
            with self.assertRaises(SetupError):
                self.inventory.snapshot()
            self.container["Config"]["Labels"][key] = original
        self.volume["Labels"] = {}
        with self.assertRaises(SetupError):
            self.inventory.snapshot()

    def test_foreign_container_data_or_volume_consumer_blocks_everything(self):
        foreign = {"Id": "f" * 64, "Name": "/somebody-else", "Created": "fixture", "Config": {"Labels": {}}, "State": {"Running": False}}
        self.state["container"].append(foreign)
        for mount in ({"Type": "bind", "Source": str(self.directory / "files")},
                      {"Type": "bind", "Source": str(self.directory.parent)},
                      {"Type": "volume", "Name": self.volume["Name"], "Source": self.volume["Mountpoint"]}):
            foreign["Mounts"] = [mount]
            with self.assertRaises(SetupError):
                self.inventory.snapshot()
        self.assertEqual(self.calls, [])

    def test_network_foreign_consumer_or_driver_blocks_deletion(self):
        self.network["Containers"]["f" * 64] = {}
        with self.assertRaises(SetupError):
            self.inventory.snapshot()
        self.network["Containers"].pop("f" * 64)
        self.network["Driver"] = "overlay"
        with self.assertRaises(SetupError):
            self.inventory.snapshot()

    def test_external_volume_driver_options_or_missing_creation_are_rejected(self):
        for key, value in (("Driver", "nfs"), ("Options", {"device": "/home"}), ("CreatedAt", "")):
            original = self.volume[key]
            self.volume[key] = value
            with self.assertRaises(SetupError):
                self.inventory.snapshot()
            self.volume[key] = original

    def test_sandbox_helper_and_private_nix_cache_are_recognized(self):
        self.container.update(Name="/" + self.inventory.prefix + "-s123abc")
        self.container["Config"]["Labels"] = {"gaoji.app.instance": self.directory.name, "gaoji.app.resource": "sandbox",
            "gaoji.app.sandbox": "true", "qqbot.id": "s123abc"}
        self.container["Mounts"] = [{"Type": "volume", "Name": self.volume["Name"], "Source": self.volume["Mountpoint"]}]
        self.assertEqual(len(self.inventory.snapshot()["containers"]), 1)
        self.container["Name"] = "/" + self.inventory.prefix + "-helper-" + "e" * 16
        self.container["Config"]["Labels"] = {"gaoji.app.instance": self.directory.name, "gaoji.app.resource": "helper"}
        self.assertEqual(len(self.inventory.snapshot()["containers"]), 1)
        self.volume["Name"] = self.inventory.prefix + "-nix"
        self.volume["Labels"] = {"gaoji.app.instance": self.directory.name, "gaoji.app.resource": "nix-cache"}
        self.container["Mounts"] = []
        self.assertEqual(self.inventory.snapshot()["volumes"][0]["name"], self.volume["Name"])

    def test_changed_or_new_resources_never_expand_approved_plan(self):
        approved = self.inventory.snapshot()
        original = self.volume["CreatedAt"]
        self.volume["CreatedAt"] = "new volume, same name"
        with self.assertRaises(SetupError):
            self.inventory.remove(approved, "containers", self.container["Id"])
        self.volume["CreatedAt"] = original
        self.container["Id"] = "e" * 64
        self.network["Containers"] = {self.container["Id"]: {}}
        with self.assertRaises(SetupError):
            self.inventory.remaining(approved)
        self.assertEqual(self.calls, [])

    def test_retired_directory_remains_protected_from_foreign_mounts(self):
        retired = self.directory.parent / "trash"
        self.state["container"].append({"Id": "f" * 64, "Name": "/other", "Config": {"Labels": {}},
            "State": {"Running": False}, "Mounts": [{"Source": str(retired / "database"), "Type": "bind"}]})
        with self.assertRaises(SetupError):
            self.inventory.snapshot(retired_path=retired)

    def test_exact_plan_required_and_no_force_or_prune(self):
        approved = self.inventory.snapshot()
        with self.assertRaises(SetupError):
            self.inventory.remove(approved, "containers", "f" * 64)
        with self.assertRaises(SetupError):
            self.inventory.remove(approved, "images", "a" * 64)
        self.assertEqual(self.calls, [])

    def test_successful_exit_without_removal_is_not_reported_as_verified(self):
        approved = self.inventory.snapshot()
        self.inventory.command = lambda *args: ""
        with self.assertRaisesRegex(SetupError, "未核实"):
            self.inventory.remove(approved, "containers", self.container["Id"])

    def test_tree_counts_links_and_sockets_without_following_targets(self):
        target = self.directory.parent / "outside"
        target.write_bytes(b"outside")
        (self.directory / "link").symlink_to(target)
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(str(self.directory / "stale.sock"))
        result = tree_snapshot(self.directory)
        self.assertEqual(result["entries"], 3)
        self.assertEqual(target.read_bytes(), b"outside")
        (self.directory / "data").write_bytes(b"new")
        self.assertNotEqual(tree_snapshot(self.directory)["fingerprint"], result["fingerprint"])

    def test_tree_mount_and_scan_limit_fail_closed(self):
        with patch("src.local_runtime.purge_inventory.Storage.kernel_mounts", return_value=[self.directory.resolve() / "external"]):
            with self.assertRaises(SetupError):
                tree_snapshot(self.directory)
        with self.assertRaises(SetupError):
            tree_snapshot(self.directory, max_files=0)

    def test_malformed_resource_metadata_is_reported_without_partial_deletion(self):
        self.container["Config"] = None
        with self.assertRaisesRegex(SetupError, "信息不完整"):
            self.inventory.snapshot()
        self.assertEqual(self.calls, [])

    def test_docker_enumeration_rejects_truncation_or_incomplete_inspection(self):
        actual = PurgeInventory(self.directory.name, self.directory, self.inventory.deployment)
        actual.command = lambda *args: "abc\n"
        with self.assertRaises(SetupError):
            actual.objects("container")
        actual.command = lambda *args: "a" * 64 if args[0] == "ps" else ""
        with self.assertRaises(SetupError):
            actual.objects("container")
        actual.command = lambda *args: "a" * 64 if args[0] == "ps" else json.dumps({"Id": "a" * 64})
        self.assertEqual(actual.objects("container"), [{"Id": "a" * 64}])


if __name__ == "__main__":
    unittest.main()
