"""M2 product-helper acceptance inside a marked disposable native Linux VM.

Run prepare, power off the guest, boot the same disk, then run recover. There
are no production credentials or host mounts. The fixture image exercises the
actual driver/storage/RPC code, not the Nix package installation workflow.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.local_runtime.files import read_json, write_json  # noqa: E402
from src.sandbox_runtime.client import SandboxClient  # noqa: E402
from src.sandbox_runtime.docker import DockerSandboxManager, SandboxError  # noqa: E402
from src.sandbox_runtime.project_volumes import LinuxVolumeHost, MIB, ProjectVolumes  # noqa: E402
from src.sandbox_runtime.server import SandboxRunner  # noqa: E402

ROOT = Path("/var/tmp/gaoji-m2-product")
PROOF = b"project data survives an actual guest reboot\n"


def command(*args, timeout=180):
    return subprocess.run(
        args, check=True, capture_output=True, text=True, timeout=timeout
    ).stdout.strip()


def docker(*args, **kwargs):
    return command("docker", "--host=unix:///var/run/docker.sock", *args, **kwargs)


def environment():
    assert os.geteuid() == 0
    assert Path("/etc/gaoji-m0-disposable").read_text().strip() == "gaoji-m0"
    assert not Path("/run/systemd/container").exists()
    assert command("systemd-detect-virt", "--vm") in {"qemu", "kvm", "apple"}
    assert Path("/var/run/docker.sock").is_socket()


def fixture_image():
    context = Path(__file__).resolve().parent / "project-worker"
    digest = hashlib.sha256(
        b"".join(path.read_bytes() for path in sorted(context.iterdir()))
    ).hexdigest()
    image = "gaoji-app-sandbox:" + digest
    docker(
        "build",
        "--pull=false",
        "--network=none",
        "-t",
        image,
        str(context),
        timeout=300,
    )
    return image


async def checked_exec(client, sandbox_id, script):
    result = await client.exec(
        "probe", sandbox_id, "python -c " + shlex.quote(script), timeout_seconds=60
    )
    if result.returncode:
        raise RuntimeError(result.stderr + result.stdout)
    return result.stdout


async def run(phase):
    environment()
    host = LinuxVolumeHost()
    if phase == "prepare":
        ROOT.mkdir(mode=0o700)
        config = {
            "instance_id": secrets.token_hex(16),
            "token": secrets.token_urlsafe(32),
            "image": fixture_image(),
            "boot_id": host.boot_id(),
        }
        write_json(ROOT / "probe.json", config)
    else:
        config = read_json(ROOT / "probe.json")
        assert config["boot_id"] != host.boot_id(), "A real guest reboot is required"
    state = ROOT / "state"
    state.mkdir(mode=0o700, exist_ok=True)
    manager = DockerSandboxManager(
        namespace=config["instance_id"],
        image=config["image"],
        network="none",
        memory_mib=256,
        cpus=1,
        max_file_bytes=4 * MIB,
    )
    volumes = ProjectVolumes(state / "project-volumes", manager.namespace, host=host)
    runner = SandboxRunner(
        manager,
        socket_path=ROOT / "socket/runner.sock",
        state_directory=state,
        token=config["token"],
        project_volumes=volumes,
    )
    client = SandboxClient(
        str(ROOT / "socket/runner.sock"), config["token"], max_file_bytes=4 * MIB
    )
    report = {"phase": phase, "boot_id": host.boot_id(), "image": config["image"]}
    await runner.start()
    try:
        if phase == "prepare":
            ids = {}
            for project_id in (1, 2):
                await client.project_volume(
                    "create", "probe", project_id, quota_bytes=64 * MIB, fence=1
                )
                await client.project_volume("mount", "probe", project_id, fence=2)
                created = await client.project_workspace(
                    "start", "probe", project_id, generation=1, fence=3
                )
                assert created["verified"]
                ids[str(project_id)] = created["sandbox_id"]
                await client.write_file(
                    "probe", created["sandbox_id"], "proof.txt", PROOF
                )
            fill = """import errno,json,os
count=0
try:
    with open('/workspace/fill', 'wb', buffering=0) as f:
        while True:
            count += f.write(b'x' * 1048576)
except OSError as exc:
    if exc.errno != errno.ENOSPC: raise
    print(json.dumps({'errno':exc.errno, 'bytes':count}))
"""
            report["quota"] = json.loads(await checked_exec(client, ids["1"], fill))
            assert report["quota"]["errno"] == 28
            assert await client.read_file("probe", ids["2"], "proof.txt") == PROOF
            await client.write_file(
                "probe", ids["2"], "independent.bin", b"y" * MIB, allow_large=True
            )
            await checked_exec(
                client, ids["1"], "import os; os.unlink('/workspace/fill')"
            )
            report["independent_project_verified"] = True
            original = volumes.finish_workspace

            def interrupt(*args, **kwargs):
                raise RuntimeError("intentional loss before durable completion")

            volumes.finish_workspace = interrupt
            try:
                try:
                    await client.project_workspace(
                        "start", "probe", 1, generation=1, fence=4
                    )
                except SandboxError:
                    pass
                else:
                    raise RuntimeError("Fault injection did not interrupt completion")
            finally:
                volumes.finish_workspace = original
            assert volumes.status("probe", 1)["pending"]
            config.update(
                sandbox_ids=ids,
                filesystems={
                    str(pid): read_json(volumes.root / f"p{pid}/volume.json")["fs_uuid"]
                    for pid in (1, 2)
                },
            )
            write_json(ROOT / "probe.json", config)
            report["pending_start_retained"] = True
            command("sync")
        else:
            recovered = []
            for project_id in (1, 2):
                before = await client.project_workspace(
                    "status", "probe", project_id, generation=1
                )
                assert before["state"] == "needs_recovery" and not before["verified"]
                result = await client.project_workspace(
                    "start", "probe", project_id, generation=1, fence=4
                )
                assert (
                    result["verified"]
                    and result["sandbox_id"] != config["sandbox_ids"][str(project_id)]
                )
                record = read_json(volumes.root / f"p{project_id}/volume.json")
                assert record["fs_uuid"] == config["filesystems"][str(project_id)]
                assert (
                    await client.read_file("probe", result["sandbox_id"], "proof.txt")
                    == PROOF
                )
                recovered.append(result)
            assert (
                len(
                    docker(
                        "ps",
                        "-aq",
                        "--filter",
                        "label=gaoji.app.instance=" + manager.namespace,
                    ).splitlines()
                )
                == 2
            )
            report["recovered"] = recovered
            report["sentinel_sha256"] = hashlib.sha256(PROOF).hexdigest()
            report["reboot_recovery_verified"] = True
            for project_id in (1, 2):
                await client.project_workspace(
                    "stop", "probe", project_id, generation=1, fence=5
                )
                await client.project_workspace(
                    "remove", "probe", project_id, generation=1, fence=6
                )
                await client.project_volume("trim", "probe", project_id, fence=7)
                await client.project_volume("unmount", "probe", project_id, fence=8)
                await client.project_volume("fsck", "probe", project_id, fence=9)
                assert (
                    await client.project_volume("delete", "probe", project_id, fence=10)
                )["deleted"]
            assert not docker(
                "ps", "-aq", "--filter", "label=gaoji.app.instance=" + manager.namespace
            )
            assert not any(
                row.get("back-file", "").startswith(str(ROOT)) for row in host.loops()
            )
            assert not any(
                row.get("target", "").startswith(str(ROOT)) for row in host.mounts()
            )
            labels = json.loads(
                docker(
                    "volume",
                    "inspect",
                    "--format",
                    "{{json .Labels}}",
                    manager.nix_cache_volume,
                )
            )
            assert labels == {
                "gaoji.app.instance": manager.namespace,
                "gaoji.app.resource": "nix-cache",
            }
            docker("volume", "rm", manager.nix_cache_volume)
            docker("image", "rm", config["image"])
    finally:
        await runner.close()
    if phase == "recover":
        shutil.rmtree(ROOT)
        report["owned_resources_removed"] = not ROOT.exists()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("prepare", "recover"), required=True)
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args.phase))
    except Exception as exc:
        print(
            json.dumps({"phase": args.phase, "error": f"{type(exc).__name__}: {exc}"})
        )
        raise
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
