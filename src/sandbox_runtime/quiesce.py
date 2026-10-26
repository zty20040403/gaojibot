"""Host service stop hook; not part of the Bot's RPC operation set."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re

from src.local_runtime.files import file_lock
from .docker import DockerSandboxManager, SandboxError, SANDBOX_ID_PATTERN
from .server import load_config, manager_from_config


async def stop_instance(manager: DockerSandboxManager) -> int:
    if not manager.namespace:
        raise ValueError("Cannot quiesce an unscoped Docker engine")
    listing = await manager._run("docker", "ps", "--no-trunc", "--quiet", "--filter",
        f"label=gaoji.app.instance={manager.namespace}", timeout=20)
    if listing.returncode:
        raise SandboxError("无法核对本实例的运行容器，尚未暂停沙盒。")
    identifiers = listing.stdout.split()
    if len(identifiers) > 64 or any(not re.fullmatch(r"[a-f0-9]{64}", item) for item in identifiers):
        raise SandboxError("本实例容器清单异常，尚未暂停沙盒。")
    stopped = 0
    for identifier in identifiers:
        inspected = await manager._run("docker", "inspect", "--format",
            "[{{json .Name}},{{json .Config.Labels}},{{json .State.Running}}]", identifier, timeout=20)
        if inspected.returncode:
            raise SandboxError("容器状态发生变化，暂停结果尚未确认。")
        try:
            name, labels, running = json.loads(inspected.stdout)
        except (ValueError, TypeError):
            raise SandboxError("无法核对容器归属。") from None
        if not isinstance(name, str) or not isinstance(labels, dict) or type(running) is not bool:
            raise SandboxError("无法核对容器归属。")
        if labels.get("gaoji.app.instance") != manager.namespace:
            raise SandboxError("容器不属于本实例。")
        kind = labels.get("gaoji.app.resource")
        sandbox_id = labels.get("qqbot.id", "")
        if kind == "helper":
            valid = re.fullmatch(r"/gaoji-app-" + manager.namespace + r"-helper-[a-f0-9]{16}", name)
        else:
            valid = (kind == "sandbox" and isinstance(sandbox_id, str) and SANDBOX_ID_PATTERN.fullmatch(sandbox_id)
                and labels.get("gaoji.app.sandbox") == "true"
                and name == "/" + manager._container_name(sandbox_id))
        if not valid:
            raise SandboxError("发现不符合本实例命名和归属记录的容器，未继续停止。")
        if running:
            result = await manager._run("docker", "stop", "--time", "5", identifier, timeout=20)
            if result.returncode:
                raise SandboxError("沙盒尚未确认停止，未删除工作文件。")
            stopped += 1
    verified = await manager._run("docker", "ps", "--quiet", "--filter",
        f"label=gaoji.app.instance={manager.namespace}", timeout=20)
    if verified.returncode or verified.stdout.strip():
        raise SandboxError("仍有本实例沙盒在运行，暂停未完成。")
    return stopped


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    # A live runner may still create containers. Never race it during backup.
    with file_lock(Path(config["state_directory"]) / ".runner.lock", timeout=1):
        async def quiesce():
            return await asyncio.wait_for(stop_instance(manager_from_config(config)), timeout=120)
        stopped = asyncio.run(quiesce())
    print(json.dumps({"quiesced": True, "stopped": stopped}))


if __name__ == "__main__":
    main()
