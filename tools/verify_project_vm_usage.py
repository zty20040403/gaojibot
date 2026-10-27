"""Real ext4/QEMU accounting in a disposable container, without mounts or KVM."""

import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import uuid

from src.sandbox_runtime.project_vm_usage import qcow_used_bytes
from src.sandbox_runtime.project_volume_journal import ProjectVolumeError


def run(*args, timeout=30):
    return subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "LC_ALL": "C"},
    ).stdout


def verify():
    if sys.platform != "linux" or not Path("/.dockerenv").exists():
        raise RuntimeError("Use only a disposable local container")
    fields = dict(
        row.split(":", 1) for row in Path("/proc/self/status").read_text().splitlines()
    )
    if int(fields["CapEff"], 16) or Path("/var/run/docker.sock").exists():
        raise RuntimeError("No capabilities or Docker socket may be present")
    if any(
        name != "lo" and int(Path(f"/sys/class/net/{name}/flags").read_text(), 16) & 1
        for _, name in socket.if_nameindex()
    ):
        raise RuntimeError("Disconnect fixture networking before verification")
    report = {
        "scope": "offline ext4/qcow2 accounting only; no native KVM acceptance",
        "cases": [],
    }
    with tempfile.TemporaryDirectory(prefix="gaoji-vm-usage-proof-") as directory:
        root = Path(directory)
        for block, reserved in ((1024, 0), (4096, 5)):
            image, qcow = root / "project.ext4", root / "project.qcow2"
            quota, identity = 64 * 1024**2, str(uuid.uuid4())
            with image.open("wb") as stream:
                stream.truncate(quota)
            run(
                "mkfs.ext4",
                "-q",
                "-F",
                "-b",
                str(block),
                "-m",
                str(reserved),
                "-U",
                identity,
                str(image),
            )
            used = []
            for populated in (False, True):
                if populated:
                    proof = root / "retained.txt"
                    proof.write_bytes(b"retained-project-proof\n" * 5000)
                    run(
                        "debugfs",
                        "-w",
                        "-R",
                        f"write {proof} /retained.txt",
                        str(image),
                    )
                header = run("dumpe2fs", "-h", str(image))
                counters = dict(
                    line.split(":", 1) for line in header.splitlines() if ":" in line
                )
                if counters["Filesystem state"].strip() != "clean":
                    raise RuntimeError("Proof filesystem is not clean")
                expected = (
                    quota
                    - max(
                        0,
                        int(counters["Free blocks"])
                        - int(counters["Reserved block count"]),
                    )
                    * block
                )
                run(
                    "qemu-img",
                    "convert",
                    "-f",
                    "raw",
                    "-O",
                    "qcow2",
                    str(image),
                    str(qcow),
                )
                with qcow.open("rb") as stream:
                    before = hashlib.file_digest(stream, "sha256").hexdigest()
                used.append(
                    qcow_used_bytes(
                        qcow, filesystem_uuid=identity, quota_bytes=quota, run=run
                    )
                )
                with qcow.open("rb") as stream:
                    after = hashlib.file_digest(stream, "sha256").hexdigest()
                if used[-1] != expected or before != after:
                    raise RuntimeError(
                        "QEMU usage differs from ext4 or modified its source"
                    )
                if populated:
                    retained = run("debugfs", "-R", "cat /retained.txt", str(image))
                    if retained.encode() != proof.read_bytes():
                        raise RuntimeError("Retained proof file changed")
                qcow.unlink()
            if used[1] <= used[0]:
                raise RuntimeError("Added file was not reflected in usage")
            run("debugfs", "-w", "-R", "set_super_value state 0", str(image))
            run(
                "qemu-img", "convert", "-f", "raw", "-O", "qcow2", str(image), str(qcow)
            )
            try:
                qcow_used_bytes(
                    qcow, filesystem_uuid=identity, quota_bytes=quota, run=run
                )
            except ProjectVolumeError:
                pass
            else:
                raise RuntimeError("Dirty filesystem accounting was accepted")
            qcow.unlink()
            image.unlink()
            report["cases"].append(
                {
                    "block_size": block,
                    "reserved_percent": reserved,
                    "empty_used_bytes": used[0],
                    "populated_used_bytes": used[1],
                    "dirty_rejected": True,
                    "source_unchanged": True,
                }
            )
    report["temporary_root_removed"] = not root.exists()
    return report


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2))
