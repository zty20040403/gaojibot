"""Pinned build inputs, archive verification and restart behavior without Docker."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
from src.storage_admission import CapacityError

from src.local_runtime.files import read_json, write_json
from src.local_runtime.sandbox_image import SandboxImage, SandboxImageError, archive_image, recipe


ROOT = Path(__file__).resolve().parents[1]


class ImageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for name in ("default.nix", "build.sh", "builder.lock.json"):
            shutil.copyfile(ROOT / "packaging/sandbox" / name, self.source / name)
        shutil.copyfile(ROOT / "nix/sandbox-image.nix", self.source / "image.nix")
        self.image = SandboxImage(self.source, self.root / "cache", docker="/test/docker", docker_host="unix:///test/engine")
        integrity = patch.object(self.image, "verify_integrity", side_effect=lambda ready: ready)
        self.integrity = integrity.start()
        self.addCleanup(integrity.stop)

    def archive(self, *, image=None, user="1000:1000", platform="amd64", mismatch=False):
        self.image.archive.parent.mkdir(parents=True, exist_ok=True)
        config = json.dumps({"architecture": platform, "os": "linux", "config": {"User": user,
            "Labels": {"io.gaoji.app.recipe": self.image.recipe_id}},
            "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "b" * 64]}}).encode()
        digest = hashlib.sha256(config).hexdigest()
        name = ("a" * 64 if mismatch else digest) + ".json"
        manifest = [{"Config": name, "RepoTags": [image or self.image.image], "Layers": ["layer.tar"]}]
        with tarfile.open(self.image.archive, "w:gz") as archive:
            for filename, contents in (("manifest.json", json.dumps(manifest).encode()), (name, config), ("layer.tar", b"fixture")):
                member = tarfile.TarInfo(filename)
                member.size = len(contents)
                archive.addfile(member, io.BytesIO(contents))
        return {"config_id": "sha256:" + digest, "config": json.loads(config)["config"], "layers": ["sha256:" + "b" * 64]}

    def test_recipe_hash_ignores_app_version_but_tracks_all_tool_inputs(self):
        identifier, _ = recipe(self.source)
        (self.source / "version.txt").write_text("next App version")
        self.assertEqual(recipe(self.source)[0], identifier)
        for name in ("default.nix", "image.nix", "build.sh"):
            original = (self.source / name).read_bytes()
            (self.source / name).write_bytes(original + b"\n# changed\n")
            self.assertNotEqual(recipe(self.source)[0], identifier)
            (self.source / name).write_bytes(original)

    def test_recipe_rejects_unpinned_or_wrong_builder_and_symlinks(self):
        lock = read_json(self.source / "builder.lock.json")
        for changed in ({"image": "nixos/nix:latest"}, {"platform": "linux/arm64"}, {"image": "private.example/nix@sha256:" + "a" * 64}):
            write_json(self.source / "builder.lock.json", {**lock, **changed})
            with self.assertRaises(SandboxImageError):
                recipe(self.source)
        write_json(self.source / "builder.lock.json", lock)
        (self.source / "build.sh").unlink()
        (self.source / "build.sh").symlink_to(ROOT / "packaging/sandbox/build.sh")
        with self.assertRaises(SandboxImageError):
            recipe(self.source)

    def test_builder_checks_store_contents_and_does_not_build_as_root(self):
        script = (self.source / "build.sh").read_text()
        self.assertIn("--option build-users-group nixbld", script)
        self.assertNotIn("--option build-users-group ''", script)
        self.assertLess(script.index("nix-store --verify --check-contents"), script.index("cp -L"))

    def test_archive_checks_real_config_digest_platform_user_and_expected_tag(self):
        expected = self.archive()
        self.assertEqual(archive_image(self.image.archive, self.image.image), expected)
        for options in ({"image": "other:latest"}, {"user": "0:0"}, {"platform": "arm64"}, {"mismatch": True}):
            self.archive(**options)
            with self.assertRaises(SandboxImageError):
                archive_image(self.image.archive, self.image.image)

    def test_archive_never_extracts_links_or_paths_outside_target(self):
        self.image.archive.parent.mkdir(parents=True)
        for name, kind in (("../outside", tarfile.REGTYPE), ("/root/config", tarfile.REGTYPE), ("manifest.json", tarfile.SYMTYPE)):
            with tarfile.open(self.image.archive, "w:gz") as archive:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = "/etc/passwd"
                archive.addfile(member)
            with self.assertRaises(SandboxImageError):
                archive_image(self.image.archive, self.image.image)
        self.assertFalse((self.root / "outside").exists())

    def test_existing_ready_image_is_reused_without_a_build_or_pull(self):
        ready = {"image": self.image.image, "image_id": "sha256:" + "d" * 64, "bytes": 1024,
                 "config_id": "sha256:" + "a" * 64, "integrity": "nix-store-v1"}
        self.image.directory(self.image.root)
        write_json(self.image.marker, ready)
        admit = Mock(side_effect=CapacityError("容量不足"))
        with patch.object(self.image, "existing_image", return_value=ready), \
                patch.object(self.image, "clean_builder") as clean, patch.object(self.image, "prepare_builder") as build:
            self.assertEqual(self.image.ensure(admit=admit), ready)
            admit.assert_not_called()
            build.assert_not_called()
            clean.assert_called_once()
            self.integrity.assert_not_called()
        self.assertEqual(read_json(self.image.marker), ready)

    def test_capacity_is_checked_before_new_image_build(self):
        admit = Mock(side_effect=CapacityError("容量不足"))
        with patch.object(self.image, "existing_image", return_value=None), patch.object(self.image, "prepare_builder") as build:
            with self.assertRaisesRegex(CapacityError, "容量"):
                self.image.ensure(admit=admit)
        admit.assert_called_once_with(4 * 1024**3)
        build.assert_not_called()

    def test_old_marker_does_not_skip_integrity_check(self):
        ready = {"image_id": "sha256:" + "d" * 64, "config_id": "sha256:" + "a" * 64}
        self.image.directory(self.image.root)
        write_json(self.image.marker, ready)
        with patch.object(self.image, "existing_image", return_value=ready), patch.object(self.image, "clean_builder"):
            self.image.ensure()
        self.integrity.assert_called_once_with(ready)

    def test_failed_integrity_does_not_mark_image_ready_or_remove_build_evidence(self):
        self.archive()
        ready = {"image_id": "sha256:" + "d" * 64}
        self.integrity.side_effect = SandboxImageError("bad contents")
        with patch.object(self.image, "existing_image", return_value=ready), \
                patch.object(self.image, "verify_loaded", return_value=ready), \
                patch.object(self.image, "clean_builder") as cleanup, self.assertRaises(SandboxImageError):
            self.image.ensure()
        cleanup.assert_not_called()
        self.assertFalse(self.image.marker.exists())
        self.assertTrue(self.image.archive.exists())

    def test_integrity_check_is_offline_readonly_and_checks_contents(self):
        ready = {"image_id": "sha256:" + "d" * 64}
        with patch.object(self.image, "run", return_value="") as command:
            verified = SandboxImage.verify_integrity(self.image, ready)
        args = command.call_args_list[0].args
        self.assertEqual(args[0], "run")
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertIn("--read-only", args)
        self.assertIn("local?read-only=true", args)
        self.assertIn("--all", args)
        self.assertNotIn("--no-contents", args)
        self.assertNotIn("--mount", args)
        self.assertEqual(verified["integrity"], "nix-store-v1")

    def test_integrity_timeout_removes_only_exact_own_check(self):
        identifier = "f" * 64
        ready = {"image_id": "sha256:" + "d" * 64}
        name = "gaoji-app-image-check-" + "e" * 32
        record = {"Id": identifier, "Name": "/" + name, "Config": {"Image": ready["image_id"],
                  "Labels": {"gaoji.app.image-check": self.image.recipe_id}}}
        with patch("src.local_runtime.sandbox_image.uuid.uuid4") as random_id, \
                patch.object(self.image, "run", side_effect=[SandboxImageError("timeout"), identifier, json.dumps(record), ""]) as command:
            random_id.return_value.hex = "e" * 32
            with self.assertRaisesRegex(SandboxImageError, "校验未通过"):
                SandboxImage.verify_integrity(self.image, ready)
        self.assertEqual(command.call_args_list[-1].args, ("rm", "-f", identifier))

    def test_running_build_is_polled_and_not_started_again(self):
        expected = self.archive()
        builder = {"Id": "e" * 64, "State": {"Running": True, "Status": "running"}}
        done = {"Id": "e" * 64, "State": {"Running": False, "Status": "exited", "ExitCode": 0}}
        ready = {"image": self.image.image, "image_id": expected["config_id"]}
        with patch.object(self.image, "existing_image", side_effect=[None, ready]), \
                patch.object(self.image, "existing_builder", side_effect=[builder, builder, done]), \
                patch.object(self.image, "run") as command, patch.object(self.image, "clean_builder"), \
                patch.object(self.image, "verify_loaded", return_value=ready) as verify, \
                patch.object(self.image, "prepare_builder") as create, patch("src.local_runtime.sandbox_image.time.sleep"):
            self.assertEqual(self.image.ensure(), ready)
        create.assert_not_called()
        self.assertEqual([call.args[0] for call in command.call_args_list], ["load"])
        verify.assert_called_once_with(expected, ready)

    def test_failed_terminal_build_can_resume_same_container(self):
        expected = self.archive()
        failed = {"Id": "e" * 64, "State": {"Running": False, "Status": "exited", "ExitCode": 1}}
        done = {"Id": "e" * 64, "State": {"Running": False, "Status": "exited", "ExitCode": 0}}
        with patch.object(self.image, "existing_image", side_effect=[None, {"image_id": expected["config_id"]}]), \
                patch.object(self.image, "existing_builder", side_effect=[failed, done]), \
                patch.object(self.image, "run") as command, patch.object(self.image, "clean_builder"), \
                patch.object(self.image, "verify_loaded", return_value={"image_id": expected["config_id"]}), \
                patch.object(self.image, "prepare_builder") as create:
            self.image.ensure()
        create.assert_not_called()
        self.assertEqual(command.call_args_list[0].args, ("start", "e" * 64))

    def test_loaded_image_verifies_layers_and_configuration_for_both_docker_stores(self):
        expected = self.archive()
        for image_id in (expected["config_id"], "sha256:" + "c" * 64):
            ready = {"image_id": image_id}
            actual = {"Id": image_id, "Os": "linux", "Architecture": "amd64", "Config": expected["config"],
                      "RootFS": {"Type": "layers", "Layers": expected["layers"]}}
            with patch.object(self.image, "run", return_value=json.dumps(actual)):
                self.assertEqual(self.image.verify_loaded(expected, ready), {**ready, "config_id": expected["config_id"]})
            for changed in ({"RootFS": {"Type": "layers", "Layers": ["sha256:" + "d" * 64]}},
                            {"Config": {**expected["config"], "Entrypoint": ["/unexpected"]}},
                            {"Config": {**expected["config"], "User": "0:0"}}):
                with patch.object(self.image, "run", return_value=json.dumps({**actual, **changed})), self.assertRaises(SandboxImageError):
                    self.image.verify_loaded(expected, ready)

    def test_unrecorded_existing_image_requires_verified_archive_not_just_recipe_label(self):
        ready = {"image_id": "sha256:" + "c" * 64}
        with patch.object(self.image, "existing_image", return_value=ready), self.assertRaisesRegex(SandboxImageError, "校验记录"):
            self.image.ensure()
        expected = self.archive()
        verified = {**ready, "config_id": expected["config_id"]}
        with patch.object(self.image, "existing_image", return_value=ready), \
                patch.object(self.image, "verify_loaded", return_value=verified) as verify, \
                patch.object(self.image, "clean_builder"), patch.object(self.image, "prepare_builder") as build:
            self.assertEqual(self.image.ensure(), verified)
        verify.assert_called_once_with(expected, ready)
        build.assert_not_called()

    def test_observation_deadline_does_not_restart_or_remove_live_build(self):
        builder = {"Id": "e" * 64, "State": {"Running": True, "Status": "running"}}
        with patch.object(self.image, "existing_image", return_value=None), \
                patch.object(self.image, "existing_builder", return_value=builder), patch.object(self.image, "run") as command, \
                patch("src.local_runtime.sandbox_image.time.monotonic", side_effect=[0, 0, 4000]), self.assertRaisesRegex(SandboxImageError, "仍在运行"):
            self.image.ensure()
        command.assert_not_called()

    def test_builder_is_unprivileged_scoped_and_only_binds_public_recipe_and_output(self):
        self.image.directory(self.image.root)
        with patch.object(self.image, "run", return_value=str(8 * 1024**3)) as command, patch("src.local_runtime.sandbox_image.shutil.disk_usage",
                return_value=shutil._ntuple_diskusage(30 * 1024**3, 1, 29 * 1024**3)):
            self.image.prepare_builder()
        created = command.call_args_list[-1].args
        self.assertEqual(created[0], "create")
        self.assertNotIn("--privileged", created)
        self.assertNotIn("docker.sock", " ".join(created))
        self.assertIn("no-new-privileges", created)
        mounts = [created[index + 1] for index, word in enumerate(created) if word == "--mount"]
        self.assertEqual(len(mounts), 2)
        self.assertTrue(mounts[0].endswith("target=/recipe,readonly"))
        self.assertTrue(mounts[1].endswith("target=/out"))

    def test_small_engine_does_not_start_build_and_leaves_other_services_alone(self):
        self.image.directory(self.image.root)
        with patch.object(self.image, "run", return_value=str(4 * 1024**3)) as command, \
                patch("src.local_runtime.sandbox_image.shutil.disk_usage",
                      return_value=shutil._ntuple_diskusage(30 * 1024**3, 1, 29 * 1024**3)), \
                self.assertRaisesRegex(SandboxImageError, "6 GiB"):
            self.image.prepare_builder()
        command.assert_called_once_with("info", "--format", "{{.MemTotal}}")

    def test_low_space_does_not_pull_or_create(self):
        self.image.directory(self.image.root)
        with patch.object(self.image, "run") as command, patch("src.local_runtime.sandbox_image.shutil.disk_usage",
                return_value=shutil._ntuple_diskusage(1024, 512, 512)), self.assertRaisesRegex(SandboxImageError, "12 GiB"):
            self.image.prepare_builder()
        command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
