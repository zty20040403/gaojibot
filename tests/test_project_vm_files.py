"""Real retained files and journal retries; guest mounts/libvirt are fixtures."""

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
import zipfile

from src.project_content import manifest_bytes
from src.sandbox_runtime.errors import SandboxError
from src.sandbox_runtime.project_vm_file_bundle import MODULES, build_bundle
from src.sandbox_runtime.project_vm_file_guest import ProjectGuestFiles, TRANSFER_BYTES
from src.sandbox_runtime.project_vm_files import VmProjectFiles
from src.sandbox_runtime.project_vm_gate import GuestDiskError, LinuxGuestCommands, ProjectGuestGate
from tests.test_project_vm_guest import binding
from tests import test_project_vm_mounts as mounts

RESERVATION = mounts.RESERVATION


class GuestFixture:
    def __init__(self, root, config=None):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.config = config or binding().guest_config()
        self.commands = Mock(spec=LinuxGuestCommands)
        self.commands.empty.return_value = True
        self.commands.sandbox_processes.return_value = False
        self.gate = ProjectGuestGate(self.config, state_path=self.root / "gate.json", lock_root=self.root,
                                     commands=self.commands, workspace=Mock())
        self.reopen()

    def reopen(self):
        self.files = ProjectGuestFiles(self.gate, root=self.root / "journal", workspace=self.workspace)

    def request(self, action, fence=6, reservation=RESERVATION, payload=b"", **parameters):
        return {"identity": self.gate.identity, "action": action, "fence": fence,
                "reservation": reservation, "parameters": parameters,
                "payload": base64.b64encode(payload).decode("ascii")}

    def call(self, action, **kwargs):
        result = self.files.operate(self.request(action, **kwargs))
        return result["receipt"], base64.b64decode(result["payload"], validate=True)

    def record(self):
        return json.loads((self.root / "journal" / f"p{self.config['project_id']}" / "volume.json").read_text())


class GuestFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gaoji-vm-files-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = GuestFixture(self.root)
        self.fixture.gate.quiesce(5)

    def test_snapshot_binary_pagination_and_fresh_checks(self):
        f = self.fixture
        for index in range(130):
            (f.workspace / f"source-{index:03}.txt").write_text(f"line {index}")
        data = bytes(range(256)) * 401
        (f.workspace / "z.bin").write_bytes(data)
        receipt, _ = f.call("snapshot")
        digest = receipt["manifest_sha256"]
        entries = []
        while len(entries) < receipt["file_count"]:
            page, _ = f.call("page", index=len(entries), manifest_sha256=digest)
            entries.extend(page["files"])
        self.assertEqual(len(entries), 131)
        self.assertEqual(hashlib.sha256(manifest_bytes({"format": 1, "files": entries}, quota_bytes=f.config["quota_bytes"])).hexdigest(), digest)
        content = bytearray()
        for offset in range(0, len(data), TRANSFER_BYTES):
            _, chunk = f.call("read", index=130, offset=offset, manifest_sha256=digest)
            content.extend(chunk)
        self.assertEqual(content, data)
        f.reopen()
        self.assertEqual(f.call("snapshot")[0], receipt)
        f.call("check", manifest_sha256=digest)
        (f.workspace / "z.bin").write_bytes(b"changed")
        with self.assertRaises(SandboxError):
            f.call("read", index=130, offset=0, manifest_sha256=digest)
        with self.assertRaises(SandboxError):
            f.call("check", manifest_sha256=digest)

    def restore_fixture(self):
        data = b"#!/bin/sh\necho retained\n" * 3000
        document = {"format": 1, "files": [
            {"path": "empty", "sha256": hashlib.sha256(b"").hexdigest(), "size": 0, "mode": 0o644},
            {"path": "src/run.sh", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mode": 0o755},
        ]}
        encoded = manifest_bytes(document, quota_bytes=self.fixture.config["quota_bytes"])
        digest = hashlib.sha256(encoded).hexdigest()
        return data, encoded, digest

    def test_restore_resumes_after_lost_chunk_and_completion_receipts(self):
        f = self.fixture
        data, manifest, digest = self.restore_fixture()
        options = {"manifest_sha256": digest}
        f.call("restore_begin", manifest_size=len(manifest), **options)
        f.call("restore_manifest", offset=0, payload=manifest, **options)
        f.call("restore_file", index=0, offset=0, payload=b"", **options)
        f.call("restore_file", index=1, offset=0, payload=data[:TRANSFER_BYTES], **options)
        f.reopen()
        self.assertFalse(f.call("restore_begin", manifest_size=len(manifest), **options)[0]["complete"])
        with self.assertRaises(SandboxError):
            f.call("restore_finish", **options)
        self.assertTrue(f.record()["pending"])
        # A conflicting retry cannot corrupt the accepted prefix.
        with self.assertRaises(SandboxError):
            f.call("restore_file", index=1, offset=0, payload=b"WRONG", **options)
        for offset in range(0, len(data), TRANSFER_BYTES):
            f.call("restore_file", index=1, offset=offset, payload=data[offset:offset + TRANSFER_BYTES], **options)
        save = f.files.volumes._save
        def lost(directory, record):
            save(directory, record)
            if not record["pending"]:
                raise OSError("lost completion reply")
        with patch.object(f.files.volumes, "_save", side_effect=lost), self.assertRaises(OSError):
            f.call("restore_finish", **options)
        f.reopen()
        self.assertTrue(f.call("restore_begin", manifest_size=len(manifest), **options)[0]["complete"])
        f.call("restore_check", **options)
        self.assertEqual((f.workspace / "src/run.sh").read_bytes(), data)
        self.assertEqual((f.workspace / "src/run.sh").stat().st_mode & 0o777, 0o755)
        with self.assertRaises(SandboxError):
            f.call("restore_file", index=1, offset=0, payload=data[:1], **options)

    def test_restore_does_not_overwrite_existing_data_or_escape_root(self):
        f = self.fixture
        (f.workspace / "keep.txt").write_bytes(b"keep")
        _, manifest, digest = self.restore_fixture()
        with self.assertRaises(SandboxError):
            f.call("restore_begin", manifest_size=len(manifest), manifest_sha256=digest)
        self.assertEqual((f.workspace / "keep.txt").read_bytes(), b"keep")
        (f.workspace / "keep.txt").unlink()
        f.call("restore_begin", manifest_size=len(manifest), manifest_sha256=digest)
        f.call("restore_manifest", offset=0, payload=manifest, manifest_sha256=digest)
        (f.workspace / "src").symlink_to(self.root)
        with self.assertRaises(OSError):
            f.call("restore_file", index=1, offset=0, payload=b"bad", manifest_sha256=digest)
        self.assertFalse((self.root / "run.sh").exists())

    def test_pending_restore_does_not_lose_epoch_on_rejected_new_operation(self):
        f = self.fixture
        _, manifest, digest = self.restore_fixture()
        f.call("restore_begin", manifest_size=len(manifest), manifest_sha256=digest)
        with self.assertRaises(SandboxError):
            f.call("snapshot", fence=7)
        f.call("restore_manifest", offset=0, payload=manifest, manifest_sha256=digest)
        self.assertEqual(f.record()["fence"], 6)
        with self.assertRaises(SandboxError):
            f.call("restore_begin", reservation="c" * 32, manifest_size=len(manifest), manifest_sha256=digest)

    def test_guest_gate_blocks_active_stale_or_unverified_file_operations(self):
        f = self.fixture
        f.gate.activate(6)
        with self.assertRaises(GuestDiskError):
            f.call("snapshot", fence=7)
        f.gate.quiesce(7)
        with self.assertRaises(GuestDiskError):
            f.call("snapshot", fence=6)
        f.commands.sandbox_processes.return_value = True
        with self.assertRaises(GuestDiskError):
            f.call("snapshot", fence=8)
        f.commands.sandbox_processes.return_value = False
        f.gate.workspace.verify.side_effect = GuestDiskError("wrong mount")
        with self.assertRaises(GuestDiskError):
            f.call("snapshot", fence=8)
        self.assertFalse((f.root / "journal" / f"p{f.config['project_id']}" / "volume.json").exists())

    def test_wrong_identity_ranges_and_unexpected_fields_rejected(self):
        f = self.fixture
        requests = []
        for change in ({"project_id": True}, {"generation": f.config["generation"] + 1}, {"serial": "other"}):
            request = f.request("snapshot")
            request["identity"] = {**request["identity"], **change}
            requests.append(request)
        requests.extend([
            f.request("snapshot", fence=True), f.request("snapshot", payload=b"data"),
            f.request("snapshot", path="/etc"),
            f.request("read", manifest_sha256="a" * 64, index=-1, offset=0),
            f.request("read", manifest_sha256="a" * 64, index=0, offset=True),
            f.request("read", manifest_sha256="a" * 64, index=0, offset=0, max_bytes=TRANSFER_BYTES + 1),
        ])
        for request in requests:
            with self.subTest(request=request), self.assertRaises((SandboxError, GuestDiskError)):
                f.files.operate(request)
        self.assertFalse((f.root / "journal" / f"p{f.config['project_id']}" / "volume.json").exists())

    def test_special_files_are_not_exported(self):
        f = self.fixture
        secret = self.root / "outside"
        secret.write_bytes(b"secret")
        target = f.workspace / "unsafe"
        for kind in ("symlink", "hardlink", "fifo"):
            if kind == "symlink":
                target.symlink_to(secret)
            elif kind == "hardlink":
                os.link(secret, target)
            else:
                os.mkfifo(target)
            with self.subTest(kind=kind), self.assertRaises(SandboxError):
                f.call("snapshot")
            target.unlink()
        self.assertEqual(f.call("snapshot")[0]["file_count"], 0)

    def test_prune_only_frozen_rebuildable_roots_and_retry(self):
        f = self.fixture
        (f.workspace / "package.json").write_text('{"name":"sample"}')
        (f.workspace / "package-lock.json").write_text('{"lockfileVersion":3}')
        (f.workspace / "src").mkdir()
        (f.workspace / "src/main.js").write_bytes(b"retained source")
        (f.workspace / "node_modules").mkdir()
        (f.workspace / "node_modules/dependency").write_bytes(b"rebuild me")
        snapshot, _ = f.call("snapshot", rebuild_command="npm ci")
        options = {"manifest_sha256": snapshot["manifest_sha256"], "snapshot_fence": 6, "fence": 7}
        self.assertEqual(snapshot["file_count"], 3)
        self.assertEqual(f.call("prune", **options)[0]["pruned_roots"], 1)
        f.reopen()
        f.call("prune", **options)
        f.call("prune_check", **options)
        self.assertFalse((f.workspace / "node_modules").exists())
        self.assertEqual((f.workspace / "src/main.js").read_bytes(), b"retained source")

    def test_bundle_is_deterministic_isolated_and_has_no_host_drivers(self):
        package = build_bundle()
        self.assertEqual(package, build_bundle())
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            self.assertEqual(set(archive.namelist()), {*MODULES, "__main__.py", "src/__init__.py",
                                                     "src/local_runtime/__init__.py", "src/sandbox_runtime/__init__.py"})
            for name in MODULES:
                compile(archive.read(name), name, "exec")
        target = self.root / "helper.pyz"
        target.write_bytes(package)
        script = ("import sys; sys.path.insert(0,sys.argv[1]); "
                  "from src.sandbox_runtime.project_vm_file_guest import ProjectGuestFiles; "
                  "assert 'nonebot' not in sys.modules; assert 'src.sandbox_runtime.docker' not in sys.modules; "
                  "assert 'src.sandbox_runtime.project_volumes' not in sys.modules")
        subprocess.run([sys.executable, "-I", "-c", script, str(target)], check=True, cwd=self.root,
                       capture_output=True, timeout=15)


class VmFileBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.holder = mounts.MountBridgeTests()
        await self.holder.asyncSetUp()
        self.addCleanup(self.holder.fixture.doCleanups)
        await self.holder.mount()
        self.manager, self.bridge = self.holder.manager, self.holder.bridge
        self.fixture = GuestFixture(self.holder.fixture.root.parent / "guest", self.holder.fixture.binding.guest_config())
        self.fixture.gate.quiesce(5)
        self.client = VmProjectFiles(self.bridge)
        self.manager._guest_exec_raw = AsyncMock(side_effect=self.guest)

    async def guest(self, sid, path, args, *, input_bytes, timeout):
        self.assertEqual(path, "/usr/bin/python3")
        self.assertEqual(args, ["-I", "/usr/local/lib/gaoji/project_files.pyz"])
        result = self.fixture.files.operate(json.loads(input_bytes))
        return 0, json.dumps(result).encode(), b""

    async def call(self, action, fence=6, **kwargs):
        return await self.client.project_files(action, "scope", 1, generation=1, fence=fence,
                                                reservation=RESERVATION, **kwargs)

    async def test_host_snapshot_and_binary_stream_share_guest_protocol(self):
        data = b"test\0\xff" * 8000
        (self.fixture.workspace / "output.bin").write_bytes(data)
        result, _ = await self.call("snapshot")
        options = {"manifest_sha256": result["manifest_sha256"]}
        _, output = await self.call("read", index=0, offset=0, **options)
        self.assertEqual(output, data)
        await self.call("check", **options)
        self.assertFalse(self.holder.fixture.record()["pending"])
        self.assertEqual(self.holder.fixture.record()["fence"], 6)
        self.assertTrue(self.holder.fixture.record()["maintenance"])

    async def test_lost_guest_receipt_replays_same_snapshot_and_blocks_release(self):
        original = self.guest
        async def lost(*args, **kwargs):
            await original(*args, **kwargs)
            raise SandboxError("QGA disconnected after completion")
        self.manager._guest_exec_raw.side_effect = lost
        with self.assertRaises(SandboxError):
            await self.call("snapshot")
        self.assertTrue(self.holder.fixture.record()["pending"])
        with self.assertRaises(SandboxError):
            await self.bridge.reserve("release", "scope", 1, generation=1, fence=7, reservation=RESERVATION)
        with self.assertRaises(SandboxError):
            await self.call("snapshot", fence=7)
        self.client = VmProjectFiles(self.bridge)
        self.fixture.reopen()
        self.manager._guest_exec_raw.side_effect = self.guest
        self.assertFalse((await self.call("snapshot"))[0]["pending"])
        self.assertFalse(self.holder.fixture.record()["pending"])

    async def test_host_restore_stays_pending_until_complete_tree_is_verified(self):
        data = b"deliverable"
        encoded = manifest_bytes({"format": 1, "files": [{"path": "a.txt", "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(), "mode": 0o644}]}, quota_bytes=64 * 1024**2)
        options = {"manifest_sha256": hashlib.sha256(encoded).hexdigest()}
        await self.call("restore_begin", manifest_size=len(encoded), **options)
        self.assertTrue(self.holder.fixture.record()["pending"])
        await self.call("restore_manifest", offset=0, payload=encoded, **options)
        with self.assertRaises(SandboxError):
            await self.call("restore_finish", **options)
        await self.call("restore_file", index=0, offset=0, payload=data, **options)
        await self.call("restore_finish", **options)
        self.assertFalse(self.holder.fixture.record()["pending"])
        await self.call("restore_check", **options)

    async def test_invalid_receipt_and_foreign_domain_cannot_complete(self):
        original = self.guest
        async def bad(*args, **kwargs):
            code, out, err = await original(*args, **kwargs)
            result = json.loads(out)
            result["receipt"]["fence"] = True
            return code, json.dumps(result).encode(), err
        self.manager._guest_exec_raw.side_effect = bad
        with self.assertRaises(SandboxError):
            await self.call("snapshot")
        self.assertTrue(self.holder.fixture.record()["pending"])
        self.manager._guest_exec_raw.side_effect = self.guest
        self.holder.xml = self.holder.xml.replace(mounts.DOMAIN, mounts.OTHER)
        before = self.manager._guest_exec_raw.await_count
        with self.assertRaises(SandboxError):
            await self.call("snapshot")
        self.assertEqual(before, self.manager._guest_exec_raw.await_count)

    async def test_changed_receipt_cannot_cross_project_or_manifest(self):
        result, _ = await self.call("snapshot")
        self.manager._guest_exec_raw.reset_mock()
        for options in ({"manifest_sha256": "f" * 64}, {"manifest_sha256": result["manifest_sha256"], "fence": 7}):
            with self.assertRaises(SandboxError):
                await self.call("check", **options)
        self.manager._guest_exec_raw.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
