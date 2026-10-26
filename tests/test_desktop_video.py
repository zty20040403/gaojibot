from __future__ import annotations

import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from src.storage_admission import CapacityError

from src.local_runtime import video_assets as module
from src.local_runtime.runtime_check import check_video


DATA = b"fixture-model" * 200


class Response(io.BytesIO):
    def __init__(self, data=DATA, *, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.url = module.MODEL_URL
        self.headers = headers or {"Content-Length": str(len(data))}


class VideoAssetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.assets = module.VideoAssets(self.root)
        for name, value in (("MODEL_BYTES", len(DATA)), ("MODEL_SHA256", hashlib.sha256(DATA).hexdigest())):
            active = patch.object(module, name, value)
            active.start(); self.addCleanup(active.stop)
        self.opener_patch = patch.object(module.urllib.request, "build_opener")
        self.opener = self.opener_patch.start().return_value
        self.addCleanup(self.opener_patch.stop)

    def test_verified_download_is_readonly_and_reused_without_network(self):
        self.opener.open.return_value = Response()
        self.assertEqual(self.assets.status()["state"], "not_installed")
        target = self.assets.ensure()
        self.assertEqual(target.read_bytes(), DATA)
        self.assertEqual(target.stat().st_mode & 0o777, 0o444)
        self.assertEqual(self.assets.status()["state"], "installed")
        admit = Mock(side_effect=CapacityError("容量不足"))
        self.assets.ensure(admit=admit)
        admit.assert_not_called()
        self.opener.open.assert_called_once()
        with patch("src.local_runtime.runtime_check.subprocess.run") as command:
            self.assertTrue(check_video(target)["video_ready"])
            self.assertEqual(command.call_args.kwargs["timeout"], 120)
            self.assertIn(str(target), command.call_args.args[0])
            arguments = command.call_args.args[0]
            self.assertEqual(arguments[arguments.index("-ac") + 1], "128")
            self.assertIn("-nf", arguments)

    def test_host_hash_verification_supports_python_310(self):
        self.assets.prepare()
        self.assets.target.write_bytes(DATA)
        with patch.object(module.hashlib, "file_digest", side_effect=AssertionError("Not available on Python 3.10"), create=True):
            self.assets.ensure()
        self.opener.open.assert_not_called()

    def test_capacity_is_checked_before_new_model_download(self):
        admit = Mock(side_effect=CapacityError("容量不足"))
        with self.assertRaisesRegex(CapacityError, "容量"):
            self.assets.ensure(admit=admit)
        admit.assert_called_once_with(len(DATA) + 64 * 1024**2)
        self.opener.open.assert_not_called()
        self.assertFalse(self.assets.part.exists())

    def test_interrupted_download_resumes_same_file_with_valid_range(self):
        response = Response()
        reads = iter([DATA[:200], TimeoutError("network interrupted")])
        def read(_size):
            value = next(reads)
            if isinstance(value, Exception):
                raise value
            return value
        response.read1 = read
        self.opener.open.return_value = response
        with self.assertRaises(module.VideoAssetError):
            self.assets.ensure()
        self.assertEqual(self.assets.status()["state"], "partial")
        self.assertFalse(self.assets.target.exists())
        self.opener.open.return_value = Response(DATA[200:], status=206,
            headers={"Content-Range": f"bytes 200-{len(DATA)-1}/{len(DATA)}"})
        self.assets.ensure()
        self.assertEqual(self.opener.open.call_args.args[0].get_header("Range"), "bytes=200-")
        self.assertEqual(self.assets.target.read_bytes(), DATA)

    def test_full_uncommitted_download_is_verified_and_published_after_restart(self):
        self.assets.prepare(); self.assets.part.write_bytes(DATA)
        self.assets.ensure()
        self.opener.open.assert_not_called()
        self.assertEqual(self.assets.status()["state"], "installed")

    def test_server_ignoring_range_restarts_only_the_partial_file(self):
        self.assets.prepare(); self.assets.part.write_bytes(DATA[:200])
        self.opener.open.return_value = Response()
        self.assets.ensure()
        self.assertEqual(self.assets.target.read_bytes(), DATA)

    def test_wrong_range_size_or_checksum_cannot_be_published(self):
        self.assets.prepare(); self.assets.part.write_bytes(DATA[:200])
        self.opener.open.return_value = Response(DATA[200:], status=206,
            headers={"Content-Range": f"bytes 201-{len(DATA)-1}/{len(DATA)}"})
        with self.assertRaisesRegex(module.VideoAssetError, "范围"):
            self.assets.ensure()
        self.assertEqual(self.assets.part.read_bytes(), DATA[:200])
        for data, expected in ((DATA + b"oversize", "大小"), (b"bad" + DATA[3:], "校验")):
            self.opener.open.return_value = Response(data)
            with self.subTest(expected=expected), self.assertRaisesRegex(module.VideoAssetError, expected):
                self.assets.ensure()
            self.assertFalse(self.assets.target.exists())

    def test_low_space_and_symlinks_never_modify_outside_data(self):
        with patch.object(module.shutil, "disk_usage", return_value=SimpleNamespace(free=0)), self.assertRaisesRegex(module.VideoAssetError, "空间"):
            self.assets.ensure()
        self.opener.open.assert_not_called()
        outside = self.root / "user-file"; outside.write_text("keep")
        self.assets.part.symlink_to(outside)
        with self.assertRaises(module.VideoAssetError):
            self.assets.ensure()
        self.assertEqual(outside.read_text(), "keep")
        self.assets.part.unlink()
        self.assets.target.symlink_to(outside)
        with self.assertRaises(module.VideoAssetError):
            self.assets.ensure()
        self.assertEqual(outside.read_text(), "keep")

    def test_redirects_are_https_and_within_fixed_model_cdn(self):
        for url in ("http://huggingface.co/file", "https://huggingface.co.evil.invalid/file", "https://127.0.0.1/file", "https://user@huggingface.co/file"):
            with self.subTest(url=url), self.assertRaises(module.VideoAssetError):
                module.ModelRedirects().redirect_request(None, None, 302, "", {}, url)

    def test_dangling_model_directory_is_invalid_without_creating_its_target(self):
        outside = self.root / "absent-user-directory"
        self.assets.root.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.assets.status()["state"], "invalid")
        with self.assertRaises(module.VideoAssetError):
            self.assets.ensure()
        self.assertFalse(outside.exists())
        self.opener.open.assert_not_called()

    def test_corrupted_installed_model_is_repaired_and_failed_runtime_check_raises(self):
        self.opener.open.return_value = Response()
        target = self.assets.ensure()
        target.chmod(0o644); target.write_bytes(b"x" * len(DATA))
        self.assertEqual(self.assets.status()["state"], "needs_verification")
        with self.assertRaises(ValueError):
            check_video(target)
        self.opener.open.return_value = Response()
        self.assets.ensure()
        self.assertEqual(target.read_bytes(), DATA)
        with patch("src.local_runtime.runtime_check.subprocess.run", side_effect=OSError("missing executable")), self.assertRaises(OSError):
            check_video(target)


if __name__ == "__main__":
    unittest.main()
