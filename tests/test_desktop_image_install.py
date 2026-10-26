import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

from src.local_runtime.image_install import catalog_image, install_options, prepare_image, progress_detail, failure_detail, run_observed
from src.local_runtime.releases import ReleaseError


class ImageInstallTests(unittest.TestCase):
    def test_prebuilt_digest_and_scope(self):
        remote = "ghcr.io/example/runtime@sha256:" + "a" * 64
        catalog = {"schema": 1, "checksum": "b" * 64, "images": {"linux/amd64": {"runtime": remote, "controller": remote}}}
        self.assertEqual(catalog_image(catalog, "b" * 64), remote)
        with self.assertRaises(ReleaseError):
            catalog_image(catalog, "c" * 64)
        catalog["images"]["linux/amd64"]["runtime"] = "ghcr.io/example/runtime:latest"
        with self.assertRaises(ReleaseError):
            catalog_image(catalog, "b" * 64)

    def test_prebuilt_pull_is_not_a_source_build_and_failed_pull_does_not_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            release = Path(temp)
            remote = "ghcr.io/example/runtime@sha256:" + "a" * 64
            (release / ".runtime-images.json").write_text(json.dumps({"schema": 1, "checksum": "b" * 64,
                "images": {"linux/amd64": {"runtime": remote, "controller": remote}}}))
            run = Mock(side_effect=["", json.dumps([{"Architecture": "amd64", "Config": {"Labels": {"gaoji.desktop.runtime-digest": "b" * 64}}}]), ""])
            prepare_image("/docker", release, "b" * 64, "gaoji-app-runtime:" + "c" * 64, run, Mock())
            self.assertEqual([call.args[0][1] for call in run.call_args_list], ["pull", "image", "tag"])
            failed = Mock(side_effect=RuntimeError("network"))
            with self.assertRaises(RuntimeError):
                prepare_image("/docker", release, "b" * 64, "target", failed, Mock())
            self.assertEqual(failed.call_count, 1)

    def test_selection_is_allowlisted_and_raw_logs_never_go_to_public_status(self):
        with self.assertRaises(ReleaseError):
            install_options({"source": "official; reboot"})
        self.assertIsNone(progress_detail("https://secret.invalid/?token=sensitive"))
        self.assertEqual(progress_detail("#7 10 Get:194 https://deb.invalid bookworm/main amd64 ffmpeg amd64 7.0 [1 MB]"), "下载系统依赖：ffmpeg")
        report = Mock()
        code = run_observed([sys.executable, "-u", "-c", "print('Downloading https://private.invalid/?token=secret')"],
            timeout=5, environment=dict(os.environ), log=io.BytesIO(), report=report)
        self.assertEqual(code, 0)
        self.assertNotIn("secret", str(report.call_args_list))

    def test_timeout_is_reported_without_raw_log_or_tokens(self):
        line = "curl: (28) Operation timed out https://private.invalid/?token=secret"
        self.assertEqual(failure_detail(line), "构建依赖下载超时，当前下载连接过慢或中断")
        report = Mock()
        code = run_observed([sys.executable, "-u", "-c", "import sys; print(" + repr(line) + "); sys.exit(28)"],
            timeout=5, environment=dict(os.environ), log=io.BytesIO(), report=report)
        self.assertEqual(code, 28)
        self.assertEqual(report.call_args.args[0]["failure"], failure_detail(line))
        self.assertNotIn("secret", str(report.call_args_list))
