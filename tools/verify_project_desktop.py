"""Disposable local Docker Desktop soft-quota proof; never connects to servers.

Uses already-installed images, a fresh labelled volume and namespace. The only
fixture is an empty readonly Nix cache: this verifies storage and stopping real
writers, not package installation. All temporary containers/volumes are removed.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid


def docker(*args):
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=180
    )
    if result.returncode:
        raise RuntimeError(result.stderr[-7000:])
    return result.stdout.strip()


async def guest(instance, volume, image):
    from src.sandbox_runtime.docker import DockerSandboxManager, SandboxError
    from src.sandbox_runtime.project_desktop_workspaces import DesktopProjectWorkspaces
    from src.sandbox_runtime.project_directory import (
        DesktopVolumeHost,
        DirectoryProjectVolumes,
    )
    from src.sandbox_runtime.project_usage import ProjectUsage
    from src.sandbox_runtime.project_volumes import MIB

    root = Path("/fixture")
    host = DesktopVolumeHost(data_root=root, volume=volume)
    volumes = DirectoryProjectVolumes(root / "project-volumes", instance, host=host)
    manager = DockerSandboxManager(
        namespace=instance, image=image, network="none", max_file_bytes=MIB
    )
    workspaces = DesktopProjectWorkspaces(manager, volumes)
    manager.project_workspaces = workspaces
    docker(
        "volume",
        "create",
        "--label",
        "gaoji.project.proof=" + instance,
        manager.nix_cache_volume,
    )

    async def readonly_cache(_image):
        record = json.loads(docker("volume", "inspect", manager.nix_cache_volume))[0]
        assert record["Labels"]["gaoji.project.proof"] == instance

    manager._ensure_nix_volume = readonly_cache
    task = None
    try:
        identities = []
        for identifier in (1, 2):
            volumes.create("scope", identifier, quota_bytes=64 * MIB, fence=1)
            volumes.mutate("scope", identifier, "mount", fence=2)
            try:
                state = await workspaces.operate(
                    "start", "scope", identifier, generation=1, fence=3
                )
            except SandboxError:
                for cid in docker(
                    "ps", "-aq", "--filter", "label=gaoji.app.instance=" + instance
                ).splitlines():
                    row = json.loads(docker("inspect", cid))[0]
                    print(
                        json.dumps(
                            {
                                "Config": {
                                    key: row["Config"].get(key)
                                    for key in (
                                        "Image",
                                        "User",
                                        "Cmd",
                                        "Entrypoint",
                                        "Labels",
                                    )
                                },
                                "Mounts": row["Mounts"],
                                "HostConfig": {
                                    key: row["HostConfig"].get(key)
                                    for key in (
                                        "Mounts",
                                        "ReadonlyRootfs",
                                        "Privileged",
                                        "CapAdd",
                                        "CapDrop",
                                        "SecurityOpt",
                                        "NetworkMode",
                                        "Memory",
                                        "MemorySwap",
                                        "NanoCpus",
                                        "PidsLimit",
                                    )
                                },
                            }
                        ),
                        file=sys.stderr,
                    )
                raise
            identities.append(state["sandbox_id"])
        first = volumes.binding("scope", 1)
        (first / "retained.txt").write_text("keep my project\n")
        os.chown(first / "retained.txt", 1000, 1000)
        (root / "private-sentinel").write_text("not visible to sandbox")
        check = await manager.exec(
            "scope",
            identities[0],
            "test ! -e /fixture/private-sentinel; cat /workspace/retained.txt",
        )
        assert check.returncode == 0 and "keep my project" in check.stdout
        task = asyncio.create_task(
            manager.exec(
                "scope",
                identities[0],
                "head -c 83886080 /dev/zero > /workspace/fill; sleep 120",
                timeout_seconds=150,
            )
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if (first / "fill").exists() and (
                first / "fill"
            ).stat().st_size == 83886080:
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("Real writer did not fill the workspace")
        assert manager._exec_locks[identities[0]].locked()
        usage = ProjectUsage(volumes).measure("scope", 1, generation=1)
        assert usage["quota_mode"] == "soft" and usage["used_bytes"] >= 64 * MIB
        start = time.monotonic()
        await workspaces.enforce("scope", 1, 1)
        result = await asyncio.wait_for(task, timeout=30)
        stop_seconds = time.monotonic() - start
        assert result.returncode != 0
        stopped = await workspaces.status("scope", 1, generation=1)
        assert stopped["state"] == "stopped"
        other = await manager.exec("scope", identities[1], "printf unaffected")
        assert other.returncode == 0 and other.stdout == "unaffected"
        assert (first / "retained.txt").read_text() == "keep my project\n"
        try:
            await workspaces.operate("start", "scope", 1, generation=1, fence=4)
        except SandboxError as error:
            assert "project_quota_full" in str(error)
        else:
            raise AssertionError("Overfull workspace was allowed to restart")
        # Test-only cleanup, never a product auto-delete. Explicitly remove just
        # the generated fill file so that retained project recovery can be tested.
        (first / "fill").unlink()
        reopened = DirectoryProjectVolumes(
            root / "project-volumes", instance, host=host
        )
        assert len(reopened.projects()) == 2
        await workspaces.operate("start", "scope", 1, generation=1, fence=5)
        recovered = await manager.exec(
            "scope", identities[0], "cat /workspace/retained.txt"
        )
        assert recovered.returncode == 0 and recovered.stdout == "keep my project\n"
        print(
            json.dumps(
                {
                    "soft_usage_bytes": usage["used_bytes"],
                    "quota_bytes": 64 * MIB,
                    "writer_exit": result.returncode,
                    "stop_seconds": round(stop_seconds, 2),
                    "other_project": "unaffected",
                    "retained_data": "unchanged",
                    "restart": "passed",
                }
            )
        )
    finally:
        ids = docker(
            "ps", "-aq", "--filter", "label=gaoji.app.instance=" + instance
        ).splitlines()
        for identifier in ids:
            row = json.loads(docker("inspect", identifier))[0]
            assert row["Config"]["Labels"]["gaoji.app.instance"] == instance
            docker("rm", "-f", identifier)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        docker("volume", "rm", manager.nix_cache_volume)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--controller")
    parser.add_argument("--image", required=True)
    parser.add_argument("--guest", action="store_true")
    parser.add_argument("--instance")
    args = parser.parse_args()
    if not re.fullmatch(r"python@sha256:[a-f0-9]{64}", args.image):
        raise ValueError("An already-installed pinned Python image is required")
    if args.guest:
        if not re.fullmatch(r"[a-f0-9]{32}", args.instance or ""):
            raise ValueError("Invalid proof namespace")
        asyncio.run(
            guest(args.instance, "gaoji-desktop-" + args.instance + "-data", args.image)
        )
        return
    if not re.fullmatch(
        r"gaoji-desktop-controller:[a-f0-9]{64}", args.controller or ""
    ):
        raise ValueError("An already-installed controller image is required")
    docker("image", "inspect", args.controller)
    docker("image", "inspect", args.image)
    instance = uuid.uuid4().hex
    volume = "gaoji-desktop-" + instance + "-data"
    name = "gaoji-soft-proof-" + instance
    source = Path(__file__).resolve().parents[1]
    docker("volume", "create", "--label", "gaoji.project.proof=" + instance, volume)
    try:
        print(
            docker(
                "run",
                "--rm",
                "--name",
                name,
                "--pull=never",
                "--network=none",
                "--label",
                "gaoji.desktop.job=" + instance,
                "--memory=512m",
                "--cpus=1",
                "--cap-drop=ALL",
                "--cap-add=CHOWN",
                "--cap-add=DAC_OVERRIDE",
                "--cap-add=FOWNER",
                "--mount",
                f"type=volume,source={volume},target=/fixture",
                "--mount",
                f"type=bind,source={source},target=/opt/source,readonly",
                "--mount",
                "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
                "--workdir=/opt/source",
                "--env=PYTHONPATH=/opt/source",
                "--env=PYTHONDONTWRITEBYTECODE=1",
                "--entrypoint=python3",
                args.controller,
                "tools/verify_project_desktop.py",
                "--guest",
                "--instance",
                instance,
                "--image",
                args.image,
            )
        )
    finally:
        for identifier in docker(
            "ps", "-aq", "--filter", "label=gaoji.app.instance=" + instance
        ).splitlines():
            docker("rm", "-f", identifier)
        # A timed-out helper can remain alive; remove its exact fresh name first.
        existing = docker("ps", "-a", "--format", "{{.Names}}").splitlines()
        if name in existing:
            docker("rm", "-f", name)
        for item in docker(
            "volume",
            "ls",
            "--filter",
            "label=gaoji.project.proof=" + instance,
            "--format",
            "{{.Name}}",
        ).splitlines():
            docker("volume", "rm", item)


if __name__ == "__main__":
    main()
