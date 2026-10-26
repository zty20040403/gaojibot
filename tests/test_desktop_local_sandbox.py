import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from src.local_runtime.desktop import desktop_compose, ROOT
from src.local_runtime.desktop_sandbox import DesktopSandboxImage, sandbox_volume_sizes
from src.local_runtime.desktop_updates import engine_socket_matches
from src.storage_admission import CapacityError

REPO = Path(__file__).parents[1]


class DesktopSandboxTests(unittest.TestCase):
    def test_desktop_socket_proxy_requires_original_engine_mount(self):
        target = "/var/run/docker.sock"
        mount = {"Type": "bind", "Source": target, "Destination": target, "RW": True}
        requested = {"Type": "bind", "Source": target, "Target": target}
        row = {"Mounts": [mount], "HostConfig": {"Mounts": [requested]}}
        self.assertTrue(engine_socket_matches(row))
        mount["Source"] = "/run/host-services/docker.proxy.sock"
        self.assertTrue(engine_socket_matches(row))
        requested["Source"] = mount["Source"]
        self.assertTrue(engine_socket_matches(row))
        requested["Source"] = "/some/other.sock"
        self.assertFalse(engine_socket_matches(row))
        requested["Source"] = target
        mount["RW"] = False
        self.assertFalse(engine_socket_matches(row))
        mount["RW"] = True
        mount["Source"] = "/some/other.sock"
        self.assertFalse(engine_socket_matches(row))

    def test_builder_uses_only_own_volume_subpaths_and_rejects_foreign_mounts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            for name in ("default.nix", "build.sh", "builder.lock.json"):
                shutil.copyfile(REPO / "packaging/sandbox" / name, source / name)
            shutil.copyfile(REPO / "nix/sandbox-image.nix", source / "image.nix")
            job = "a" * 32
            deployment = {"job": job, "volume": "gaoji-desktop-" + job + "-data",
                          "docker": "/test/docker", "docker_host": "unix:///test/socket"}
            image = DesktopSandboxImage(source, root / "sandbox-images", deployment=deployment, root=root)
            args = image.builder_mount_arguments()
            self.assertNotIn("type=bind", " ".join(args))
            self.assertIn("volume-subpath=sandbox-images/", " ".join(args))
            mounts = [{"Type": "volume", "Source": deployment["volume"], "Target": "/" + child,
                       "ReadOnly": child == "recipe", "VolumeOptions": {"Subpath": image.prefix + "/" + child}}
                      for child in ("recipe", "out")]
            value = {"Mounts": [{}, {}], "HostConfig": {"Mounts": mounts}}
            self.assertTrue(image.builder_mounts_match(value))
            mounts[1]["Source"] = "somebody-else"
            self.assertFalse(image.builder_mounts_match(value))
            self.assertTrue(image.container.endswith(job))

    def test_bot_receives_only_runner_socket_not_engine_or_controller_credentials(self):
        instance, job = "b" * 32, "a" * 32
        config = {"id": instance, "ports": {"bot": 28080, "napcat": 28081}}
        deployment = {"job": job, "volume": "gaoji-desktop-" + job + "-data", "network": "gaoji-desktop-" + job,
                      "runtime_image": "gaoji-app-runtime:" + "c" * 64}
        images = {"postgres": "postgres@sha256:" + "d" * 64, "napcat": "napcat@sha256:" + "e" * 64}
        document = desktop_compose(config, ROOT / "data/instances" / instance, deployment, images, sandbox_enabled=True)
        mounts = document["services"]["bot"]["volumes"]
        runner = [m for m in mounts if m["target"] == "/run/gaoji-sandbox"]
        self.assertEqual(len(runner), 1)
        self.assertTrue(runner[0]["read_only"])
        self.assertTrue(runner[0]["volume"]["subpath"].endswith("/sandbox/socket"))
        self.assertNotIn("docker.sock", json.dumps(document))
        self.assertFalse(any(m["target"] == str(ROOT) for m in mounts))

    def test_volume_accounting_filters_other_instances_and_fails_closed_on_unknown_size(self):
        instance = "a" * 32
        name = "gaoji-app-" + instance + "-nix"
        row = {"Name": name, "UsageData": {"Size": 4096},
            "Labels": {"gaoji.app.instance": instance, "gaoji.app.resource": "nix-cache"}}
        with patch("src.local_runtime.desktop_sandbox.DockerConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = json.dumps({"Volumes": [row, {"Name": "foreign", "UsageData": {"Size": 999999}}]}).encode()
            self.assertEqual(sandbox_volume_sizes(instance, [name]), [4096])
            row["UsageData"]["Size"] = -1
            response.read.return_value = json.dumps({"Volumes": [row]}).encode()
            with self.assertRaises(CapacityError):
                sandbox_volume_sizes(instance, [name])
            row["UsageData"]["Size"] = 4096
            row["Labels"]["gaoji.app.instance"] = "b" * 32
            response.read.return_value = json.dumps({"Volumes": [row]}).encode()
            with self.assertRaises(CapacityError):
                sandbox_volume_sizes(instance, [name])


if __name__ == "__main__":
    unittest.main()
