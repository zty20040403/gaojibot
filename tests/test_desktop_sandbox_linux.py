"""Opt-in local Linux checks of sandbox boundaries, not a complete Nix toolchain.

Requires GAOJI_TEST_DOCKER and GAOJI_TEST_DOCKER_SOCKET (a local Unix socket).
The pinned Python fixture image must already be present. Each fixture owns its
random namespaces; no existing instance, image, volume or network is changed.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import unittest
import uuid
from unittest.mock import patch

from src.sandbox_runtime.docker import DockerSandboxManager, SandboxError


DOCKER = os.environ.get("GAOJI_TEST_DOCKER", "")
SOCKET = os.environ.get("GAOJI_TEST_DOCKER_SOCKET", "")
IMAGE = "docker.io/library/python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2"
SANDBOX = "s123abc"


@unittest.skipUnless(DOCKER and SOCKET, "Explicit local Docker fixture required")
class SandboxLinuxTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if not Path(DOCKER).is_absolute() or not SOCKET.startswith("unix:///"):
            raise ValueError("Only an explicit local Unix Docker socket is allowed")
        path = Path(SOCKET.removeprefix("unix://"))
        if not path.is_socket():
            raise ValueError("The local Docker socket is not available")
        result = cls.cli("image", "inspect", "--platform", "linux/amd64", IMAGE, "--format", "{{.Os}}/{{.Architecture}}")
        if result.stdout.strip() != "linux/amd64":
            raise ValueError("The fixture image must be the pinned Linux amd64 Python image")

    @staticmethod
    def cli(*arguments, check=True):
        result = subprocess.run([DOCKER, "--host", SOCKET, *arguments], capture_output=True,
                                text=True, timeout=45)
        if check and result.returncode:
            raise AssertionError(result.stderr[-1500:])
        return result

    async def asyncSetUp(self):
        platform = patch.dict(os.environ, {"DOCKER_DEFAULT_PLATFORM": "linux/amd64"})
        platform.start()
        self.addCleanup(platform.stop)
        self.managers = []
        self.addAsyncCleanup(self.clean_fixtures)
        self.manager = await self.fixture()

    async def fixture(self):
        manager = DockerSandboxManager(namespace=uuid.uuid4().hex, image=IMAGE,
            docker_executable=DOCKER, docker_host=SOCKET, memory_mib=512,
            cpus=1, pids_limit=64, max_file_bytes=1024 * 1024)
        self.managers.append(manager)
        volume = manager._workspace_volume_name(SANDBOX)
        await manager._create_volume(manager.nix_cache_volume, kind="nix-cache")
        await manager._create_volume(volume, kind="workspace", owner="owner", sandbox_id=SANDBOX)
        initialized = await manager._run("docker", "run", "--pull=never", "--rm", "--network", "none",
            "--user", "0:0", "--cap-drop", "ALL", "--cap-add", "CHOWN", "--read-only",
            "--memory", "256m", "--mount", f"type=volume,source={volume},target=/workspace", IMAGE,
            "python", "-c", "import os; os.chmod('/workspace',0o700); os.chown('/workspace',1000,1000)", timeout=30)
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        launched = await manager._run("docker", "run", "--pull=never", "-d", "--name", manager._container_name(SANDBOX),
            "--label", "gaoji.app.sandbox=true", *manager._resource_labels("sandbox"),
            "--label", f"qqbot.id={SANDBOX}", "--label", f"qqbot.owner={manager._owner_hash('owner')}",
            "--label", "qqbot.owner_ref=owner", "--label", "qqbot.runtime=python", "--label", "qqbot.purpose=task",
            "--user", "1000:1000", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--read-only", "--network", "none", "--memory", "512m", "--memory-swap", "512m",
            "--mount", f"type=volume,source={volume},target=/workspace",
            "--mount", f"type=volume,source={manager.nix_cache_volume},target=/nix,readonly",
            IMAGE, "python", "-c", "import time; time.sleep(600)", timeout=30)
        self.assertEqual(launched.returncode, 0, launched.stderr)
        return manager

    async def clean_fixtures(self):
        for manager in self.managers:
            raw = await asyncio.to_thread(self.cli, "ps", "-a", "--filter",
                f"label=gaoji.app.instance={manager.namespace}", "--format", "{{.Names}}")
            for name in raw.stdout.splitlines():
                if not name.startswith(f"gaoji-app-{manager.namespace}-"):
                    raise AssertionError("Refusing to clean an unexpected fixture name")
                await asyncio.to_thread(self.cli, "rm", "-f", name)
            for volume in (manager._workspace_volume_name(SANDBOX), manager.nix_cache_volume):
                inspected = await asyncio.to_thread(self.cli, "volume", "inspect", "--format", "{{json .Labels}}", volume, check=False)
                if inspected.returncode:
                    if "no such volume" not in inspected.stderr.lower():
                        raise AssertionError(inspected.stderr)
                    continue
                if json.loads(inspected.stdout).get("gaoji.app.instance") != manager.namespace:
                    raise AssertionError("Refusing to clean another instance's volume")
                await asyncio.to_thread(self.cli, "volume", "rm", volume)

    async def test_instances_and_users_cannot_access_each_others_workspace(self):
        first = self.manager
        second = await self.fixture()
        await first.write_file("owner", SANDBOX, "result.txt", b"first")
        await second.write_file("owner", SANDBOX, "result.txt", b"second")
        self.assertEqual(await first.read_file("owner", SANDBOX, "result.txt"), b"first")
        self.assertEqual(await second.read_file("owner", SANDBOX, "result.txt"), b"second")
        with self.assertRaises(SandboxError):
            await first.read_file("another-user", SANDBOX, "result.txt")
        self.assertEqual(len(await first.list("owner")), 1)
        self.assertEqual(len(await second.list("owner")), 1)
        await first.destroy("owner", SANDBOX)
        self.assertEqual(await first.list("owner"), [])
        self.assertEqual(await second.read_file("owner", SANDBOX, "result.txt"), b"second")
        self.assertTrue(await first._check_volume(first.nix_cache_volume, kind="nix-cache"))

    async def test_readonly_handoff_works_without_privileging_the_worker(self):
        manager = self.manager
        path = "upstream/abc/report.txt"
        await manager.install_readonly_file("owner", SANDBOX, path, b"report")
        await manager.install_readonly_file("owner", SANDBOX, path, b"report")
        with self.assertRaises(SandboxError):
            await manager.install_readonly_file("owner", SANDBOX, path, b"different")
        self.assertEqual(await manager.read_file("owner", SANDBOX, path), b"report")
        name = manager._container_name(SANDBOX)
        permissions = await manager._run("docker", "exec", name, "python", "-c",
            "import json,os,stat; s=os.stat('/workspace/upstream/abc/report.txt'); "
            "print(json.dumps({'uid':os.getuid(),'owner':s.st_uid,'mode':stat.S_IMODE(s.st_mode)}))", timeout=10)
        self.assertEqual(json.loads(permissions.stdout), {"uid": 1000, "owner": 0, "mode": 0o444})
        with self.assertRaises(SandboxError):
            await manager.write_file("owner", SANDBOX, path, b"overwrite")
        running = await manager._run("docker", "inspect", "--format", "{{json .HostConfig}}", name, timeout=10)
        config = json.loads(running.stdout)
        self.assertFalse(config["Privileged"])
        self.assertEqual(config["CapDrop"], ["ALL"])
        self.assertTrue(config["ReadonlyRootfs"])
        self.assertEqual(config["Memory"], 512 * 1024 * 1024)
        self.assertEqual(config["PidsLimit"], 64)
        self.assertEqual(config["NanoCpus"], 1_000_000_000)
        helpers = await asyncio.to_thread(self.cli, "ps", "-a", "--filter",
            f"label=gaoji.app.instance={manager.namespace}", "--filter", "label=gaoji.app.resource=helper", "--format", "{{.Names}}")
        self.assertEqual(helpers.stdout.strip(), "")

    async def test_timed_out_helper_is_not_left_running(self):
        manager = self.manager
        with self.assertRaises(SandboxError):
            await manager._run_bytes("docker", "run", "--pull=never", "--rm", "--network", "none",
                "--user", "1000:1000", "--cap-drop", "ALL", "--read-only", "--memory", "256m",
                IMAGE, "python", "-c", "import time; time.sleep(600)", timeout=2)
        helpers = await asyncio.to_thread(self.cli, "ps", "-a", "--filter",
            f"label=gaoji.app.instance={manager.namespace}", "--filter", "label=gaoji.app.resource=helper", "--format", "{{.Names}}")
        self.assertEqual(helpers.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
