"""Bounded, read-only accounting of a stopped project's whole-device ext4 disk.

Layout: https://www.kernel.org/doc/html/latest/filesystems/ext4/super.html
No mounts, filesystem repairs, force-share reads or full raw-image copies.
"""

from pathlib import Path
import struct
import tempfile
import uuid

from .project_volume_journal import ProjectVolumeError


def crc32c(data):
    """ext4's seeded CRC32C, without the conventional final complement."""
    value = 0xFFFFFFFF
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0x82F63B78 if value & 1 else 0)
    return value


def ext4_used_bytes(data, *, filesystem_uuid, quota_bytes):
    if len(data) != 1024:
        raise ProjectVolumeError("Incomplete project filesystem superblock")

    def u32(offset):
        return struct.unpack_from("<I", data, offset)[0]

    incompat, ro = u32(0x60), u32(0x64)
    log = u32(0x18)
    if (
        struct.unpack_from("<H", data, 0x38)[0] != 0xEF53
        or struct.unpack_from("<H", data, 0x3A)[0] != 1
        or u32(0x4C) != 1
        or str(uuid.UUID(bytes=bytes(data[0x68:0x78]))) != filesystem_uuid
        # Supported project format: filetype, extents, 64bit, flex_bg, csum_seed.
        or incompat & ~(0x2 | 0x40 | 0x80 | 0x200 | 0x2000)
        or not ro & 0x400
        or ro & (0x200 | 0x10000)
        or data[0x175] != 1
        or u32(0x3FC) != crc32c(data[:0x3FC])
        or u32(0xE4)
        or u32(0xE8)
        or log > 6
        or u32(0x1C) != log
    ):
        raise ProjectVolumeError(
            "Project filesystem is dirty, unsupported or unverified"
        )
    counts = []
    for low, high in ((0x4, 0x150), (0x8, 0x154), (0xC, 0x158)):
        if not incompat & 0x80 and u32(high):
            raise ProjectVolumeError("Invalid project filesystem block counters")
        counts.append(u32(low) + ((u32(high) << 32) if incompat & 0x80 else 0))
    total, reserved, free = counts
    block = 1 << (10 + log)
    if (
        type(quota_bytes) is not int
        or quota_bytes <= 0
        or total * block != quota_bytes
        or reserved > total
        or free > total
    ):
        raise ProjectVolumeError("Project filesystem geometry differs from its quota")
    return quota_bytes - max(0, free - reserved) * block


def qcow_used_bytes(path, *, filesystem_uuid, quota_bytes, run):
    # Caller holds allocation, VM IO and disk journal locks and verifies that
    # the domain is stopped/absent. QEMU still takes its own non-shared lock.
    with tempfile.TemporaryDirectory(prefix="gaoji-project-usage-") as directory:
        output = Path(directory) / "superblock"
        run(
            "qemu-img",
            "dd",
            "-f",
            "qcow2",
            "-O",
            "raw",
            "bs=1024",
            "count=2",
            f"if={path}",
            f"of={output}",
            timeout=10,
        )
        with output.open("rb") as stream:
            data = stream.read(2049)
        if len(data) != 2048:
            raise ProjectVolumeError("Incomplete project filesystem prefix")
        return ext4_used_bytes(
            data[1024:], filesystem_uuid=filesystem_uuid, quota_bytes=quota_bytes
        )
