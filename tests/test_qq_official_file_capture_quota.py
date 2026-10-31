"""Physical quotas include pre-acceptance captures, staging and DB orphans."""
import asyncio
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.file_artifacts import ArtifactError, ArtifactStore
from src.plugins.ai_chat.execution_control import LeaseLost
from tests.qq_official_file_fixture import FileFixture, PDF, KEY

ONE = PDF + b"a" * (1024 - len(PDF))
TWO = PDF + b"b" * (1024 - len(PDF))


class CaptureQuotaTests(FileFixture):
    def setUp(self):
        super().setUp()
        self.files = ArtifactStore(transaction=self.tx, jobs=self.jobs, blobs=self.blobs,
            clock=lambda:self.now, max_bytes=1024, quota_bytes=1024,
            max_artifacts=2, account_key=self.account.key)

    def capture(self, data=ONE, **kwargs):
        return self.files.capture_verified(self.source, content=data, filename="test.pdf", **kwargs)

    def register(self, data=ONE):
        return self.files.register(self.source, content=data, filename="test.pdf", logical_request_key=KEY)

    def test_capture_has_real_source_and_bytes_but_no_manifest_or_file_job(self):
        blob = self.capture()
        self.assertEqual(self.blobs.read(blob.sha256), ONE)
        self.assertEqual(self.files.physical_usage(), (1024, 1))
        self.assertEqual(self.query("SELECT * FROM qq_official_file_artifacts"), [])
        self.assertEqual(self.query("SELECT * FROM qq_official_file_deliveries"), [])
        with self.assertRaises(LeaseLost):
            self.files.capture_verified(replace(self.source), content=ONE, filename="test.pdf")

    def test_same_sha_reuse_at_quota_does_not_rewrite_or_count_twice(self):
        first = self.capture()
        self.files.max_artifacts = 1
        with patch.object(self.blobs, "put", side_effect=AssertionError("duplicate temporary allocation")):
            second = self.capture()
            record = self.register()
        self.assertEqual(first, second)
        self.assertEqual(record.sha256, first.sha256)
        self.assertEqual(self.files.physical_usage(), (1024, 1))
        self.assertEqual(len(self.query("SELECT * FROM qq_official_file_artifacts")), 1)

    def test_unregistered_capture_charges_new_capture_and_direct_registration(self):
        self.capture()
        for operation in (lambda:self.capture(TWO), lambda:self.register(TWO)):
            with self.assertRaisesRegex(ArtifactError, "storage_quota"):
                operation()
        self.assertEqual(self.query("SELECT * FROM qq_official_file_artifacts"), [])
        self.assertEqual(self.files.physical_usage(), (1024, 1))

    def test_db_rollback_orphan_cannot_bypass_next_physical_quota_check(self):
        with patch.object(self.jobs, "enqueue", side_effect=RuntimeError("TEST injected rollback")):
            with self.assertRaises(RuntimeError):
                self.register()
        self.assertEqual(self.query("SELECT * FROM qq_official_file_artifacts"), [])
        self.assertEqual(self.files.physical_usage(), (1024, 1))
        with self.assertRaisesRegex(ArtifactError, "storage_quota"):
            self.capture(TWO)
        # The same source and bytes can finish the original registration.
        self.assertEqual(self.register().sha256, hashlib.sha256(ONE).hexdigest())

    def test_abandoned_staging_bytes_and_objects_are_charged(self):
        stage = self.blobs.root / "staging" / "upload-TEST"
        stage.write_bytes(b"x")
        stage.chmod(0o600)
        self.assertEqual(self.files.physical_usage(), (1, 1))
        with self.assertRaisesRegex(ArtifactError, "storage_quota"):
            self.capture()
        self.files.quota_bytes = 2048
        self.files.max_artifacts = 1
        with self.assertRaisesRegex(ArtifactError, "storage_quota"):
            self.capture()
        self.assertEqual(list((self.blobs.root / "blobs").iterdir()), [])

    def test_distinct_concurrent_captures_cannot_both_consume_last_capacity(self):
        async def run():
            calls = DatabaseCalls()
            try:
                results = await asyncio.gather(calls(self.capture, ONE), calls(self.capture, TWO), return_exceptions=True)
                self.assertEqual(sum(not isinstance(x, BaseException) for x in results), 1)
                self.assertEqual(sum(isinstance(x, ArtifactError) for x in results), 1)
            finally:
                await calls.close()
        asyncio.run(run())
        self.assertEqual(self.files.physical_usage(), (1024, 1))

    def test_source_lost_after_fsync_keeps_charged_orphan_and_no_manifest(self):
        original = self.blobs.put
        def lost(content):
            value = original(content)
            self.admitted = False
            return value
        with patch.object(self.blobs, "put", side_effect=lost):
            with self.assertRaises(LeaseLost):
                self.capture()
        self.assertEqual(self.files.physical_usage(), (1024, 1))
        self.assertEqual(self.query("SELECT * FROM qq_official_file_artifacts"), [])
        self.admitted = True
        with self.assertRaisesRegex(ArtifactError, "storage_quota"):
            self.capture(TWO)

    def test_symlink_inventory_is_rejected_without_following_external_data(self):
        with tempfile.TemporaryDirectory(prefix="TEST-quota-outside-") as directory:
            foreign = Path(directory) / "TEST-secret"
            foreign.write_bytes(b"TEST secret must not be read")
            (self.blobs.root / "staging" / "TEST-link").symlink_to(foreign)
            with self.assertRaisesRegex(ArtifactError, "storage_inventory"):
                self.capture()
            self.assertEqual(foreign.read_bytes(), b"TEST secret must not be read")
        self.assertEqual(list((self.blobs.root / "blobs").iterdir()), [])

    def test_inventory_stops_at_first_object_or_byte_excess(self):
        scandir = os.scandir
        for byte_size, expected in ((1, 3), (600, 2)):
            inspected = []
            def entries():
                for index in range(1000):
                    def info(*, follow_symlinks, i=index):
                        self.assertFalse(follow_symlinks)
                        inspected.append(i)
                        if len(inspected) > expected:
                            raise AssertionError("inventory scanned beyond first quota excess")
                        return SimpleNamespace(st_mode=stat.S_IFREG | 0o600,
                            st_uid=os.geteuid(), st_size=byte_size)
                    yield SimpleNamespace(stat=info)
            from contextlib import nullcontext
            def scan(path):
                return nullcontext(entries()) if Path(path).name == "staging" else scandir(path)
            with self.subTest(bytes=byte_size), patch("os.scandir", side_effect=scan):
                with self.assertRaisesRegex(ArtifactError, "storage_quota"):
                    self.files.physical_usage()
            self.assertEqual(len(inspected), expected)


if __name__ == "__main__":
    unittest.main()
