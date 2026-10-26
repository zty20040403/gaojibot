"""Local filesystem and Docker-metadata fixtures; never contacts a server/engine."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.local_runtime.capacity import Capacity, allocated_bytes, prepare_directory
from src.local_runtime.instances import InstanceStore
from src.storage_admission import (
    CapacityError, DEFAULT_POLICY, GIB, MIB, check_snapshot, policy,
    read_snapshot, require_capacity, require_tool_capacity,
)


class CapacityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = InstanceStore(self.root / "data")
        config = self.store.create("Capacity fixture")
        self.identifier = config["id"]
        self.directory = self.store.directory(self.identifier)
        self.c = SimpleNamespace(store=self.store, identifier=self.identifier, directory=self.directory)
        self.capacity = Capacity(self.c)
        self.engine = self.root / "engine"
        self.engine.mkdir()
        self.volumes = []
        self.containers = []
        self.capacity.command = self.command

    def command(self, *args):
        if args[:1] == ("info",):
            return json.dumps(str(self.engine))
        if args[:2] == ("volume", "ls"):
            return "\n".join(row["Name"] for row in self.volumes)
        if args[:2] == ("volume", "inspect"):
            return json.dumps(self.volumes)
        if args[:1] == ("ps",):
            return "\n".join(row["Id"] for row in self.containers)
        if args[:2] == ("container", "inspect"):
            return json.dumps(self.containers)
        raise AssertionError(args)

    def volume(self, suffix):
        name = f"gaoji-app-{self.identifier}-{suffix}"
        folder = self.engine / "volumes" / name / "_data"
        folder.mkdir(parents=True)
        self.volumes.append({"Name": name, "Driver": "local", "Options": None, "Mountpoint": str(folder),
            "Labels": {"gaoji.app.instance": self.identifier, "gaoji.app.resource": "nix-cache" if suffix == "nix" else "workspace"}})
        return folder

    def snapshot(self):
        return {"schema": 1, "instance_id": self.identifier, "sampled_at": int(time.time()), "complete": True,
            "policy": dict(DEFAULT_POLICY), "used_bytes": 3 * GIB, "disks": [{"name": "instance", "free_bytes": 10 * GIB}]}

    def test_policy_rejects_unknown_fields_boolean_float_and_unbounded_values(self):
        self.assertEqual(policy(DEFAULT_POLICY), DEFAULT_POLICY)
        for key, values in (("max_data_gib", [0, 4, True, 5.5, 1048577]), ("reserve_free_gib", [0, False, "2", 1025])):
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(CapacityError):
                    policy({**DEFAULT_POLICY, key: value})
        with self.assertRaises(CapacityError):
            policy({**DEFAULT_POLICY, "delete_pending": True})

    def test_prepare_does_not_change_an_already_correct_directory(self):
        from src.local_runtime.purge_inventory import tree_snapshot
        public = prepare_directory(self.directory)
        before = tree_snapshot(self.directory)
        for _ in range(3):
            self.assertEqual(prepare_directory(self.directory), public)
        self.assertEqual(tree_snapshot(self.directory), before)
        public.chmod(0o700)
        prepare_directory(self.directory)
        self.assertEqual(public.stat().st_mode & 0o7777, 0o755)
        public.chmod(0o777)
        with self.assertRaises(CapacityError):
            prepare_directory(self.directory)

    def test_snapshot_rejects_stale_foreign_corrupt_and_incomplete_measurements(self):
        value = self.snapshot()
        self.capacity.publish(value)
        self.assertEqual(read_snapshot(self.capacity.filename, self.identifier), value)
        for changes in ({"sampled_at": value["sampled_at"] - 181}, {"sampled_at": value["sampled_at"] + 20},
            {"instance_id": "e" * 32}, {"disks": []}, {"disks": [False]}, {"used_bytes": -1}, {"used_bytes": True}):
            self.capacity.publish({**value, **changes})
            with self.subTest(changes=changes), self.assertRaises(CapacityError):
                read_snapshot(self.capacity.filename, self.identifier)
        self.capacity.filename.write_text("not json")
        self.assertEqual(self.capacity.status()["state"], "unknown")
        self.capacity.publish({**value, "complete": False})
        self.assertEqual(self.capacity.status()["state"], "unknown")

    def test_status_never_follows_links_or_blocks_on_fifo(self):
        prepare_directory(self.directory)
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(self.snapshot()))
        self.capacity.filename.symlink_to(outside)
        with self.assertRaises(CapacityError):
            read_snapshot(self.capacity.filename, self.identifier)
        self.capacity.filename.unlink()
        os.mkfifo(self.capacity.filename)
        with self.assertRaises(CapacityError):
            read_snapshot(self.capacity.filename, self.identifier)
        self.capacity.filename.unlink()
        self.capacity.publish(self.snapshot())
        self.capacity.filename.chmod(0o666)
        with self.assertRaises(CapacityError):
            read_snapshot(self.capacity.filename, self.identifier)
        self.capacity.filename.chmod(0o644)
        self.capacity.filename.write_bytes(b" " * 65537)
        with self.assertRaises(CapacityError):
            read_snapshot(self.capacity.filename, self.identifier)

    def test_budget_and_each_filesystem_reserve_have_explicit_boundaries(self):
        value = self.snapshot()
        value["policy"]["max_data_gib"] = 5
        check_snapshot(value, 2 * GIB)
        with self.assertRaisesRegex(CapacityError, "额度"):
            check_snapshot(value, 2 * GIB + 1)
        value["disks"].append({"name": "docker", "free_bytes": 2 * GIB + MIB})
        check_snapshot(value, MIB)
        with self.assertRaisesRegex(CapacityError, "预留"):
            check_snapshot(value, MIB + 1)
        with self.assertRaises(CapacityError):
            check_snapshot(value, -1)

    def test_allocated_scan_deduplicates_hardlinks_and_does_not_follow_symlinks(self):
        folder = self.root / "scan"
        folder.mkdir()
        original = folder / "file"
        original.write_bytes(b"x" * 8192)
        (folder / "hardlink").hardlink_to(original)
        outside = self.root / "external"
        outside.write_bytes(b"x" * 1048576)
        link = folder / "link"
        link.symlink_to(outside)
        expected = sum(item.lstat().st_blocks * 512 for item in (folder, original, link))
        self.assertEqual(allocated_bytes(folder, deadline=time.monotonic() + 5), expected)
        with self.assertRaises(CapacityError):
            allocated_bytes(folder, deadline=time.monotonic() - 1)
        with self.assertRaises(OSError):
            allocated_bytes(link, deadline=time.monotonic() + 5)

    def test_scan_counts_only_owned_volumes_directory_and_writable_layers(self):
        volume = self.volume("work-s123abc")
        (volume / "report").write_bytes(b"x" * 4096)
        self.volume("nix")
        self.containers = [{"Id": "1" * 64, "SizeRw": 8192,
            "Config": {"Labels": {"gaoji.app.instance": self.identifier}}},
            {"Id": "2" * 64, "SizeRw": 4096,
            "Config": {"Labels": {"com.docker.compose.project": "gaoji-" + self.identifier}}}]
        snapshot = self.capacity.scan()
        self.assertTrue(snapshot["complete"], snapshot)
        self.assertEqual(snapshot["writable_bytes"], 12288)
        self.assertGreaterEqual(snapshot["sandbox_bytes"], 4096)
        self.assertEqual(snapshot["used_bytes"], sum(snapshot[key] for key in ("data_bytes", "sandbox_bytes", "writable_bytes")))
        self.assertEqual(self.capacity.status()["state"], "ready")
        self.assertEqual(self.capacity.filename.stat().st_mode & 0o777, 0o644)
        self.assertEqual((volume / "report").read_bytes(), b"x" * 4096)

    def test_untrusted_volume_metadata_never_scans_other_directories(self):
        self.volume("nix")
        original = deepcopy(self.volumes[0])
        for changes in ({"Labels": {"gaoji.app.instance": "f" * 32}}, {"Driver": "nfs"},
            {"Mountpoint": str(self.root)}, {"Options": {"device": str(self.root)}}, {"Labels": ["bad"]}):
            self.volumes[0] = {**original, **changes}
            with patch("src.local_runtime.capacity.allocated_bytes", side_effect=AssertionError("must not scan")):
                self.assertFalse(self.capacity.scan()["complete"])

    def test_duplicate_inspection_rows_and_foreign_containers_are_not_counted(self):
        self.volume("nix")
        self.volume("work-s123abc")
        original = self.command
        def duplicate(*args):
            if args[:2] == ("volume", "inspect"):
                return json.dumps([self.volumes[0], self.volumes[0]])
            return original(*args)
        self.capacity.command = duplicate
        self.assertFalse(self.capacity.scan()["complete"])
        self.capacity.command = original
        self.containers = [{"Id": "1" * 64, "SizeRw": 32, "Config": {"Labels": {"gaoji.app.instance": "f" * 32}}}]
        self.assertFalse(self.capacity.scan()["complete"])

    def test_low_space_or_engine_failure_preserves_pending_files_and_can_recover(self):
        pending = self.directory / "files/undelivered.pdf"
        pending.write_bytes(b"pending")
        with patch("src.local_runtime.capacity.shutil.disk_usage", return_value=shutil._ntuple_diskusage(10 * GIB, 9 * GIB, GIB)):
            self.assertTrue(self.capacity.scan()["complete"])
            self.assertEqual(self.capacity.status()["state"], "blocked")
            with self.assertRaisesRegex(CapacityError, "预留"):
                self.capacity.require()
        with patch.object(self.capacity, "command", side_effect=CapacityError("Docker 容量统计暂不可用")):
            self.capacity.scan()
            self.assertEqual(self.capacity.status()["state"], "unknown")
        self.capacity.require()
        self.assertEqual(self.capacity.status()["state"], "ready")
        self.assertEqual(pending.read_bytes(), b"pending")

    def test_docker_commands_share_the_sample_deadline(self):
        self.c.deployment = {"docker": "/fixture/docker", "docker_host": "unix:///fixture/docker.sock"}
        self.c.process_environment = lambda: {"DOCKER_HOST": "unix:///fixture/docker.sock"}
        self.capacity.deadline = time.monotonic() + 2
        with patch("src.local_runtime.capacity.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=b"ok")) as run:
            self.assertEqual(Capacity.command(self.capacity, "info"), "ok")
            self.assertEqual(run.call_args.args[0], ["/fixture/docker", "info"])
            self.assertGreater(run.call_args.kwargs["timeout"], 0)
            self.assertLessEqual(run.call_args.kwargs["timeout"], 2)
            self.capacity.deadline = time.monotonic() - 1
            with self.assertRaisesRegex(CapacityError, "超时"):
                Capacity.command(self.capacity, "info")
            run.assert_called_once()

    def test_legacy_is_unchanged_and_existing_delivery_reads_cleanup_are_allowed(self):
        with patch.dict(os.environ, {"AI_APP_CAPACITY_FILE": ""}):
            require_capacity()
        with patch.dict(os.environ, {"AI_APP_CAPACITY_FILE": str(self.capacity.filename), "AI_APP_INSTANCE_ID": self.identifier}):
            for tool, effects in (("send_file_from_sandbox", ("send:file",)), ("sandbox_read_file", ("read",)),
                ("sandbox_destroy", ("write:sandbox",)), ("browser_clear", ("write:browser",)),
                ("run_subagents", ("spawn:agent",)), ("resume_subagent", ("resume:agent",))):
                require_tool_capacity(tool, effects)
            for effect in ("write:sandbox", "write:browser"):
                with self.subTest(effect=effect), self.assertRaises(CapacityError):
                    require_tool_capacity("new_task", (effect,))
            self.capacity.publish(self.snapshot())
            require_tool_capacity("sandbox_exec", ("write:sandbox",))

    async def test_media_and_video_downloads_are_refused_before_network_or_file_writes(self):
        import nonebot
        nonebot.init()
        from src.plugins.ai_chat.media_library import MediaLibrary, MediaLibraryError
        from src.plugins.ai_chat.vision_worker import VisionWorker, VisionJobError
        from src.plugins.ai_chat.video_analysis import DeepVideoAnalyzer, DeepVideoAnalysisError
        self.capacity.publish({**self.snapshot(), "used_bytes": 101 * GIB})
        with patch.dict(os.environ, {"AI_APP_CAPACITY_FILE": str(self.capacity.filename), "AI_APP_INSTANCE_ID": self.identifier}), \
                patch("httpx.AsyncClient", side_effect=AssertionError("No network when blocked")):
            for kind, error in ((MediaLibrary, MediaLibraryError), (VisionWorker, VisionJobError)):
                downloader = object.__new__(kind)
                downloader.max_source_bytes = 100 * MIB
                with self.subTest(kind=kind), self.assertRaisesRegex(error, "额度"):
                    await downloader._download("https://multimedia.nt.qq.com.cn/fixture.png")
            analyzer = object.__new__(DeepVideoAnalyzer)
            analyzer.max_download_bytes = 512 * MIB
            target = self.directory / "files/new-video.mp4"
            with self.assertRaisesRegex(DeepVideoAnalysisError, "额度"):
                await analyzer._download("https://fixture.invalid/video.mp4", target, max_bytes=512 * MIB)
            with self.assertRaisesRegex(DeepVideoAnalysisError, "额度"):
                await analyzer._download_qq_media("https://multimedia.nt.qq.com.cn/fixture.mp4", target)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
