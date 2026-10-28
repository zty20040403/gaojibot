"""Real private journals with simulated Docker effects, not native quota proof."""

import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from src.sandbox_runtime.docker import DockerSandboxManager, SandboxError, SandboxResult
from src.sandbox_runtime.project_browser_oci import OciProjectBrowser, worker_command
from src.sandbox_runtime.project_browser_worker import ProjectBrowserWorker
from src.sandbox_runtime.project_desktop_workspaces import DesktopProjectWorkspaces
from src.sandbox_runtime.project_directory import DirectoryProjectVolumes
from src.sandbox_runtime.project_volumes import MIB, ProjectVolumes
from src.sandbox_runtime.project_workspaces import ProjectWorkspaces
from src.sandbox_runtime.wire import ProtocolError
from tests.test_project_browser_host import FixturePipe
from tests.test_project_desktop import DesktopDockerFixture, DesktopHostFixture
from tests.test_project_volumes import HostFixture
from tests.test_project_workspaces import DockerFixture, IMAGE
from tests.test_sandbox_runner import INSTANCE

PACKAGES = ("/nix/store/" + "a" * 32 + "-chromium", "/nix/store/" + "b" * 32 + "-python3-env")


class BrowserFixtureMixin:
    async def listing(self, owner=None):
        return [{"sandbox_id": row["Config"]["Labels"]["qqbot.id"],
                 "status": "Up 1 second" if row["State"]["Running"] else "Exited (0)"}
                for row in self.containers.values() if "qqbot.id" in row["Config"]["Labels"]
                and (owner is None or row["Config"]["Labels"]["qqbot.owner"] == self.manager._owner_hash(owner))]

    async def run(self, *args, **kwargs):
        action = args[1]
        if action == "create":
            # Reuse fixture mount decoding, then report the unstarted sidecar.
            result = await super().run("docker", "run", "-d", *args[2:], **kwargs)
            row = self.containers[args[args.index("--name") + 1]]
            row["Name"] = "/" + args[args.index("--name") + 1]
            row["Config"].update(WorkingDir="/workspace", Tty=False, OpenStdin=True)
            row["HostConfig"].update(Init=True, Memory=int(args[args.index("--memory") + 1]),
                MemorySwap=int(args[args.index("--memory-swap") + 1]),
                NanoCpus=int(args[args.index("--cpus") + 1]) * 10**9,
                PidsLimit=int(args[args.index("--pids-limit") + 1]),
                Sysctls={"net.ipv6.conf.all.disable_ipv6": "1"},
                RestartPolicy={"Name": "no"}, LogConfig={"Type": "none"})
            row["State"].update(Running=False, Pid=0)
        elif action == "ps" and "label=gaoji.app.browser=true" in args:
            return SandboxResult("\n".join(
                row["Id"] + "|" + row["Config"]["Labels"]["qqbot.owner"]
                for row in self.containers.values() if row["Config"]["Labels"].get("gaoji.app.browser") == "true"
            ), "", 0)
        else:
            if action == "inspect":
                args = (*args[:-1], next((name for name, row in self.containers.items()
                                          if row["Id"] == args[-1]), args[-1]))
            result = await super().run(*args, **kwargs)
            if action == "stop":
                row = next(row for row in self.containers.values() if row["Id"] == args[-1])
                row["State"]["Pid"] = 0
        if action in self.after:
            raise self.after.pop(action)
        return result


class BrowserDockerFixture(BrowserFixtureMixin, DockerFixture):
    pass


class BrowserDesktopFixture(BrowserFixtureMixin, DesktopDockerFixture):
    pass


class BrowserOciTests(unittest.IsolatedAsyncioTestCase):
    desktop = False

    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory(prefix="gj-browser-oci-", dir="/tmp")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.host = DesktopHostFixture(self.root) if self.desktop else HostFixture()
        volume_type = DirectoryProjectVolumes if self.desktop else ProjectVolumes
        self.volumes = volume_type(self.root / "projects", INSTANCE, host=self.host)
        self.volumes.create("scope", 1, quota_bytes=64 * MIB, fence=1)
        self.volumes.mutate("scope", 1, "mount", fence=2)
        self.manager = DockerSandboxManager(namespace=INSTANCE, image=IMAGE, network="none")
        fixture = BrowserDesktopFixture if self.desktop else BrowserDockerFixture
        self.docker = fixture(self.manager, self.volumes)
        self.host.in_use = self.docker.in_use
        self.manager._run = self.docker.run
        async def run_bytes(*args, **kwargs):
            result = await self.docker.run(*args, **kwargs)
            return result.stdout.encode(), result.stderr.encode(), result.returncode
        self.manager._run_bytes = run_bytes
        self.manager.list = self.docker.listing
        self.manager._list_by_label = self.docker.list_by_label
        self.manager._ensure_nix_volume = AsyncMock()
        self.manager._prepare_packages = AsyncMock(return_value=PACKAGES)
        workspace_type = DesktopProjectWorkspaces if self.desktop else ProjectWorkspaces
        self.workspaces = workspace_type(self.manager, self.volumes)
        self.manager.project_workspaces = self.workspaces
        await self.workspaces.operate("start", "scope", 1, generation=1, fence=3)
        self.parent = next(iter(self.docker.containers.values()))
        self.browser = self.workspaces.browser
        self.identity = dict(instance_id=INSTANCE, owner="scope", project_id=1, generation=1, fence=3)
        self.engine = SimpleNamespace(bound_owner="scope", close=AsyncMock(),
                                      click=AsyncMock(return_value={"text": "clicked"}))
        self.channel = None
        self.browser._spawn = AsyncMock(side_effect=self.spawn)
        self.published = {}

    async def spawn(self, record, spec):
        current = self.browser._load(record)
        self.assertEqual(current["phase"], "starting")
        self.assertEqual(current["container_id"], spec["container_id"])
        self.worker = ProjectBrowserWorker(self.identity, self.engine)
        self.addAsyncCleanup(self.worker.close)
        self.channel = FixturePipe(self.worker)
        self.channel.eof = AsyncMock()
        row = self.docker.containers[spec["name"]]
        row["State"].update(Running=True, Pid=2000)
        return self.channel

    async def open(self):
        self.session = await self.browser.open("scope", 1, generation=1, fence=3)
        self.sid = self.session["session_id"]
        self.spec = self.browser._load(self.session)
        return self.session

    async def persist(self, sid, seq, reply):
        self.assertEqual(self.published.setdefault((sid, seq), deepcopy(reply)), reply)

    async def call(self, sequence=1, *, browser=None, persist=None):
        return await (browser or self.browser).execute(self.identity, self.sid, sequence,
            "browser_click", {"ref": "b1"}, persist=persist or self.persist)

    async def test_isolated_launch_effect_replay_and_drain_keep_parent_and_files(self):
        target = self.volumes.binding("scope", 1)
        (target / "keep.txt").write_text("retained")
        parent = deepcopy(self.parent)
        await self.open()
        row = self.docker.containers[self.spec["name"]]
        self.assertNotIn("gaoji.app.sandbox", row["Config"]["Labels"])
        self.assertEqual(row["HostConfig"]["Memory"], 1024 * MIB)
        self.assertEqual(row["HostConfig"]["LogConfig"]["Type"], "none")
        self.assertEqual(await self.manager._browser_capacity("scope"), (1, 1))
        self.assertEqual(await self.manager._browser_capacity("other"), (0, 1))
        self.assertEqual(await self.call(), await self.call())
        self.engine.click.assert_awaited_once()
        with self.assertRaises(SandboxError):
            await self.workspaces.operate("stop", "scope", 1, generation=1, fence=4)
        await self.browser.drain(self.identity, self.sid)
        self.assertTrue(self.volumes.browsers.maintenance_ready(self.identity))
        self.assertEqual(self.parent, parent)
        self.assertEqual((target / "keep.txt").read_text(), "retained")
        self.assertEqual(await self.manager._browser_capacity("scope"), (0, 0))
        self.assertEqual(self.browser._load(self.session)["phase"], "removed")

    async def test_lost_publication_replays_after_driver_restart_and_shutdown(self):
        await self.open()
        with self.assertRaises(OSError):
            await self.call(persist=AsyncMock(side_effect=OSError("db offline")))
        restarted = OciProjectBrowser(self.workspaces)
        await restarted.drain(self.identity, self.sid)
        self.assertFalse(self.volumes.browsers.maintenance_ready(self.identity))
        await self.call(browser=restarted)
        self.assertTrue(self.volumes.browsers.maintenance_ready(self.identity))
        self.engine.click.assert_awaited_once()
        self.assertTrue(self.parent["State"]["Running"])

    async def test_unknown_create_is_reconciled_not_restarted(self):
        self.docker.after["create"] = TimeoutError("reply lost")
        with self.assertRaises(TimeoutError):
            await self.open()
        record = self.volumes.browsers.current(self.identity)
        self.assertEqual(record["state"], "needs_attention")
        self.assertEqual(await self.manager._browser_capacity("scope"), (1, 1))
        self.browser._spawn.assert_not_awaited()
        restarted = OciProjectBrowser(self.workspaces)
        await restarted.drain(self.identity, record["session_id"], cancel_pending=True)
        self.assertEqual(len(self.docker.containers), 1)

    async def test_lost_remove_ack_can_be_reconciled_from_immutable_intent(self):
        await self.open()
        self.docker.after["rm"] = TimeoutError("reply lost")
        with self.assertRaises(TimeoutError):
            await self.browser.drain(self.identity, self.sid)
        self.assertFalse(self.volumes.browsers.maintenance_ready(self.identity))
        await OciProjectBrowser(self.workspaces).drain(self.identity, self.sid)
        self.assertTrue(self.volumes.browsers.maintenance_ready(self.identity))

    async def test_missing_unconfirmed_create_is_not_treated_as_closed(self):
        self.browser._spawn.side_effect = TimeoutError("start not observed")
        with self.assertRaises(TimeoutError):
            await self.open()
        record = self.volumes.browsers.current(self.identity)
        spec = self.browser._load(record)
        del self.docker.containers[spec["name"]]
        with self.assertRaisesRegex(SandboxError, "absence alone"):
            await self.browser.drain(self.identity, record["session_id"], cancel_pending=True)
        self.assertFalse(self.volumes.browsers.maintenance_ready(self.identity))

    async def test_changed_container_is_never_executed_or_destroyed(self):
        await self.open()
        row = self.docker.containers[self.spec["name"]]
        row["Config"]["Labels"]["gaoji.app.browser-session"] = "foreign"
        with self.assertRaises(SandboxError):
            await self.call()
        self.engine.click.assert_not_awaited()
        with self.assertRaises(SandboxError):
            await self.browser.drain(self.identity, self.sid, cancel_pending=True)
        self.assertTrue(row["State"]["Running"])

    async def test_new_effect_rechecks_parent_mount_and_process(self):
        await self.open()
        self.parent["Id"] = "d" * 64
        with self.assertRaisesRegex(SandboxError, "parent container"):
            await self.call()
        self.engine.click.assert_not_awaited()
        self.parent["Id"] = self.session["runtime"]["runtime_id"]
        await self.browser.drain(self.identity, self.sid, cancel_pending=True)
        self.assertFalse(self.volumes.browsers.maintenance_ready(self.identity))

    async def test_browser_reservation_counts_for_project_and_task_admission(self):
        await self.open()
        self.manager.max_per_owner = 2
        with self.assertRaises(SandboxError):
            await self.workspaces._capacity("scope")
        with self.assertRaises(SandboxError):
            await self.manager.create("scope")
        self.manager.max_total = 2
        with self.assertRaises(SandboxError):
            await self.manager.create("other")

    async def test_post_handshake_mount_failure_fences_session(self):
        original = self.spawn

        async def wrong_mount(*args):
            result = await original(*args)
            self.docker.identity_override = "1:2"
            return result

        self.browser._spawn.side_effect = wrong_mount
        with self.assertRaises(SandboxError):
            await self.open()
        self.assertEqual(self.volumes.browsers.current(self.identity)["state"], "needs_attention")

    async def test_close_waits_for_dispatch_without_holding_allocation_lock(self):
        await self.open()
        self.channel.gate = asyncio.Event()
        action = asyncio.create_task(self.call())
        await self.channel.started.wait()
        closing = asyncio.create_task(self.browser.drain(self.identity, self.sid))
        try:
            await asyncio.sleep(0)
            async with asyncio.timeout(1):
                async with self.manager._allocation_lock:
                    self.assertFalse(closing.done())
            self.channel.gate.set()
            await asyncio.wait_for(action, 2)
            await asyncio.wait_for(closing, 2)
        finally:
            self.channel.gate.set()
            for pending in (action, closing):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(action, closing, return_exceptions=True)

    async def test_capacity_inventory_failure_never_becomes_zero_usage(self):
        self.manager._run = AsyncMock(return_value=SandboxResult("truncated-id", "", 0))
        with self.assertRaisesRegex(SandboxError, "inventory"):
            await self.manager._browser_capacity("scope")

    def test_launch_source_never_imports_project_code_and_checks_root_first(self):
        command = worker_command("/nix/store/python/bin/python3", b"source", "123:456")
        self.assertEqual(command[1:3], ["-I", "-c"])
        self.assertLess(command[3].index("os.stat('/workspace'"), command[3].index("runpy.run_path"))
        compile(command[3], "browser-bootstrap", "exec")


class BrowserDesktopTests(BrowserOciTests):
    desktop = True

    async def test_quota_stops_browser_and_parent_and_keeps_unknown_effect_fenced(self):
        await self.open()
        self.channel.gate = asyncio.Event()
        action = asyncio.create_task(self.call())
        await self.channel.started.wait()
        try:
            with patch("src.sandbox_runtime.project_directory.allocated_bytes", return_value=70 * MIB):
                await asyncio.wait_for(self.workspaces.enforce("scope", 1, 1), 3)
            self.assertFalse(self.parent["State"]["Running"])
            self.assertNotIn(self.spec["name"], self.docker.containers)
            self.assertFalse(self.volumes.browsers.maintenance_ready(self.identity))
        finally:
            action.cancel()
            self.channel.gate.set()
            with self.assertRaises(asyncio.CancelledError):
                await action
        await self.browser.drain(self.identity, self.sid, cancel_pending=True)
        self.assertFalse(self.volumes.browsers.maintenance_ready(self.identity))

    async def test_quota_stops_browser_even_when_parent_already_exited(self):
        await self.open()
        self.parent["State"].update(Running=False, Pid=0)
        with patch("src.sandbox_runtime.project_directory.allocated_bytes", return_value=70 * MIB):
            await self.workspaces.enforce("scope", 1, 1)
        self.assertNotIn(self.spec["name"], self.docker.containers)

    async def test_quota_failure_on_foreign_browser_still_stops_own_parent(self):
        await self.open()
        self.docker.containers[self.spec["name"]]["Config"]["Labels"]["gaoji.app.browser-session"] = "foreign"
        with patch("src.sandbox_runtime.project_directory.allocated_bytes", return_value=70 * MIB):
            with self.assertRaises(SandboxError):
                await self.workspaces.enforce("scope", 1, 1)
        self.assertFalse(self.parent["State"]["Running"])
        self.assertTrue(self.docker.containers[self.spec["name"]]["State"]["Running"])

    async def test_full_project_refuses_new_browser_before_creation(self):
        with patch("src.sandbox_runtime.project_directory.allocated_bytes", return_value=70 * MIB):
            with self.assertRaisesRegex(SandboxError, "project_quota_full"):
                await self.open()
        self.browser._spawn.assert_not_awaited()
        self.assertIsNone(self.volumes.browsers.current(self.identity))

    async def test_unknown_result_can_receive_original_late_reply_but_not_repeat_click(self):
        await self.open()
        self.channel.lose_reply = True
        with self.assertRaises(TimeoutError):
            await self.call()
        with self.assertRaises(ProtocolError):
            await self.call()
        await self.browser.receive_pending(self.identity, self.sid, persist=self.persist)
        self.engine.click.assert_awaited_once()
        await self.browser.drain(self.identity, self.sid)
        self.assertTrue(self.volumes.browsers.maintenance_ready(self.identity))


if __name__ == "__main__":
    unittest.main()
