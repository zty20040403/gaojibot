"""Real file/RPC safety with simulated mounts and Docker, not native acceptance."""

import hashlib
import os
import unittest
from unittest.mock import patch

from src.project_content import CHUNK_BYTES, manifest_bytes
from src.sandbox_runtime.docker import SandboxError
from src.sandbox_runtime.protocol import validate_request
from tests import test_project_workspaces as fixture
from tests.test_sandbox_runner import request


class ProjectFileTransferTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ProjectWorkspaceTests.asyncSetUp
    start_runner = fixture.ProjectWorkspaceTests.start_runner
    stop_runner = fixture.ProjectWorkspaceTests.stop_runner
    operate = fixture.ProjectWorkspaceTests.operate
    journal = fixture.ProjectWorkspaceTests.journal

    async def reserve(self):
        self.path = self.volumes.binding("scope", 1)
        await self.client.project_workspace(
            "reserve", "scope", 1, generation=1, fence=3, reservation="a" * 32
        )

    async def call(self, action, **kwargs):
        return await self.client.project_files(
            action, "scope", 1, generation=1, fence=4, reservation="a" * 32, **kwargs
        )

    async def test_snapshot_pagination_and_binary_chunks_do_not_start_docker(self):
        await self.reserve()
        for index in range(132):
            (self.path / f"file-{index:03}.txt").write_bytes(str(index).encode())
        data = b"\x00\xff" * (CHUNK_BYTES + 17)
        (self.path / "z.dat").write_bytes(data)
        snapshot, _ = await self.call("snapshot")
        self.assertEqual(snapshot["file_count"], 133)
        digest = snapshot["manifest_sha256"]
        entries = []
        while len(entries) < snapshot["file_count"]:
            page, _ = await self.call(
                "page", index=len(entries), manifest_sha256=digest
            )
            entries.extend(page["files"])
        self.assertEqual(
            hashlib.sha256(
                manifest_bytes(
                    {"format": 1, "files": entries}, quota_bytes=64 * CHUNK_BYTES
                )
            ).hexdigest(),
            digest,
        )
        result = bytearray()
        for offset in range(0, len(data), CHUNK_BYTES):
            _, block = await self.call(
                "read", index=132, offset=offset, manifest_sha256=digest
            )
            result.extend(block)
        self.assertEqual(result, data)
        await self.call("check", manifest_sha256=digest)
        self.assertFalse(any(command[1] == "run" for command in self.docker.commands))

    async def test_snapshot_does_not_follow_links_or_accept_special_files(self):
        await self.reserve()
        secret = self.root / "host-secret"
        secret.write_bytes(b"never exported")
        target = self.path / "unsafe"
        for kind in ("symlink", "hardlink", "fifo"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    target.symlink_to(secret)
                elif kind == "hardlink":
                    os.link(secret, target)
                else:
                    os.mkfifo(target)
                try:
                    with self.assertRaises(SandboxError):
                        await self.call("snapshot")
                    self.assertEqual(secret.read_bytes(), b"never exported")
                finally:
                    target.unlink()
        result, _ = await self.call("snapshot")
        self.assertEqual(result["file_count"], 0)

    async def test_changed_or_added_file_invalidates_snapshot(self):
        await self.reserve()
        target = self.path / "file"
        target.write_bytes(b"old")
        result, _ = await self.call("snapshot")
        target.write_bytes(b"new")
        with self.assertRaises(SandboxError):
            await self.call(
                "read", index=0, offset=0, manifest_sha256=result["manifest_sha256"]
            )
        with self.assertRaises(SandboxError):
            await self.call("check", manifest_sha256=result["manifest_sha256"])

    async def test_running_container_cannot_be_snapshotted(self):
        await self.operate("start", 3)
        await self.client.project_workspace(
            "reserve", "scope", 1, generation=1, fence=4, reservation="a" * 32
        )
        with self.assertRaises(SandboxError):
            await self.client.project_files(
                "snapshot", "scope", 1, generation=1, fence=5, reservation="a" * 32
            )
        self.assertEqual(self.journal()["fence"], 4)

    def manifest(self, data):
        return {
            "format": 1,
            "files": [
                {
                    "path": "src/run.sh",
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                    "mode": 0o755,
                }
            ],
        }

    async def begin(self, manifest):
        body = manifest_bytes(manifest, quota_bytes=64 * CHUNK_BYTES)
        digest = hashlib.sha256(body).hexdigest()
        await self.call(
            "restore_begin", manifest_sha256=digest, manifest_size=len(body)
        )
        await self.call(
            "restore_manifest", manifest_sha256=digest, offset=0, payload=body
        )
        return digest

    async def test_restore_retries_partial_chunks_and_finishes_only_after_full_check(
        self,
    ):
        await self.reserve()
        data = b"#!/bin/sh\nprintf restored\n"
        digest = await self.begin(self.manifest(data))
        await self.call(
            "restore_file", manifest_sha256=digest, index=0, offset=0, payload=data[:10]
        )
        with self.assertRaises(SandboxError):
            await self.call("restore_finish", manifest_sha256=digest)
        await self.stop_runner()
        await self.start_runner()
        await self.call(
            "restore_file", manifest_sha256=digest, index=0, offset=0, payload=data
        )
        self.assertEqual((self.path / "src/run.sh").read_bytes(), data)
        self.assertEqual((self.path / "src/run.sh").stat().st_mode & 0o777, 0o755)
        for _ in range(2):
            result, _ = await self.call("restore_finish", manifest_sha256=digest)
            self.assertFalse(result["pending"])
        self.assertFalse((self.volumes.root / "p1/manifest.upload").exists())
        with self.assertRaises(SandboxError):
            await self.call(
                "restore_file", manifest_sha256=digest, index=0, offset=0, payload=data
            )

    async def test_restore_rejects_bad_bytes_order_or_foreign_files_without_deleting(
        self,
    ):
        await self.reserve()
        data = b"correct content"
        digest = await self.begin(self.manifest(data))
        with self.assertRaises(SandboxError):
            await self.call(
                "restore_file", manifest_sha256=digest, index=0, offset=3, payload=b"x"
            )
        await self.call(
            "restore_file", manifest_sha256=digest, index=0, offset=0, payload=data[:3]
        )
        with self.assertRaises(SandboxError):
            await self.call(
                "restore_file",
                manifest_sha256=digest,
                index=0,
                offset=0,
                payload=b"bad",
            )
        await self.call(
            "restore_file", manifest_sha256=digest, index=0, offset=0, payload=data
        )
        unexpected = self.path / "not-yours"
        unexpected.write_bytes(b"retain me")
        with self.assertRaises(SandboxError):
            await self.call("restore_finish", manifest_sha256=digest)
        self.assertEqual(unexpected.read_bytes(), b"retain me")
        self.assertTrue(self.journal()["pending"])

    async def test_restore_check_observes_changes_despite_cached_finish_receipt(self):
        await self.reserve()
        data = b"restore content"
        digest = await self.begin(self.manifest(data))
        with self.assertRaises(SandboxError):
            await self.call("restore_check", manifest_sha256=digest)
        await self.call(
            "restore_file", manifest_sha256=digest, index=0, offset=0, payload=data
        )
        finish_id, check_id = "b" * 32, "c" * 32
        await self.call("restore_finish", manifest_sha256=digest, request_id=finish_id)
        await self.call("restore_check", manifest_sha256=digest, request_id=check_id)
        (self.path / "src/run.sh").write_bytes(b"changed after completion")
        cached, _ = await self.call(
            "restore_finish", manifest_sha256=digest, request_id=finish_id
        )
        self.assertFalse(cached["pending"])
        with self.assertRaises(SandboxError):
            await self.call(
                "restore_check", manifest_sha256=digest, request_id=check_id
            )

    async def test_restore_begin_does_not_truncate_a_linked_private_upload(self):
        await self.reserve()
        original = self.root / "unrelated"
        original.write_bytes(b"preserve these bytes")
        original.chmod(0o600)
        os.link(original, self.volumes.root / "p1/manifest.upload")
        with self.assertRaises(SandboxError):
            await self.begin(self.manifest(b"body"))
        self.assertEqual(original.read_bytes(), b"preserve these bytes")

    async def test_restore_refuses_nonempty_volume_and_parent_symlink(self):
        await self.reserve()
        (self.path / "keep").write_bytes(b"original")
        with self.assertRaises(SandboxError):
            await self.begin(self.manifest(b"body"))
        self.assertEqual(self.journal()["fence"], 3)
        (self.path / "keep").unlink()
        digest = await self.begin(self.manifest(b"body"))
        outside = self.root / "outside"
        outside.mkdir()
        (self.path / "src").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(SandboxError):
            await self.call(
                "restore_file",
                manifest_sha256=digest,
                index=0,
                offset=0,
                payload=b"body",
            )
        self.assertEqual(list(outside.iterdir()), [])

    async def test_snapshot_interrupt_after_manifest_persist_reconciles_same_fence(
        self,
    ):
        await self.reserve()
        (self.path / "file").write_bytes(b"preserved")
        original = self.volumes._finish
        with patch.object(
            self.volumes, "_finish", side_effect=RuntimeError("lost ack")
        ):
            with self.assertRaises(SandboxError):
                await self.call("snapshot")
        self.assertTrue(self.journal()["pending"])
        await self.stop_runner()
        await self.start_runner()
        result, _ = await self.call("snapshot")
        self.assertFalse(result["pending"])
        self.assertEqual(result["file_count"], 1)
        self.assertEqual(self.volumes._finish, original)

    async def test_manifest_schema_and_protocol_are_bounded(self):
        for path in ("../secret", "/etc/passwd", "a/../x", "C:/secret", "a\\x"):
            with self.assertRaises(ValueError):
                manifest_bytes(
                    {
                        "format": 1,
                        "files": [{**self.manifest(b"x")["files"][0], "path": path}],
                    },
                    quota_bytes=1024,
                )
        args = {
            "owner": "scope",
            "project_id": 1,
            "generation": 1,
            "fence": 4,
            "reservation": "a" * 32,
            "manifest_sha256": "b" * 64,
            "index": 0,
            "offset": 0,
        }
        for changes in (
            {"index": -1},
            {"offset": True},
            {"path": "host"},
            {"manifest_sha256": "bad"},
        ):
            with self.assertRaises(SandboxError):
                validate_request(
                    request("project_files_read", {**args, **changes}),
                    file_limit=CHUNK_BYTES,
                )


if __name__ == "__main__":
    unittest.main()
