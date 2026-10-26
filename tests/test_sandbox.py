from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from src.sandbox_runtime.docker import (
    DockerSandboxManager,
    SandboxError,
    SandboxResult,
)


class DockerSandboxManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = DockerSandboxManager()

    def test_workspace_path_rejects_escape(self) -> None:
        with self.assertRaises(SandboxError):
            self.manager._workspace_path("../secret")
        with self.assertRaises(SandboxError):
            self.manager._workspace_path("/etc/passwd")

    def test_new_docker_connection_error_is_user_friendly(self) -> None:
        detail = (
            "failed to connect to the docker API at unix:///tmp/docker.sock; "
            "check if the daemon is running"
        )
        self.assertEqual(
            self.manager._docker_error(detail),
            "Docker 服务没有启动。请先打开 Docker Desktop。",
        )

    def test_missing_local_image_reports_deployment_failure(self) -> None:
        error = self.manager._docker_error(
            "Error response from daemon: No such image: gaoji-sandbox:latest"
        )
        self.assertIn("未在本机加载或已被清理", error)
        self.assertNotIn("docker login", error)

    def test_long_failure_keeps_the_error_after_progress_logs(self) -> None:
        detail = "copying dependency...\n" * 1000 + "error: final failure reason"
        self.assertTrue(self.manager._docker_error(detail).endswith("error: final failure reason"))


class DockerSandboxCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_failure_retains_bounded_stderr_tail(self) -> None:
        manager = DockerSandboxManager(max_output_chars=1000)
        manager._run_bytes = AsyncMock(return_value=(b"", b"progress\n" * 1000 + b"error: final failure", 1))
        result = await manager._run("docker", "inspect", "fixture", timeout=5)
        self.assertTrue(result.stderr.endswith("error: final failure"))
        self.assertLess(len(result.stderr), 1100)

    async def test_create_always_applies_memory_cpu_and_process_limits(self) -> None:
        manager = DockerSandboxManager()
        manager.list = AsyncMock(return_value=[])  # type: ignore[method-assign]
        manager._list_by_label = AsyncMock(  # type: ignore[method-assign]
            return_value=[]
        )
        manager._run = AsyncMock(  # type: ignore[method-assign]
            return_value=SandboxResult("container-id\n", "", 0)
        )

        await manager.create("owner", "python")

        command = manager._docker_command(manager._run.await_args.args)
        self.assertEqual(command[command.index("--memory") + 1], "8g")
        self.assertEqual(command[command.index("--memory-swap") + 1], "8g")
        self.assertEqual(command[command.index("--cpus") + 1], "2")
        self.assertEqual(command[command.index("--pids-limit") + 1], "256")
        self.assertEqual(command[command.index("--network") + 1], "gaoji-sandbox")
        self.assertNotIn("--pull=never", command)
        self.assertIn("qqbot.owner_ref=owner", command)
        self.assertIn("qqbot.purpose=task", command)

    async def test_default_shell_reuses_the_group_sandbox(self) -> None:
        manager = DockerSandboxManager(image="gaoji-sandbox:latest")
        manager.list = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                {
                    "sandbox_id": "sabc123",
                    "runtime": "debian",
                    "purpose": "shell",
                    "status": "Up 2 minutes",
                }
            ]
        )
        manager.create = AsyncMock()  # type: ignore[method-assign]

        selected = await manager.ensure_default("shell:group:1")

        self.assertEqual(selected["sandbox_id"], "sabc123")
        manager.create.assert_not_awaited()

    async def test_configured_advanced_image_runs_as_sandbox_user(self) -> None:
        manager = DockerSandboxManager(image="gaoji-sandbox:latest")
        manager.list = AsyncMock(return_value=[])  # type: ignore[method-assign]
        manager._list_by_label = AsyncMock(  # type: ignore[method-assign]
            return_value=[]
        )
        manager._run = AsyncMock(  # type: ignore[method-assign]
            return_value=SandboxResult("container-id\n", "", 0)
        )

        created = await manager.create("owner", "python")

        command = manager._run.await_args.args
        self.assertIn("gaoji-sandbox:latest", command)
        self.assertEqual(command[command.index("--user") + 1], "1000:1000")
        self.assertIn("--read-only", command)
        self.assertIn(
            "type=volume,source=gaoji-nix-v2,target=/nix,readonly",
            command,
        )
        self.assertTrue(
            any(
                str(item).startswith("type=volume,source=gaoji-work-s")
                and str(item).endswith(",target=/workspace")
                for item in command
            )
        )
        self.assertEqual(created["toolset"], "advanced")
        calls = [call.args for call in manager._run.await_args_list]
        for call in calls:
            if call[:2] == ("docker", "run"):
                self.assertIn("--pull=never", call)
        self.assertIn(
            ("docker", "volume", "create", "gaoji-nix-v2"),
            calls,
        )
        self.assertTrue(
            any(
                call[:3] == ("docker", "volume", "create")
                and str(call[3]).startswith("gaoji-work-s")
                for call in calls
            )
        )
        workspace_setup = next(
            call
            for call in calls
            if call[:2] == ("docker", "run")
            and call[-1].endswith("chown 1000:1000 /workspace")
        )
        self.assertEqual(
            workspace_setup[-1],
            "chmod 700 /workspace && chown 1000:1000 /workspace",
        )

    async def test_nix_cache_initialization_allows_slow_first_copy(self) -> None:
        manager = DockerSandboxManager(image="gaoji-sandbox:latest")
        manager._run = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                SandboxResult("gaoji-nix-v2\n", "", 0),
                SandboxResult("", "", 0),
            ]
        )

        await manager._ensure_nix_volume("gaoji-sandbox:latest")

        initialization = manager._run.await_args_list[1]
        self.assertEqual(initialization.kwargs["timeout"], 300)
        self.assertEqual(initialization.args[-1], "gaoji-cache-seed")
        self.assertIn("type=volume,source=gaoji-nix-v2,target=/cache/nix", initialization.args)
        self.assertFalse(any("rm" in call.args for call in manager._run.await_args_list))
        self.assertTrue(manager._nix_volume_ready)

    async def test_missing_image_does_not_mark_cache_ready(self) -> None:
        manager = DockerSandboxManager(image="gaoji-sandbox:latest")
        manager._run = AsyncMock(side_effect=[
            SandboxResult("gaoji-nix-v2\n", "", 0),
            SandboxResult("", "No such image: gaoji-sandbox:latest", 125),
        ])
        with self.assertRaisesRegex(SandboxError, "未在本机加载或已被清理"):
            await manager._ensure_nix_volume(manager.image)
        self.assertFalse(manager._nix_volume_ready)
        self.assertIn("--pull=never", manager._run.await_args.args)

    async def test_destroy_removes_the_owned_workspace_volume(self) -> None:
        for prefix in ("gaoji-work", "kennethbot-work"):
            with self.subTest(prefix=prefix):
                manager = DockerSandboxManager(image="gaoji-sandbox:latest")
                manager._owned_container = AsyncMock(return_value="qqbot-sabc123")  # type: ignore[method-assign]
                volume = f"{prefix}-sabc123"
                mounts = json.dumps([{"Type": "volume", "Destination": "/workspace", "Name": volume}])
                manager._run = AsyncMock(side_effect=[  # type: ignore[method-assign]
                    SandboxResult(mounts, "", 0),
                    SandboxResult("", "", 0),
                    SandboxResult("", "", 0),
                ])
                await manager.destroy("owner", "sabc123")
                self.assertEqual(
                    manager._run.await_args_list[2].args,
                    ("docker", "volume", "rm", "-f", volume),
                )

    async def test_destroy_refuses_a_foreign_workspace_volume(self) -> None:
        manager = DockerSandboxManager(image="gaoji-sandbox:latest")
        manager._owned_container = AsyncMock(return_value="qqbot-sabc123")  # type: ignore[method-assign]
        mounts = json.dumps([{"Type": "volume", "Destination": "/workspace", "Name": "unrelated-data"}])
        manager._run = AsyncMock(return_value=SandboxResult(mounts, "", 0))  # type: ignore[method-assign]
        with self.assertRaises(SandboxError):
            await manager.destroy("owner", "sabc123")
        self.assertEqual(manager._run.await_count, 1)

    async def test_automatic_reclaim_never_forces_a_running_or_foreign_container(self):
        manager = DockerSandboxManager(image="gaoji-sandbox:latest")
        labels = {"qqbot.sandbox": "true", "qqbot.id": "sabc123",
                  "qqbot.owner": manager._owner_hash("owner"), "qqbot.purpose": "task"}
        for running, changes in ((True, {}), (False, {"qqbot.purpose": "shell"}),
                                 (False, {"qqbot.owner": "foreign"})):
            with self.subTest(running=running, changes=changes):
                manager._run = AsyncMock(return_value=SandboxResult(json.dumps([{**labels, **changes}, running, [], "1970-01-01T00:00:10Z"]), "", 0))
                with self.assertRaises(SandboxError):
                    await manager.reclaim_stopped("owner", "sabc123", eligible=lambda: True)
                self.assertEqual(manager._run.await_count, 1)

    async def test_automatic_reclaim_removes_only_owned_workspace_without_force(self):
        manager = DockerSandboxManager(image="gaoji-sandbox:latest")
        labels = {"qqbot.sandbox": "true", "qqbot.id": "sabc123",
                  "qqbot.owner": manager._owner_hash("owner"), "qqbot.purpose": "task"}
        mounts = [{"Type": "volume", "Destination": "/workspace", "Name": "gaoji-work-sabc123"},
                  {"Type": "volume", "Destination": "/nix", "Name": "gaoji-nix-v2"}]
        manager._run = AsyncMock(side_effect=[SandboxResult(json.dumps([labels, False, mounts, "1970-01-01T00:00:10Z"]), "", 0),
                                              SandboxResult("", "", 0), SandboxResult("", "", 0)])
        await manager.reclaim_stopped("owner", "sabc123", eligible=lambda: True)
        self.assertEqual(manager._run.await_args_list[1].args, ("docker", "rm", "qqbot-sabc123"))
        self.assertEqual(manager._run.await_args_list[2].args, ("docker", "volume", "rm", "-f", "gaoji-work-sabc123"))

    async def test_automatic_reclaim_rechecks_task_before_removal(self):
        manager = DockerSandboxManager()
        labels = {"qqbot.sandbox": "true", "qqbot.id": "sabc123",
                  "qqbot.owner": manager._owner_hash("owner"), "qqbot.purpose": "task"}
        manager._run = AsyncMock(return_value=SandboxResult(json.dumps([labels, False, [], "1970-01-01T00:00:10Z"]), "", 0))
        with self.assertRaises(SandboxError):
            await manager.reclaim_stopped("owner", "sabc123", eligible=lambda: False)
        self.assertEqual(manager._run.await_count, 1)

    async def test_automatic_reclaim_protects_a_workspace_used_again_after_delivery(self):
        manager = DockerSandboxManager()
        labels = {"qqbot.sandbox": "true", "qqbot.id": "sabc123",
                  "qqbot.owner": manager._owner_hash("owner"), "qqbot.purpose": "task"}
        manager._run = AsyncMock(return_value=SandboxResult(json.dumps([labels, False, [], "1970-01-01T00:02:00Z"]), "", 0))
        with self.assertRaises(SandboxError):
            await manager.reclaim_stopped("owner", "sabc123", eligible=lambda: True, not_used_since=100)
        self.assertEqual(manager._run.await_count, 1)

    async def test_automatic_reclaim_retries_volume_removal_after_container_was_removed(self):
        manager = DockerSandboxManager()
        manager._run = AsyncMock(side_effect=[SandboxResult("", "Error: No such object", 1),
                                              SandboxResult("", "", 0), SandboxResult("", "", 0)])
        await manager.reclaim_stopped("owner", "sabc123", eligible=lambda: True)
        self.assertTrue(all(c.args[:3] == ("docker", "volume", "rm") for c in manager._run.await_args_list[1:]))

    async def test_stop_preserves_the_owned_workspace_volume(self) -> None:
        manager = DockerSandboxManager(image="gaoji-sandbox:latest")
        manager._owned_container = AsyncMock(return_value="qqbot-sabc123")  # type: ignore[method-assign]
        manager.list = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                {
                    "sandbox_id": "sabc123",
                    "runtime": "python",
                    "purpose": "task",
                    "status": "Up 2 minutes",
                }
            ]
        )
        manager._run = AsyncMock(  # type: ignore[method-assign]
            return_value=SandboxResult("qqbot-sabc123\n", "", 0)
        )

        await manager.stop_owned("owner", "sabc123")

        manager._run.assert_awaited_once_with(
            "docker",
            "stop",
            "--time",
            "5",
            "qqbot-sabc123",
            timeout=15,
        )

    async def test_exec_prepares_and_wraps_requested_nix_packages(self) -> None:
        manager = DockerSandboxManager(image="gaoji-sandbox:latest")
        manager._owned_container = AsyncMock(return_value="qqbot-sabc123")  # type: ignore[method-assign]
        manager._prepare_packages = AsyncMock(  # type: ignore[method-assign]
            return_value=("/nix/store/abc-ffmpeg",)
        )
        manager._run_bytes = AsyncMock(return_value=(b"ok", b"", 0))  # type: ignore[method-assign]
        manager._run = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                SandboxResult("gaoji-sandbox\n", "", 0),
                SandboxResult("", "", 0),
                SandboxResult("", "", 0),
                SandboxResult("", "", 0),
                SandboxResult("", "", 0),
            ]
        )

        await manager.exec(
            "owner",
            "sabc123",
            "ffmpeg -version",
            30,
            packages=["ffmpeg"],
        )

        manager._prepare_packages.assert_awaited_once_with(
            ["ffmpeg"], timeout_seconds=120
        )
        wrapped = manager._run_bytes.await_args.args[-1]
        self.assertIn("/nix/store/abc-ffmpeg/bin", wrapped)
        self.assertIn("ffmpeg -version", wrapped)

    async def test_package_attributes_are_structured_and_reject_injection(self) -> None:
        manager = DockerSandboxManager(image="gaoji-sandbox:latest")
        with self.assertRaises(SandboxError):
            await manager._prepare_packages(["ffmpeg;id"], timeout_seconds=30)
        expression = manager._package_expression(
            ("ffmpeg", "python3Packages.pandas")
        )
        self.assertIn('builtins.getAttr "ffmpeg" pkgs', expression)
        self.assertIn('builtins.getAttr "pandas" ps', expression)
        self.assertIn("python3.withPackages", expression)

    async def test_zero_file_limit_allows_large_transfers(self) -> None:
        manager = DockerSandboxManager(max_file_bytes=0)
        manager._owned_container = AsyncMock(  # type: ignore[method-assign]
            return_value="qqbot-sabc123"
        )
        manager._run = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                SandboxResult("", "", 0),
                SandboxResult("", "", 0),
            ]
        )

        self.assertIsNone(manager._file_limit(None))
        self.assertEqual(manager._file_limit(64 * 1024), 64 * 1024)
        content = b"x" * (600 * 1024)
        self.assertEqual(
            await manager.write_file("owner", "sabc123", "large.bin", content),
            len(content),
        )

    async def test_export_directory_returns_zip_bytes(self) -> None:
        manager = DockerSandboxManager()
        manager._owned_container = AsyncMock(return_value="qqbot-sabc123")  # type: ignore[method-assign]
        manager._run = AsyncMock(return_value=SandboxResult("directory", "", 0))  # type: ignore[method-assign]
        manager._run_bytes = AsyncMock(return_value=(b"PK-archive", b"", 0))  # type: ignore[method-assign]

        content, directory = await manager.export_artifact("owner", "sabc123", "project")

        self.assertTrue(directory)
        self.assertEqual(content, b"PK-archive")
        self.assertEqual(manager._run_bytes.await_args.args[-1], "/workspace/project")

    async def test_exec_returns_observed_manifest(self) -> None:
        manager = DockerSandboxManager()
        manager._owned_container = AsyncMock(return_value="qqbot-sabc123")  # type: ignore[method-assign]
        manager._run_bytes = AsyncMock(return_value=(b"hello", b"warn", 0))  # type: ignore[method-assign]
        manager._run = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                SandboxResult("gaoji-sandbox\n", "", 0),
                SandboxResult("", "", 0),
                SandboxResult("/workspace/main.py\n", "", 0),
                SandboxResult("C /workspace/main.py\n", "", 0),
                SandboxResult("", "", 0),
            ]
        )

        result = await manager.exec("owner", "sabc123", "python main.py")

        self.assertEqual(result.stdout, "hello")
        self.assertEqual(result.stderr, "warn")
        self.assertIsNotNone(result.manifest)
        self.assertEqual(result.manifest.network_mode, "gaoji-sandbox")  # type: ignore[union-attr]
        self.assertEqual(
            result.manifest.changed_workspace_paths,  # type: ignore[union-attr]
            ("main.py",),
        )
        self.assertEqual(result.manifest.stdout_bytes, 5)  # type: ignore[union-attr]
        self.assertEqual(len(result.manifest.stdout_sha256), 64)  # type: ignore[union-attr]
        self.assertEqual(
            manager._last_execs["sabc123"].command,
            "python main.py",
        )
        self.assertEqual(manager._last_execs["sabc123"].status, "completed")

    async def test_admin_snapshot_reports_resources_and_workspace(self) -> None:
        manager = DockerSandboxManager()
        manager._run = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                SandboxResult(
                    (
                        "qqbot-sabc123|sabc123|python|"
                        "group:1:user:2|ownerhash|task|Up 2 minutes\n"
                    ),
                    "",
                    0,
                ),
                SandboxResult(
                    "qqbot-sabc123|1.25%|64MiB / 8GiB|0.78%\n",
                    "",
                    0,
                ),
                SandboxResult(
                    "__TOTAL__|123\n__COUNT__|2\na.txt|5\ndir/b.bin|118\n",
                    "",
                    0,
                ),
            ]
        )

        snapshot = await manager.admin_snapshot()

        self.assertEqual(snapshot["active_commands"], 0)
        item = snapshot["items"][0]  # type: ignore[index]
        self.assertEqual(item["owner"], "group:1:user:2")
        self.assertEqual(item["purpose"], "task")
        self.assertEqual(item["memory_usage"], "64MiB / 8GiB")
        self.assertEqual(item["workspace_file_count"], 2)
        self.assertEqual(item["workspace_files"][1]["path"], "dir/b.bin")

    async def test_cancelling_exec_kills_child_process(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.killed = False
                self.communicate_calls = 0
                self.returncode = -9

            async def communicate(self, _input=None):
                self.communicate_calls += 1
                if self.communicate_calls == 1:
                    await asyncio.Event().wait()
                return b"", b""

            def kill(self) -> None:
                self.killed = True

        process = FakeProcess()
        manager = DockerSandboxManager()
        with patch(
            "src.sandbox_runtime.docker.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            task = asyncio.create_task(
                manager._run_bytes("docker", "ps", timeout=30)
            )
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(process.killed)
        self.assertEqual(process.communicate_calls, 2)


if __name__ == "__main__":
    unittest.main()
