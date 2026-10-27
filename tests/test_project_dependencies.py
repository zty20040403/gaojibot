"""Durable private RPC with explicit Docker/kernel fixtures, no live installers."""

import copy
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from src.sandbox_runtime.docker import SandboxError
from tests import test_project_file_transfer as fixture
from tests.test_project_workspaces import DockerFixture


class DependencyDockerFixture(DockerFixture):
    def __init__(self, manager, volumes):
        super().__init__(manager, volumes)
        self.automatic = True
        self.exit_code, self.oom, self.changed = 0, False, False
        self.actions = []

    def dependency(self):
        return next(
            row
            for row in self.containers.values()
            if "gaoji.app.rebuild-fence" in row["Config"]["Labels"]
        )

    def complete(self, row=None):
        row = row or self.dependency()
        root = Path(
            next(m["Source"] for m in row["Mounts"] if m["Destination"] == "/workspace")
        )
        (root / "node_modules").mkdir(exist_ok=True)
        (root / "node_modules/fixture").write_bytes(b"reconstructed")
        if self.changed:
            (root / "source.js").write_bytes(b"unwanted change")
        row["State"].update(
            Status="exited",
            Running=False,
            ExitCode=self.exit_code,
            OOMKilled=self.oom,
            FinishedAt="2026-10-27T01:00:00Z",
        )

    async def run(self, *arguments, **options):
        self.actions.append(arguments)
        if arguments[1] == "create":
            result = await super().run("docker", "run", "-d", *arguments[2:], **options)
            row = self.dependency()
            row["Config"]["WorkingDir"] = "/workspace"
            row["HostConfig"].update(RestartPolicy={"Name": "no"}, AutoRemove=False)
            row["State"] = dict(
                Status="created",
                Running=False,
                ExitCode=0,
                OOMKilled=False,
                StartedAt="0001-01-01T00:00:00Z",
                FinishedAt="0001-01-01T00:00:00Z",
            )
        elif arguments[1] in {"start", "stop"}:
            row = next(r for r in self.containers.values() if r["Id"] == arguments[-1])
            result = await super().run(*arguments, **options)
            if "gaoji.app.rebuild-fence" in row["Config"]["Labels"]:
                if arguments[1] == "start":
                    row["State"].update(
                        Status="running", StartedAt="2026-10-27T00:00:00Z"
                    )
                    if self.automatic:
                        self.complete(row)
                else:
                    row["State"].update(
                        Status="exited", ExitCode=143, FinishedAt="2026-10-27T01:00:00Z"
                    )
        else:
            result = await super().run(*arguments, **options)
        return result


class DependencyHelperTests(unittest.IsolatedAsyncioTestCase):
    start_runner = fixture.ProjectFileTransferTests.start_runner
    stop_runner = fixture.ProjectFileTransferTests.stop_runner
    operate = fixture.ProjectFileTransferTests.operate
    journal = fixture.ProjectFileTransferTests.journal
    reserve = fixture.ProjectFileTransferTests.reserve
    call = fixture.ProjectFileTransferTests.call

    async def asyncSetUp(self):
        await fixture.ProjectFileTransferTests.asyncSetUp(self)
        self.docker = DependencyDockerFixture(self.driver, self.volumes)
        self.driver._run = self.docker.run
        self.driver.list = self.docker.listing
        self.driver._list_by_label = self.docker.list_by_label
        self.driver._prepare_packages = AsyncMock(return_value=())
        self.host.in_use = self.docker.in_use
        await self.reserve()
        (self.path / "package.json").write_bytes(b'{"name":"fixture"}')
        (self.path / "package-lock.json").write_bytes(b'{"lockfileVersion":3}')
        (self.path / "source.js").write_bytes(b"source")
        result, _ = await self.call("snapshot", rebuild_command="npm ci")
        self.digest = result["manifest_sha256"]

    async def rebuild(self, action="run", **options):
        args = dict(
            generation=1, fence=5, reservation="a" * 32, manifest_sha256=self.digest
        )
        args.update(options)
        return await self.client.project_rebuild(action, "scope", 1, **args)

    def count(self, action):
        return sum(c[1] == action for c in self.docker.actions)

    async def test_success_is_exit_plus_files_check_and_container_removal(self):
        self.driver.max_per_owner = 1
        result = await self.rebuild()
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["pending"])
        self.assertEqual(self.count("create"), 1)
        self.assertEqual(self.count("start"), 1)
        self.assertFalse(self.docker.containers)
        self.assertEqual(
            (self.path / "node_modules/fixture").read_bytes(), b"reconstructed"
        )
        self.assertEqual((await self.rebuild("status"))["state"], "completed")

    async def test_lost_create_ack_reuses_never_started_container(self):
        original = self.driver._run

        async def lost(*args, **kwargs):
            result = await original(*args, **kwargs)
            if args[1] == "create":
                raise SandboxError("fixture lost create acknowledgement")
            return result

        with patch.object(self.driver, "_run", side_effect=lost):
            with self.assertRaises(SandboxError):
                await self.rebuild()
        self.assertEqual(self.count("start"), 0)
        await self.stop_runner()
        await self.start_runner()
        self.assertEqual((await self.rebuild())["state"], "completed")
        self.assertEqual(self.count("create"), 1)
        self.assertEqual(self.count("start"), 1)

    async def test_lost_start_ack_does_not_execute_again(self):
        original = self.driver._run

        async def lost(*args, **kwargs):
            result = await original(*args, **kwargs)
            if args[1] == "start":
                raise SandboxError("fixture lost start acknowledgement")
            return result

        with patch.object(self.driver, "_run", side_effect=lost):
            with self.assertRaises(SandboxError):
                await self.rebuild()
        await self.stop_runner()
        await self.start_runner()
        self.assertEqual((await self.rebuild())["state"], "completed")
        self.assertEqual(self.count("start"), 1)

    async def test_running_container_survives_runner_restart_and_is_not_restarted(self):
        self.docker.automatic = False
        first = await self.rebuild()
        self.assertEqual(first["state"], "running")
        identity = self.docker.dependency()["Id"]
        await self.stop_runner()
        await self.start_runner()
        second = await self.rebuild()
        self.assertEqual(first["deadline"], second["deadline"])
        self.assertEqual(self.docker.dependency()["Id"], identity)
        self.docker.complete()
        self.assertEqual((await self.rebuild())["state"], "completed")
        self.assertEqual(self.count("start"), 1)

    async def test_missing_or_replaced_running_container_cannot_be_replayed(self):
        self.docker.automatic = False
        await self.rebuild()
        name, row = next(iter(self.docker.containers.items()))
        original = copy.deepcopy(row)
        row["Id"] = "f" * 64
        with self.assertRaises(SandboxError):
            await self.rebuild()
        self.docker.containers[name] = original
        del self.docker.containers[name]
        with self.assertRaises(SandboxError):
            await self.rebuild()
        self.assertEqual(self.count("create"), 1)
        self.assertEqual(self.count("start"), 1)

    async def test_failed_installation_is_persistent_without_retrying_command(self):
        self.docker.exit_code = 42
        first = await self.rebuild()
        self.assertEqual(first["state"], "failed")
        self.assertTrue(first["pending"])
        self.assertEqual(first["exit_code"], 42)
        self.assertFalse(self.docker.containers)
        self.assertEqual((await self.rebuild())["state"], "failed")
        self.assertEqual(self.count("start"), 1)
        self.assertEqual((self.path / "source.js").read_bytes(), b"source")

    async def test_oom_cannot_be_success_even_with_zero_exit(self):
        self.docker.oom = True
        result = await self.rebuild()
        self.assertEqual(result["state"], "failed")
        self.assertIn("memory", result["error"])

    async def test_zero_exit_cannot_hide_retained_file_changes(self):
        self.docker.changed = True
        result = await self.rebuild()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual((self.path / "source.js").read_bytes(), b"unwanted change")
        self.assertFalse(self.docker.containers)

    async def test_changed_lock_prevents_any_dispatch(self):
        (self.path / "package-lock.json").write_bytes(b'{"lockfileVersion":2}')
        with self.assertRaises(SandboxError):
            await self.rebuild()
        self.assertEqual(self.count("create"), 0)
        self.assertEqual(self.journal()["fence"], 4)

    async def test_status_is_uncached_and_detects_post_completion_changes(self):
        await self.rebuild(request_id="b" * 32)
        (self.path / "source.js").write_bytes(b"changed")
        cached = await self.rebuild(request_id="b" * 32)
        self.assertEqual(cached["state"], "completed")
        with self.assertRaises(SandboxError):
            await self.rebuild("status")

    async def test_deadline_does_not_extend_and_stops_only_owned_job(self):
        self.docker.automatic = False
        first = await self.rebuild()
        with patch(
            "src.sandbox_runtime.project_dependencies.time.time",
            return_value=first["deadline"] + 1,
        ):
            result = await self.rebuild()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["deadline"], first["deadline"])
        self.assertIn("timed out", result["error"])
        self.assertEqual(self.count("stop"), 1)
        self.assertFalse(self.docker.containers)

    async def test_ordinary_exec_and_release_cannot_bypass_pending_rebuild(self):
        self.docker.automatic = False
        await self.rebuild()
        sid = self.docker.dependency()["Config"]["Labels"]["qqbot.id"]
        with self.assertRaises(SandboxError):
            await self.client.exec("scope", sid, "echo forbidden")
        with self.assertRaises(SandboxError):
            await self.client.project_workspace(
                "release", "scope", 1, generation=1, fence=6, reservation="a" * 32
            )
        with self.assertRaises(SandboxError):
            await self.rebuild(reservation="c" * 32)
        self.assertEqual(self.count("start"), 1)

    async def test_removed_completion_receipt_recovers_after_lost_rm_ack(self):
        original = self.driver._run

        async def lost(*args, **kwargs):
            result = await original(*args, **kwargs)
            if args[1] == "rm":
                raise SandboxError("fixture lost rm acknowledgement")
            return result

        with patch.object(self.driver, "_run", side_effect=lost):
            with self.assertRaises(SandboxError):
                await self.rebuild()
        self.assertFalse(self.docker.containers)
        self.assertEqual((await self.rebuild())["state"], "completed")
        self.assertEqual(self.count("start"), 1)

    async def test_partial_completion_commit_cannot_be_reported_as_success(self):
        with patch.object(
            self.volumes, "_finish", side_effect=SandboxError("fixture lost commit")
        ):
            with self.assertRaises(SandboxError):
                await self.rebuild()
        self.assertTrue(self.journal()["pending"])
        with self.assertRaises(SandboxError):
            await self.rebuild("status")
        self.assertEqual((await self.rebuild())["state"], "completed")
        self.assertFalse(self.journal()["pending"])
        self.assertEqual(self.count("start"), 1)

    async def test_unpinned_image_is_rejected_before_creating_intent(self):
        for image in (None, "gaoji-sandbox:latest"):
            with patch.object(self.driver, "image", image):
                with self.assertRaises(SandboxError):
                    await self.rebuild()
        self.assertEqual(self.count("create"), 0)
        self.assertEqual(self.journal()["fence"], 4)
