"""Exercise product ext4 image operations inside a disposable, unprivileged container.

Only private temporary regular files are formatted. Mount/loop mutation is
forbidden here. This proves utility compatibility and journal reconciliation,
NOT mounted hard quotas, host reboot, Docker bind mounts or production wiring.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import sys
import tempfile

from src.sandbox_runtime.project_volumes import LinuxVolumeHost, MIB, ProjectVolumes


class ImageOnlyHost(LinuxVolumeHost):
    def in_use(self, target):
        # This fixture has no Docker socket, network, capabilities or host mounts.
        # Check that its image is detached before allowing offline image work.
        if any(
            Path(row.get("back-file", "")).parent == target.parent
            for row in self.loops()
        ):
            raise RuntimeError("Image-only probe found a loop attachment")
        return False

    def attach(self, image):
        raise RuntimeError("Image-only fixture must never attach a loop device")


def run():
    if sys.platform != "linux" or os.geteuid() != 0 or not Path("/.dockerenv").exists():
        raise RuntimeError("Run only inside the disposable local container fixture")
    status = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
        if ":" in line
    )
    if int(status["CapEff"].strip(), 16) or Path("/var/run/docker.sock").exists():
        raise RuntimeError("Fixture must not have capabilities or the Docker socket")
    host = ImageOnlyHost()
    report = {"kind": "ext4 image utilities; no mount/host acceptance"}
    with tempfile.TemporaryDirectory(prefix="gaoji-m2-image-") as directory:
        root = Path(directory) / "volumes"
        instance = secrets.token_hex(16)
        volumes = ProjectVolumes(root, instance, host=host)
        report["created"] = volumes.create("fixture", 1, quota_bytes=64 * MIB, fence=1)
        image = next((root / "p1").glob("*.ext4"))
        sentinel = Path(directory) / "proof.txt"
        sentinel.write_text("retained-project-proof\n")
        host.run("debugfs", "-w", "-R", f"write {sentinel} /proof.txt", str(image))
        report["grown"] = volumes.mutate(
            "fixture", 1, "resize", fence=2, quota_bytes=96 * MIB
        )
        report["shrunk"] = volumes.mutate(
            "fixture", 1, "resize", fence=3, quota_bytes=64 * MIB
        )
        volumes.mutate("fixture", 1, "fsck", fence=4)
        proof = host.run("debugfs", "-R", "cat /proof.txt", str(image))
        if proof.strip() != "retained-project-proof":
            raise RuntimeError("Sentinel content changed after grow/shrink/fsck")
        report["sentinel_preserved"] = True
        original_resize = host.resize

        def interrupted(image_or_device, size):
            original_resize(image_or_device, size)
            raise InterruptedError("simulated process interruption after resize")

        host.resize = interrupted
        try:
            volumes.mutate("fixture", 1, "resize", fence=5, quota_bytes=80 * MIB)
        except InterruptedError:
            pass
        else:
            raise RuntimeError("Resize interruption was not exercised")
        host.resize = original_resize
        volumes = ProjectVolumes(root, instance, host=host)
        report["resumed"] = volumes.mutate(
            "fixture", 1, "resize", fence=5, quota_bytes=80 * MIB
        )
        report["deleted"] = volumes.mutate("fixture", 1, "delete", fence=6)
        if image.exists() or not report["deleted"].get("deleted"):
            raise RuntimeError("Fixture image cleanup was not confirmed")
    report["temporary_root_removed"] = not root.exists()
    return report


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
