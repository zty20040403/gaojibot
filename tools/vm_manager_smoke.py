"""Exercise the bot VM manager against a disposable guest on a KVM host."""

from __future__ import annotations

import argparse
import asyncio
import io
import secrets
import zipfile
from pathlib import Path

from src.plugins.ai_chat.vm_sandbox import VmSandboxManager


class SmokeVmManager(VmSandboxManager):
    async def _wait_guest(self, sandbox_id: str, *, timeout: int = 300) -> None:
        await super()._wait_guest(sandbox_id, timeout=min(timeout, 100))


async def smoke(image: Path) -> None:
    root = Path.home() / ("gaoji-vm-manager-smoke-" + secrets.token_hex(4))
    manager = SmokeVmManager(
        image=str(image), root=str(root), max_total=1, max_per_owner=1,
        memory_mib=2048, disk_gib=8,
    )
    owner = "smoke:isolated"
    sandbox_id = ""
    try:
        created = await manager.create(owner, "python")
        sandbox_id = created["sandbox_id"]
        resumed = SmokeVmManager(
            image=str(image), root=str(root), max_total=1, max_per_owner=1,
            memory_mib=2048, disk_gib=8,
        )
        assert any(item["sandbox_id"] == sandbox_id for item in await resumed.list(owner))
        assert await resumed.write_file(owner, sandbox_id, "demo/note.txt", b"vm-file\n") == 8
        assert await resumed.read_file(owner, sandbox_id, "demo/note.txt") == b"vm-file\n"
        result = await resumed.exec(owner, sandbox_id, "cat demo/note.txt")
        assert result.returncode == 0 and result.stdout == "vm-file\n", result
        assert result.manifest and result.manifest.network_mode == "user-nat"
        await resumed.install_readonly_file(owner, sandbox_id, "upstream/proof.txt", b"upstream\n")
        result = await resumed.exec(owner, sandbox_id, "printf changed > upstream/proof.txt")
        assert result.returncode != 0, "readonly handoff was writable"
        data, directory = await resumed.export_artifact(owner, sandbox_id, "demo")
        assert directory
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert archive.read("demo/note.txt") == b"vm-file\n"
        await resumed.stop_owned(owner, sandbox_id)
        assert not any(item["status"].startswith("Up ") for item in await resumed.list(owner))
        await resumed._virsh("undefine", resumed._name(sandbox_id))
        await resumed.start_owned(owner, sandbox_id)
        assert await resumed.read_file(owner, sandbox_id, "demo/note.txt") == b"vm-file\n"
        print("VM manager smoke passed: create, command, files, handoff, export, stop/undefine/recover")
    finally:
        if sandbox_id:
            try:
                await manager.destroy(owner, sandbox_id)
            except Exception as exc:
                print(f"VM cleanup needs attention: {sandbox_id} in {root}: {exc}")
                raise
        if root.exists() and not any(root.iterdir()):
            root.rmdir()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(smoke(args.image.resolve(strict=True)))
