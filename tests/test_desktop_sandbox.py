"""App sandbox boundary tests; no live Docker or server is contacted."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.sandbox_runtime.docker import DockerSandboxManager, SandboxError, SandboxResult


INSTANCE = "a" * 32
IMAGE = "gaoji-app-sandbox:" + "b" * 64
ID = "s123abc"


def driver(**options):
    return DockerSandboxManager(namespace=INSTANCE, image=IMAGE, **options)


def container_labels(manager, *, owner="owner"):
    return {"gaoji.app.instance": manager.namespace, manager._sandbox_label: "true",
            "qqbot.id": ID, "qqbot.owner": manager._owner_hash(owner), "qqbot.purpose": "task"}


def result(value):
    return SandboxResult(json.dumps(value), "", 0)


class SandboxConfigurationTests(unittest.TestCase):
    def test_driver_import_needs_only_standard_library(self):
        root = str(Path(__file__).resolve().parents[1])
        process = subprocess.run([sys.executable, "-S", "-c",
            "import sys; from src.sandbox_runtime.docker import DockerSandboxManager; "
            "assert 'nonebot' not in sys.modules; assert 'src.plugins.ai_chat' not in sys.modules"],
            cwd="/private/tmp" if sys.platform == "darwin" else "/tmp",
            env={**os.environ, "PYTHONPATH": root}, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)

    def test_namespace_and_images_are_fixed_not_user_commands(self):
        for namespace in ("h310", "../app", "A" * 32, "a" * 33):
            with self.subTest(namespace=namespace), self.assertRaises(ValueError):
                DockerSandboxManager(namespace=namespace, image=IMAGE)
        for image in ("", "gaoji-sandbox:latest", "alpine:latest", "--privileged"):
            with self.subTest(image=image), self.assertRaises(ValueError):
                DockerSandboxManager(namespace=INSTANCE, image=image)

    def test_scoped_resources_do_not_share_legacy_names(self):
        manager = driver(nix_cache_volume="gaoji-nix-v2")
        other = DockerSandboxManager(namespace="c" * 32, image=IMAGE)
        self.assertNotEqual(manager._container_name(ID), other._container_name(ID))
        self.assertEqual(manager._workspace_volumes(ID), {f"gaoji-app-{INSTANCE}-work-{ID}"})
        self.assertEqual(manager.nix_cache_volume, f"gaoji-app-{INSTANCE}-nix")
        self.assertNotEqual(manager._sandbox_label, DockerSandboxManager()._sandbox_label)

    def test_default_production_names_preserved_and_helpers_always_bounded(self):
        manager = DockerSandboxManager()
        self.assertEqual(manager._container_name(ID), f"qqbot-{ID}")
        self.assertEqual(manager._workspace_volume_name(ID), f"gaoji-work-{ID}")
        self.assertEqual(manager._memory_limit(), "8g")
        command = manager._docker_command(("docker", "run", "image"))
        self.assertEqual(command[command.index("--cpus") + 1], "2")
        self.assertEqual(command[command.index("--pids-limit") + 1], "256")
        self.assertEqual(command[command.index("--network") + 1], "gaoji-sandbox")

    def test_instances_bound_helper_and_workload_resources(self):
        manager = driver(memory_mib=768, cpus=1, pids_limit=128)
        for arguments in (("docker", "run", "-d", IMAGE), ("docker", "run", "--rm", IMAGE)):
            command = manager._docker_command(arguments)
            self.assertEqual(command[command.index("--cpus") + 1], "1")
            self.assertEqual(command[command.index("--pids-limit") + 1], "128")
            self.assertIn("max-size=10m", command)
        self.assertEqual(manager._memory_limit(), "768m")
        self.assertEqual(manager._memory_limit(2048), "768m")
        self.assertEqual(manager._memory_limit(512), "512m")

    def test_resource_options_and_docker_transport_are_validated(self):
        for options in ({"memory_mib": 0}, {"memory_mib": 8193}, {"memory_mib": True},
                        {"cpus": 0}, {"cpus": True}, {"pids_limit": -1},
                        {"max_file_bytes": 0}, {"max_file_bytes": 257 * 1024 * 1024},
                        {"docker_host": "tcp://h310:2375"}, {"docker_executable": "../docker"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                driver(**options)
        with self.assertRaises(SandboxError):
            driver()._docker_command(("sh", "-c", "id"))


class SandboxIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_lookup_and_admin_listing_always_filter_instance(self):
        manager = driver()
        manager._run = AsyncMock(return_value=SandboxResult("", "", 0))
        await manager.list("owner")
        await manager.admin_snapshot()
        for call in manager._run.await_args_list:
            self.assertIn(f"label=gaoji.app.instance={INSTANCE}", call.args)
            self.assertIn("label=gaoji.app.sandbox=true", call.args)
            self.assertNotIn("label=qqbot.sandbox=true", call.args)

    async def test_owned_lookup_checks_instance_owner_and_id(self):
        manager = driver()
        labels = container_labels(manager)
        for invalid in ({**labels, "gaoji.app.instance": "c" * 32},
                        {**labels, "qqbot.id": "s000000"}, {**labels, "qqbot.owner": "other"},
                        {**labels, "gaoji.app.sandbox": "false"}):
            manager._run = AsyncMock(return_value=result([invalid, True]))
            with self.subTest(labels=invalid), self.assertRaises(SandboxError):
                await manager._owned_container("owner", ID)
        manager._run = AsyncMock(return_value=result([labels, True]))
        self.assertEqual(await manager._owned_container("owner", ID), manager._container_name(ID))

    async def test_create_uses_instance_workspace_and_fixed_image(self):
        manager = driver(memory_mib=1024)
        manager.list = AsyncMock(return_value=[])
        manager._list_by_label = AsyncMock(return_value=[])
        manager._ensure_nix_volume = AsyncMock()
        manager._prepare_workspace_volume = AsyncMock()
        manager._run = AsyncMock(side_effect=lambda *args, **kw:
            SandboxResult("" if args[1] == "ps" else "created", "", 0))
        created = await manager.create("owner", "python")
        command = manager._docker_command(manager._run.await_args.args)
        self.assertIn(f"gaoji.app.instance={INSTANCE}", command)
        self.assertIn("gaoji.app.sandbox=true", command)
        self.assertNotIn("qqbot.sandbox=true", command)
        self.assertIn("--pull=never", command)
        self.assertIn("--read-only", command)
        self.assertEqual(command[command.index("--memory") + 1], "1g")
        self.assertEqual(command[command.index("--user") + 1], "1000:1000")
        self.assertEqual(command[command.index("--name") + 1], manager._container_name(created["sandbox_id"]))
        manager._prepare_workspace_volume.assert_awaited_once_with(
            IMAGE, manager._workspace_volume_name(created["sandbox_id"]), owner="owner", sandbox_id=created["sandbox_id"])

    async def test_concurrent_creates_cannot_exceed_total_limit(self):
        manager = driver(max_total=1)
        running = []
        manager.list = AsyncMock(return_value=[])

        async def listing(_label):
            await asyncio.sleep(0)
            return list(running)

        async def run(*_args, **_kwargs):
            if _args[1] == "ps":
                return SandboxResult("", "", 0)
            await asyncio.sleep(0)
            running.append({"status": "Up 1 second"})
            return SandboxResult("created", "", 0)

        manager._list_by_label = listing
        manager._ensure_nix_volume = AsyncMock()
        manager._prepare_workspace_volume = AsyncMock()
        manager._run = run
        outcomes = await asyncio.gather(manager.create("one"), manager.create("two"), return_exceptions=True)
        self.assertEqual(sum(isinstance(item, dict) for item in outcomes), 1)
        self.assertEqual(sum(isinstance(item, SandboxError) for item in outcomes), 1)
        self.assertEqual(len(running), 1)

    async def test_default_shell_restart_uses_capacity_checks(self):
        manager = driver()
        manager.list = AsyncMock(return_value=[{"sandbox_id": ID, "purpose": "shell", "status": "Exited (0)"}])
        manager.start_owned = AsyncMock(side_effect=SandboxError("full"))
        with self.assertRaises(SandboxError):
            await manager.ensure_default("owner")
        manager.start_owned.assert_awaited_once_with("owner", ID)

    async def test_start_and_create_share_capacity_lock(self):
        manager = driver(max_total=1)
        manager._owned_container = AsyncMock(return_value=manager._container_name(ID))
        manager.list = AsyncMock(return_value=[])
        running = []

        async def listing(_label):
            await asyncio.sleep(0)
            return list(running)

        async def run(*_args, **_kwargs):
            if _args[1] == "ps":
                return SandboxResult("", "", 0)
            running.append({"status": "Up 1 second"})
            return SandboxResult("started", "", 0)

        manager._list_by_label = listing
        manager._run = run
        outcomes = await asyncio.gather(manager.start_owned("one", ID), manager.start_owned("two", "s000000"), return_exceptions=True)
        self.assertEqual(sum(isinstance(item, SandboxError) for item in outcomes), 1)
        self.assertEqual(len(running), 1)

    async def test_new_volume_is_labelled_and_rechecked(self):
        manager = driver()
        volume = manager._workspace_volume_name(ID)
        labels = manager._volume_labels("workspace", "owner", ID)
        manager._run = AsyncMock(side_effect=[SandboxResult("", "No such volume", 1),
                                              SandboxResult(volume, "", 0), result(labels)])
        await manager._create_volume(volume, kind="workspace", owner="owner", sandbox_id=ID)
        command = manager._run.await_args_list[1].args
        for key, value in labels.items():
            self.assertIn(f"{key}={value}", command)
        self.assertEqual(manager._run.await_count, 3)

    async def test_volume_name_collision_never_adopts_or_changes_foreign_data(self):
        manager = driver()
        labels = manager._volume_labels("workspace", "owner", ID)
        for changed in ({}, {**labels, "gaoji.app.instance": "c" * 32}, {**labels, "qqbot.owner": "other"}, labels):
            manager._run = AsyncMock(return_value=result(changed))
            with self.subTest(labels=changed), self.assertRaises(SandboxError):
                await manager._create_volume(manager._workspace_volume_name(ID), kind="workspace", owner="owner", sandbox_id=ID)
            self.assertEqual(manager._run.await_count, 1)

    async def test_nix_cache_may_only_reuse_own_labelled_volume(self):
        manager = driver()
        manager._run = AsyncMock(return_value=result(manager._volume_labels("nix-cache", "", "")))
        await manager._create_volume(manager.nix_cache_volume, kind="nix-cache")
        self.assertEqual(manager._run.await_count, 1)
        manager._run = AsyncMock(return_value=result({"gaoji.app.instance": "c" * 32, "gaoji.app.resource": "nix-cache"}))
        with self.assertRaises(SandboxError):
            await manager._create_volume(manager.nix_cache_volume, kind="nix-cache")

    async def test_new_volume_recheck_rejects_foreign_concurrent_create(self):
        manager = driver()
        manager._run = AsyncMock(side_effect=[SandboxResult("", "No such volume", 1),
                                              SandboxResult("created", "", 0), result({})])
        with self.assertRaises(SandboxError):
            await manager._create_volume(manager._workspace_volume_name(ID), kind="workspace", owner="owner", sandbox_id=ID)
        self.assertFalse(any("rm" in call.args for call in manager._run.await_args_list))

    async def test_orphan_cleanup_never_probes_legacy_or_other_instance_volumes(self):
        manager = driver()
        manager._run = AsyncMock(side_effect=[SandboxResult("", "No such object", 1),
                                              result(manager._volume_labels("workspace", "owner", ID)),
                                              SandboxResult("removed", "", 0)])
        await manager.reclaim_stopped("owner", ID, eligible=lambda: True)
        removed = manager._run.await_args_list[-1].args
        self.assertEqual(removed[-1], manager._workspace_volume_name(ID))
        self.assertFalse(any(any("kennethbot" in argument or argument == "gaoji-nix-v2" for argument in call.args)
                             for call in manager._run.await_args_list))

    async def test_cleanup_after_removal_checks_orphan_volume_owner(self):
        manager = driver()
        manager._run = AsyncMock(side_effect=[SandboxResult("", "No such object", 1),
                                              result(manager._volume_labels("workspace", "someone-else", ID))])
        with self.assertRaises(SandboxError):
            await manager.reclaim_stopped("owner", ID, eligible=lambda: True)
        self.assertFalse(any("rm" in call.args for call in manager._run.await_args_list))

    async def test_destroy_rejects_foreign_mount_before_removing_container(self):
        manager = driver()
        manager._owned_container = AsyncMock(return_value=manager._container_name(ID))
        manager._run = AsyncMock(return_value=result([{"Destination": "/workspace", "Type": "volume", "Name": f"gaoji-work-{ID}"}]))
        with self.assertRaises(SandboxError):
            await manager.destroy("owner", ID)
        self.assertEqual(manager._run.await_count, 1)

    async def test_destroy_checks_volume_labels_before_removing_container(self):
        manager = driver()
        manager._owned_container = AsyncMock(return_value=manager._container_name(ID))
        manager._run = AsyncMock(side_effect=[result([{"Destination": "/workspace", "Type": "volume", "Name": manager._workspace_volume_name(ID)}]), result({})])
        with self.assertRaises(SandboxError):
            await manager.destroy("owner", ID)
        self.assertFalse(any("rm" in call.args for call in manager._run.await_args_list))

    async def test_process_does_not_inherit_remote_docker_context(self):
        process = AsyncMock()
        process.returncode = 0
        manager = driver(docker_executable="/usr/bin/docker", docker_host="unix:///tmp/local-docker.sock")
        manager._communicate_bounded = AsyncMock(return_value=(b"", b""))
        with patch.dict(os.environ, {"DOCKER_HOST": "tcp://some-server:2375", "DOCKER_CONTEXT": "remote"}), \
                patch("src.sandbox_runtime.docker.asyncio.create_subprocess_exec", return_value=process) as spawn:
            await manager._run_bytes("docker", "ps", timeout=5)
        self.assertEqual(spawn.call_args.args[0], "/usr/bin/docker")
        self.assertNotIn("DOCKER_CONTEXT", spawn.call_args.kwargs["env"])
        self.assertEqual(spawn.call_args.kwargs["env"]["DOCKER_HOST"], "unix:///tmp/local-docker.sock")

    async def test_helper_timeout_reclaims_only_its_generated_name(self):
        manager = driver()
        manager._verify_network_policy = AsyncMock()
        manager._run_process = AsyncMock(side_effect=SandboxError("timeout"))
        manager._cleanup_helper = AsyncMock()
        with self.assertRaises(SandboxError):
            await manager._run_bytes("docker", "run", "--rm", IMAGE, "sleep", "infinity", timeout=1)
        command = manager._run_process.await_args.args[0]
        name = command[command.index("--name") + 1]
        self.assertTrue(name.startswith(f"gaoji-app-{INSTANCE}-helper-"))
        manager._cleanup_helper.assert_awaited_once_with(name)

    async def test_readonly_handoff_uses_only_owned_volume_and_readonly_nix_cache(self):
        manager = driver()
        manager._owned_container = AsyncMock(return_value=manager._container_name(ID))
        manager._check_volume = AsyncMock(return_value=True)
        manager._run = AsyncMock(return_value=SandboxResult("", "", 0))
        await manager.install_readonly_file("owner", ID, "upstream/abc/report.txt", b"report")
        command = manager._run.await_args.args
        self.assertEqual(command[:2], ("docker", "run"))
        self.assertIn("--read-only", command)
        self.assertNotIn("--privileged", command)
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertIn(f"type=volume,source={manager._workspace_volume_name(ID)},target=/workspace", command)
        self.assertIn(f"type=volume,source={manager.nix_cache_volume},target=/nix,readonly", command)
        self.assertFalse(any("docker.sock" in part for part in command))
        manager._check_volume.assert_awaited_once_with(manager._workspace_volume_name(ID), kind="workspace", owner="owner", sandbox_id=ID)

    async def test_helper_cancellation_also_reclaims_its_container(self):
        manager = driver()
        manager._verify_network_policy = AsyncMock()
        started = asyncio.Event()

        async def running(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        manager._run_process = running
        manager._cleanup_helper = AsyncMock()
        task = asyncio.create_task(manager._run_bytes("docker", "run", "--rm", IMAGE, timeout=60))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        manager._cleanup_helper.assert_awaited_once()

    async def test_helper_cleanup_cannot_remove_someone_elses_container(self):
        manager = driver()
        manager._run = AsyncMock(return_value=result({"gaoji.app.instance": "c" * 32, "gaoji.app.resource": "helper"}))
        with self.assertRaises(SandboxError):
            await manager._cleanup_helper(f"gaoji-app-{INSTANCE}-helper-001122")
        self.assertEqual(manager._run.await_count, 1)

    async def test_package_helpers_are_serialized(self):
        manager = driver()
        manager._verify_network_policy = AsyncMock()
        active = maximum = 0

        async def running(*args, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.005)
            active -= 1
            return b"", b"", 0

        manager._run_process = running
        manager._cleanup_helper = AsyncMock()
        await asyncio.gather(*(manager._run_bytes("docker", "run", "--rm", IMAGE, timeout=5) for _ in range(3)))
        self.assertEqual(maximum, 1)


class SandboxLocalProcessTests(unittest.IsolatedAsyncioTestCase):
    # A local Python process substitutes only for the CLI transport. No daemon,
    # user data, image or remote server participates in these pipe checks.
    async def test_bounded_binary_output_round_trip(self):
        manager = driver(docker_executable=sys.executable, max_file_bytes=4096)
        stdout, stderr, code = await manager._run_bytes("docker", "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.stderr.write('checked')",
            input_bytes=bytes(range(256)) * 4, timeout=5)
        self.assertEqual(stdout, bytes(range(256)) * 4)
        self.assertEqual(stderr, b"checked")
        self.assertEqual(code, 0)

    async def test_large_output_is_stopped_without_buffering_indefinitely(self):
        manager = driver(docker_executable=sys.executable, max_file_bytes=4096)
        with self.assertRaisesRegex(SandboxError, "传输上限"):
            await manager._run_bytes("docker", "-c", "import sys; sys.stdout.write('x' * 131072)", timeout=5)

    async def test_large_stderr_is_also_bounded(self):
        manager = driver(docker_executable=sys.executable, max_file_bytes=4096)
        with self.assertRaisesRegex(SandboxError, "传输上限"):
            await manager._run_bytes("docker", "-c", "import sys; sys.stderr.write('x' * 131072)", timeout=5)

    async def test_large_input_is_rejected_before_spawn(self):
        manager = driver(docker_executable=sys.executable, max_file_bytes=4096)
        with patch("src.sandbox_runtime.docker.asyncio.create_subprocess_exec") as spawn:
            with self.assertRaisesRegex(SandboxError, "输入文件"):
                await manager._run_bytes("docker", "-c", "print('not started')", input_bytes=b"x" * 4097, timeout=5)
        spawn.assert_not_called()

    async def test_timeout_reaps_cli_process(self):
        manager = driver(docker_executable=sys.executable, max_file_bytes=4096)
        with self.assertRaisesRegex(SandboxError, "已终止等待"):
            await manager._run_bytes("docker", "-c", "import time; time.sleep(60)", timeout=0.05)


if __name__ == "__main__":
    unittest.main()
